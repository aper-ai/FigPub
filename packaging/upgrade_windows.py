"""Build 0.3 from the byte-verified 0.2 Windows runtime and current application.

This builder never executes Windows code. Both prior release archives are
verified in full against their recorded ZIP hashes and the original manifest.
The unchanged optional Cellpose component is verified but not copied into the
main ZIP. Its original component manifest and model provenance remain valid.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import zipfile

from assemble_windows import (PROJECT_ROOT, copy_source, pe_identity, sha256,
                              validate_dependencies)

VERSION = '0.3.0'
BASE_MAIN_SHA256 = '769abacbc8723433cf8b96374b8e7c1f11d7a5f1c5ee47a874a4b41a5554eb43'
COMPONENT_SHA256 = 'c758c1c8c80fe3d37b59980b5928cf2e50e1b144c83845f9c87eb1a2fff71b11'
MANIFEST = 'WINDOWS_BUILD_MANIFEST.json'


def canonical_path(name: str) -> str:
    """Reject Windows aliases before accepting any archive or manifest path."""
    if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
        raise ValueError('Expected a nonempty canonical relative path.')
    path = PurePosixPath(name)
    if path.is_absolute() or '/'.join(path.parts) != name or '..' in path.parts:
        raise ValueError(f'Unsafe path: {name!r}')
    for part in path.parts:
        if (part.rstrip(' .') != part or any(ord(c) < 32 or c in ':<>"|?*' for c in part)
                or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', part.split('.')[0])):
            raise ValueError(f'Unsafe Windows path: {name!r}')
    return name


def inventory(archive: zipfile.ZipFile, prefix: str = '') -> dict[str, zipfile.ZipInfo]:
    records: dict[str, zipfile.ZipInfo] = {}
    folded: set[str] = set()
    for item in archive.infolist():
        # zipfile normalizes backslashes on Windows and truncates NULs. The
        # original spelling must survive unchanged, including directories.
        if item.orig_filename != item.filename:
            raise ValueError('Unsafe archive member path normalization.')
        if item.is_dir():
            continue
        if not item.filename.startswith(prefix):
            raise ValueError('Archive member is outside the declared root.')
        relative = canonical_path(item.filename[len(prefix):])
        if relative.casefold() in folded or stat.S_ISLNK(item.external_attr >> 16):
            raise ValueError(f'Archive collision or symlink: {relative}')
        folded.add(relative.casefold())
        records[relative] = item
    return records


def verify_member(archive: zipfile.ZipFile, item: zipfile.ZipInfo, expected: dict,
                  destination: Path | None = None) -> None:
    if item.file_size != expected['bytes']:
        raise ValueError(f'Incorrect file length: {item.filename}')
    digest = hashlib.sha256()
    if destination is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
    output = destination.open('xb') if destination is not None else None
    try:
        with archive.open(item) as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
                if output is not None:
                    output.write(chunk)
        if digest.hexdigest() != expected['sha256']:
            raise ValueError(f'File digest mismatch: {item.filename}')
    finally:
        if output is not None:
            output.close()


def file_records(directory: Path) -> list[dict]:
    result = []
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'Symlinks are not supported: {path}')
        if path.is_file():
            name = canonical_path(path.relative_to(directory).as_posix())
            if name != MANIFEST:
                result.append({'path': name, 'bytes': path.stat().st_size, 'sha256': sha256(path)})
    return result


def write_zip(directory: Path, path: Path) -> dict:
    """Stable member order/timestamps, followed by a full CRC verification."""
    if path.exists():
        raise ValueError(f'Refusing to replace archive: {path}')
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6,
                         allowZip64=True) as archive:
        for file in sorted(directory.rglob('*')):
            if not file.is_file():
                continue
            if file.is_symlink():
                raise ValueError('Cannot package a symlink.')
            name = directory.name + '/' + canonical_path(file.relative_to(directory).as_posix())
            info = zipfile.ZipInfo(name, date_time=(2026, 11, 11, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            with file.open('rb') as source, archive.open(info, 'w', force_zip64=True) as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
    with zipfile.ZipFile(path) as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError(f'ZIP CRC failure: {bad}')
        count = len(archive.infolist())
    return {'file': path.name, 'bytes': path.stat().st_size, 'sha256': sha256(path),
            'entries': count, 'crc_verified': True}


def build(base_main: Path, component: Path, output: Path, *, make_zip: bool = True) -> dict:
    if output.exists():
        raise ValueError('Use a new output directory; existing releases are never overwritten.')
    if sha256(base_main) != BASE_MAIN_SHA256 or sha256(component) != COMPONENT_SHA256:
        raise ValueError('The prior release archive SHA-256 does not match the fixed build input.')
    with zipfile.ZipFile(base_main) as main, zipfile.ZipFile(component) as cpu:
        prefix = 'Figure_Workbench_0.2.0_Windows_x64/'
        main_files = inventory(main, prefix)
        cpu_files = inventory(cpu)
        if set(main_files) & set(cpu_files):
            raise ValueError('Main and component archive members overlap.')
        prior_bytes = main.read(main_files[MANIFEST])
        prior = json.loads(prior_bytes)
        if (prior.get('application') != 'Figure Workbench' or prior.get('version') != '0.2.0'
                or prior.get('python', {}).get('version') != '3.12.10'):
            raise ValueError('Unexpected base release identity.')
        expected: dict[str, dict] = {}
        folded: set[str] = set()
        for record in prior['files']:
            name = canonical_path(record['path'])
            if name.casefold() in folded:
                raise ValueError('Duplicate base manifest file.')
            folded.add(name.casefold())
            expected[name] = record
        if set(expected) != (set(main_files) | set(cpu_files)) - {MANIFEST}:
            raise ValueError('Prior release manifests and archive contents differ.')
        # Validate the current app's dependency closure against the retained core.
        validate_dependencies(prior['dependencies'])
        output.mkdir(parents=True)
        retained = 0
        for name, item in main_files.items():
            if name == MANIFEST:
                continue
            keep = name.startswith(('runtime/', 'licenses/')) or name in {
                'FigureWorkbench.exe', 'windows-packages.lock'}
            verify_member(main, item, expected[name], output / name if keep else None)
            retained += int(keep)
        print(json.dumps({'stage': 'core_verified', 'files': len(main_files) - 1,
                          'retained_runtime_license_files': retained}), flush=True)
        for name, item in cpu_files.items():
            verify_member(cpu, item, expected[name])
        component_manifest = json.loads(cpu.read(cpu_files['CELLPOSE_BUILD_MANIFEST.json']))
        if component_manifest.get('version') != '0.2.0':
            raise ValueError('Unexpected optional component version.')
        print(json.dumps({'stage': 'component_verified', 'files': len(cpu_files)}), flush=True)

    source_count = copy_source(output / 'app')
    for source, name in ((PROJECT_ROOT / 'LICENSE', 'LICENSE'),
                         (PROJECT_ROOT / 'docs/WINDOWS_START_HERE.txt', 'WINDOWS_START_HERE.txt'),
                         (PROJECT_ROOT / 'docs/WINDOWS_BUILD.md', 'WINDOWS_BUILD.md')):
        shutil.copy2(source, output / name)
    binaries = []
    for file in sorted((output / 'runtime').rglob('*')):
        if file.is_file() and file.suffix.lower() in {'.exe', '.dll', '.pyd'}:
            identity = pe_identity(file)
            if identity != {'machine_hex': '0x8664', 'format': 'PE32+'}:
                raise ValueError(f'Non-AMD64 core binary: {file}')
            binaries.append(identity['machine_hex'])
    launcher = output / 'FigureWorkbench.exe'
    if pe_identity(launcher) != {'machine_hex': '0x8664', 'format': 'PE32+'}:
        raise ValueError('The application launcher must be AMD64 PE32+.')
    binaries.append('0x8664')
    created = datetime.now(timezone.utc).isoformat()
    component_record = {
        'component_version': '0.2.0', 'compatible_application_versions': ['0.2.0', VERSION],
        'bundled_in_main_archive': False, 'archive': component.name,
        'archive_sha256': COMPONENT_SHA256, 'verified_files': len(cpu_files),
        'manifest_sha256': expected['CELLPOSE_BUILD_MANIFEST.json']['sha256'],
        'files_modified': False, 'dependency_count': len(component_manifest['dependencies']),
        'models': component_manifest['models'],
        'install': 'Place the existing cellpose directory beside FigureWorkbench.exe; keep its provenance files.',
        'windows_runtime_executed': False,
    }
    report = {
        'version': VERSION, 'build': 'windows-x64-portable-3', 'recorded_at_utc': created,
        'status': 'assembled_static_checks_passed', 'distribution': 'core_with_optional_cellpose_cpu',
        'windows_runtime_executed': False,
        'base_release_verification': {
            'main_zip_sha256': BASE_MAIN_SHA256, 'component_zip_sha256': COMPONENT_SHA256,
            'prior_manifest_sha256': hashlib.sha256(prior_bytes).hexdigest(),
            'individual_files_verified': len(expected), 'zip_crc_and_file_sha256_verified': True,
            'retained_runtime_license_files': retained, 'retained_files_byte_identical': True},
        'native_launcher': {'path': 'FigureWorkbench.exe', 'bytes': launcher.stat().st_size,
                            'sha256': sha256(launcher), **pe_identity(launcher),
                            'windows_runtime_executed': False},
        'dependencies': {'core_count': len(prior['dependencies']),
                         'current_requirements_and_windows_markers_verified': True,
                         'runtime_unchanged_from_verified_base': True,
                         'wheel_record_verification': 'Performed in the original build; full installed files rehashed in this build.'},
        'pe_architecture_scan': {'binaries': len(binaries), 'machine_counts': dict(Counter(binaries)),
                                'scope': 'Main package launcher and all core .exe/.dll/.pyd files',
                                'full_dll_import_closure_checked': False},
        'optional_cellpose_component': component_record,
        'source_files': source_count,
        'test_records': {'application': 'app/benchmarks/validation.json',
                         'documentation': 'app/docs/VALIDATION.md',
                         'execution_platform': 'Linux', 'tests_executed_by_this_builder': False},
        'limitations': ['Windows startup, browser association, font and Office rendering have not been executed.',
                        'Static architecture and byte checks do not prove Windows DLL import closure.',
                        'Optional imagecodecs HEIF/JPEG-XS/Jetraw codecs are not supplied.',
                        'Official Cellpose CPU models are a separate unchanged optional component.'],
    }
    report_text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    # Include the exact same newly generated report in source and Windows app.
    (PROJECT_ROOT / 'packaging/windows_validation.json').write_text(report_text, 'utf-8')
    (output / 'app/packaging/windows_validation.json').write_text(report_text, 'utf-8')
    manifest = {
        'application': 'Figure Workbench', 'version': VERSION, 'build': report['build'],
        'built_at_utc': created, 'distribution': report['distribution'], 'python': prior['python'],
        'microsoft_runtime': prior['microsoft_runtime'], 'dependencies': prior['dependencies'],
        'windows_dependency_markers_verified': True,
        'runtime_provenance': report['base_release_verification'],
        'optional_components': {'cellpose': component_record},
        'windows_runtime_execution_verified': False,
        'validation_report': 'app/packaging/windows_validation.json',
        'files': file_records(output), 'limitations': report['limitations'],
    }
    (output / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    result = {'directory': str(output), 'application_version': VERSION,
              'main_files': len(manifest['files']) + 1, 'source_files': source_count,
              'manifest_sha256': sha256(output / MANIFEST),
              'windows_runtime_executed': False}
    if make_zip:
        result['archive'] = write_zip(output, output.parent / (output.name + '.zip'))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-main', type=Path, required=True)
    parser.add_argument('--cellpose-component-zip', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--no-zip', action='store_true')
    parser.add_argument('--record', type=Path)
    options = parser.parse_args()
    result = build(options.base_main, options.cellpose_component_zip, options.output, make_zip=not options.no_zip)
    if options.record:
        options.record.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
