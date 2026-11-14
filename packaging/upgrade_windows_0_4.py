"""Assemble 0.4.0 from the pinned 0.3.0 Windows runtime and current source.

The builder verifies both release ZIPs, each declared file, and the optional
component's integration sidecar. It never executes a Windows binary. Windows
app/ and the source release come from one snapshot and are byte-identical.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import tempfile
import zipfile

from assemble_windows import (
    CELLPOSE_MODELS, CELLPOSE_ROOT_REQUIREMENTS, PROJECT_ROOT, PYTHON_SHA256,
    PYTHON_URL, PYTHON_VERSION, SOURCE_DIRS, SOURCE_FILES, sha256,
    validate_dependencies,
)
from upgrade_windows import canonical_path, inventory, verify_member

VERSION = '0.4.0'
BASE_VERSION = '0.3.0'
BASE_MAIN_SHA256 = 'd1d9e2f871054b2a563e789f149f68a0a55b60d9ce678db926ce3eeffa36861a'
COMPONENT_SHA256 = 'c758c1c8c80fe3d37b59980b5928cf2e50e1b144c83845f9c87eb1a2fff71b11'
MANIFEST = 'WINDOWS_BUILD_MANIFEST.json'
SOURCE_MANIFEST = 'SOURCE_MANIFEST.json'
COMPONENT_MANIFEST = 'CELLPOSE_BUILD_MANIFEST.json'
COMPONENT_INTEGRATION = 'CELLPOSE_INTEGRATION.json'
REPORT = 'packaging/windows_validation.json'
EXCLUDED_DIRS = {
    '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.cache',
    '.git', '.venv', 'venv', 'node_modules', 'dist', 'test-results',
    'playwright-report', 'htmlcov', '.tox', '.nox',
}
EXCLUDED_SUFFIXES = {'.pyc', '.pyo', '.exe'}
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
PE_AMD64 = {'machine_hex': '0x8664', 'format': 'PE32+'}


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def file_record(path: Path, relative: str) -> dict:
    return {'path': canonical_path(relative), 'bytes': path.stat().st_size,
            'sha256': sha256(path)}


def manifest_records(manifest: dict, manifest_name: str) -> dict[str, dict]:
    """Validate the complete declared set before verifying or extracting files."""
    values = manifest.get('files')
    if not isinstance(values, list) or not 1 <= len(values) <= 100000:
        raise ValueError('A release manifest must list 1–100,000 files.')
    records, folded = {}, set()
    for record in values:
        if not isinstance(record, dict):
            raise ValueError('Invalid manifest file record.')
        name = canonical_path(record.get('path'))
        if name == manifest_name or name.casefold() in folded:
            raise ValueError('Duplicate or self-referencing manifest file.')
        if (type(record.get('bytes')) is not int or record['bytes'] < 0
                or not isinstance(record.get('sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', record['sha256'])):
            raise ValueError(f'Invalid size or SHA-256 in manifest: {name}')
        records[name] = record
        folded.add(name.casefold())
    return records


def read_json_member(archive: zipfile.ZipFile, item: zipfile.ZipInfo) -> tuple[dict, bytes]:
    if item.file_size > MAX_MANIFEST_BYTES:
        raise ValueError('Release metadata exceeds its size limit.')
    content = archive.read(item)
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError('Release metadata must be a JSON object.')
    return value, content


def member_pe_identity(archive: zipfile.ZipFile, item: zipfile.ZipInfo) -> dict:
    """Inspect headers directly in the ZIP; dry runs need no extraction."""
    with archive.open(item) as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b'MZ':
            raise ValueError(f'Not a Windows executable: {item.filename}')
        offset = struct.unpack_from('<I', header, 0x3c)[0]
        if offset < 64 or offset > item.file_size - 26:
            raise ValueError(f'Invalid PE header offset: {item.filename}')
        stream.seek(offset)
        pe = stream.read(26)
    if len(pe) != 26 or pe[:4] != b'PE\0\0':
        raise ValueError(f'Invalid PE signature: {item.filename}')
    magic = struct.unpack_from('<H', pe, 24)[0]
    if magic not in (0x10b, 0x20b):
        raise ValueError(f'Invalid PE optional header: {item.filename}')
    return {'machine_hex': hex(struct.unpack_from('<H', pe, 4)[0]),
            'format': 'PE32+' if magic == 0x20b else 'PE32'}


def retained_core_file(name: str) -> bool:
    return name.startswith(('runtime/', 'licenses/')) or name in {
        'FigureWorkbench.exe', 'windows-packages.lock',
    }


def verify_release_inputs(base_main: Path, component: Path, project_root: Path,
                          destination: Path | None = None) -> dict:
    """Read and authenticate both inputs; optionally extract only retained core."""
    for path, expected_hash in ((base_main, BASE_MAIN_SHA256), (component, COMPONENT_SHA256)):
        if path.is_symlink() or not path.is_file() or sha256(path) != expected_hash:
            raise ValueError(f'Fixed release ZIP SHA-256 does not match: {path.name}')
    with zipfile.ZipFile(base_main) as main, zipfile.ZipFile(component) as cpu:
        main_files = inventory(main, f'Figure_Workbench_{BASE_VERSION}_Windows_x64/')
        cpu_files = inventory(cpu)
        if MANIFEST not in main_files or COMPONENT_MANIFEST not in cpu_files or COMPONENT_INTEGRATION not in cpu_files:
            raise ValueError('A required release manifest or component integration file is missing.')
        if set(main_files) & set(cpu_files):
            raise ValueError('Main and optional component paths overlap.')
        prior, prior_bytes = read_json_member(main, main_files[MANIFEST])
        if (prior.get('application') != 'Figure Workbench' or prior.get('version') != BASE_VERSION
                or prior.get('python') != {'version': PYTHON_VERSION, 'source': PYTHON_URL, 'sha256': PYTHON_SHA256}):
            raise ValueError('Unexpected base release or Python identity.')
        main_expected = manifest_records(prior, MANIFEST)
        if set(main_files) != set(main_expected) | {MANIFEST}:
            raise ValueError('Base main ZIP inventory differs from its manifest.')
        component_manifest, component_bytes = read_json_member(cpu, cpu_files[COMPONENT_MANIFEST])
        if (component_manifest.get('application') != 'Figure Workbench Cellpose CPU component'
                or component_manifest.get('version') != '0.2.0'
                or component_manifest.get('platform') != 'Windows x64'
                or component_manifest.get('python_version') != PYTHON_VERSION
                or component_manifest.get('python_source') != PYTHON_URL
                or component_manifest.get('python_sha256') != PYTHON_SHA256):
            raise ValueError('Unexpected optional component identity.')
        cpu_expected = manifest_records(component_manifest, COMPONENT_MANIFEST)
        if set(cpu_files) != set(cpu_expected) | {COMPONENT_MANIFEST, COMPONENT_INTEGRATION}:
            raise ValueError('Component ZIP inventory differs from its manifest and known integration sidecar.')
        if COMPONENT_INTEGRATION in cpu_expected:
            raise ValueError('The fixed component integration sidecar must be outside its source manifest.')
        component_manifest_hash = hashlib.sha256(component_bytes).hexdigest()
        prior_component = prior.get('optional_components', {}).get('cellpose', {})
        if (prior_component.get('archive_sha256') != COMPONENT_SHA256
                or prior_component.get('manifest_sha256') != component_manifest_hash):
            raise ValueError('Base release does not authenticate this optional component.')
        integration, integration_bytes = read_json_member(cpu, cpu_files[COMPONENT_INTEGRATION])
        if (integration.get('component') != 'Cellpose CPU' or integration.get('version') != '0.2.0'
                or integration.get('source_manifest') != COMPONENT_MANIFEST
                or integration.get('source_manifest_sha256') != component_manifest_hash
                or integration.get('source_files_modified') is not False
                or integration.get('separate_python_numpy_environment') is not True):
            raise ValueError('Invalid binding in the optional component integration sidecar.')
        wanted_models = {name: record['sha256'] for name, record in CELLPOSE_MODELS.items()}
        if component_manifest.get('models') != wanted_models:
            raise ValueError('The component does not identify the pinned official models.')
        for name, model in CELLPOSE_MODELS.items():
            record = cpu_expected.get(f'cellpose/models/{name}', {})
            if record.get('sha256') != model['sha256'] or record.get('bytes') != model['bytes']:
                raise ValueError(f'Pinned model is missing from the component file manifest: {name}')
        requirements = (project_root / 'requirements.txt').read_text('utf-8').splitlines()
        validate_dependencies(prior['dependencies'], requirements)
        validate_dependencies(component_manifest['dependencies'], CELLPOSE_ROOT_REQUIREMENTS)

        retained, binary_machines = [], []
        for name, item in main_files.items():
            if name == MANIFEST:
                continue
            keep = retained_core_file(name)
            target = destination / name if keep and destination is not None else None
            verify_member(main, item, main_expected[name], target)
            if keep:
                retained.append(main_expected[name])
                if Path(name).suffix.lower() in {'.exe', '.dll', '.pyd'}:
                    identity = member_pe_identity(main, item)
                    if identity != PE_AMD64:
                        raise ValueError(f'Retained core binary is not AMD64 PE32+: {name}')
                    binary_machines.append(identity['machine_hex'])
        required_core = {'FigureWorkbench.exe', 'runtime/python.exe', 'runtime/python312.dll',
                         'runtime/python312.zip', 'runtime/python312._pth', 'windows-packages.lock'}
        if not required_core.issubset({record['path'] for record in retained}):
            raise ValueError('Base release is missing required launcher or runtime files.')
        print(json.dumps({'stage': 'core_verified', 'files': len(main_files),
                          'retained_files': len(retained)}), flush=True)
        for name, record in cpu_expected.items():
            verify_member(cpu, cpu_files[name], record)
        print(json.dumps({'stage': 'component_verified', 'manifest_files': len(cpu_expected),
                          'authenticated_metadata_files': 2}), flush=True)

    component_record = {
        'component_version': '0.2.0',
        'compatible_application_versions': ['0.2.0', BASE_VERSION, VERSION],
        'compatibility_basis': 'Unchanged isolated runtime; static dependency and provenance checks.',
        'bundled_in_main_archive': False, 'archive': component.name,
        'archive_bytes': component.stat().st_size, 'archive_sha256': COMPONENT_SHA256,
        'verified_files': len(cpu_files), 'manifest_declared_files_verified': len(cpu_expected),
        'manifest_sha256': component_manifest_hash,
        'integration_sha256': hashlib.sha256(integration_bytes).hexdigest(),
        'integration_authenticated_by': 'Pinned whole-archive SHA-256 and manifest binding.',
        'files_modified': False, 'dependency_count': len(component_manifest['dependencies']),
        'models': wanted_models,
        'install': 'Extract the unchanged optional ZIP beside FigureWorkbench.exe; retain cellpose/ and provenance files.',
        'windows_runtime_executed': False,
    }
    return {
        'prior': prior,
        'base_verification': {
            'base_application_version': BASE_VERSION, 'main_zip_sha256': BASE_MAIN_SHA256,
            'component_zip_sha256': COMPONENT_SHA256,
            'prior_manifest_sha256': hashlib.sha256(prior_bytes).hexdigest(),
            'main_files_verified': len(main_files), 'component_files_verified': len(cpu_files),
            'zip_crc_and_file_sha256_verified': True,
            'retained_runtime_license_files': len(retained), 'retained_files_byte_identical': True,
        },
        'retained_files': retained, 'optional_component': component_record,
        'pe_scan': {'binaries': len(binary_machines), 'machine_counts': dict(Counter(binary_machines)),
                    'scope': 'Retained main launcher and all core .exe/.dll/.pyd files',
                    'full_dll_import_closure_checked': False},
    }


def source_paths(root: Path) -> list[Path]:
    """Prune dependency/cache/build trees before walking, including nested ones."""
    paths, folded = [], set()
    for directory, directories, files in os.walk(root, followlinks=False):
        current = Path(directory)
        relative_directory = current.relative_to(root)
        allowed = []
        for name in sorted(directories):
            if name in EXCLUDED_DIRS or (current == root and name not in SOURCE_DIRS):
                continue
            child = current / name
            if child.is_symlink():
                raise ValueError(f'Source symlinks cannot be packaged: {child}')
            allowed.append(name)
        directories[:] = allowed
        for name in sorted(files):
            if current == root and name not in SOURCE_FILES:
                continue
            path = current / name
            if path.suffix.lower() in EXCLUDED_SUFFIXES or name in {'.DS_Store', 'Thumbs.db', '.coverage'}:
                continue
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise ValueError(f'Source symlink or special file cannot be packaged: {path}')
            relative = canonical_path((relative_directory / name).as_posix())
            if relative.casefold() in folded:
                raise ValueError(f'Case-insensitive source collision: {relative}')
            folded.add(relative.casefold())
            paths.append(path)
    return sorted(paths)


def snapshot_source(root: Path, destination: Path) -> list[dict]:
    paths = source_paths(root)
    expected = [file_record(path, path.relative_to(root).as_posix()) for path in paths]
    for source, record in zip(paths, expected):
        target = destination / record['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open('rb') as stream, target.open('xb') as output:
            shutil.copyfileobj(stream, output, 1024 * 1024)
        if file_record(target, record['path']) != record:
            raise ValueError(f'Source changed during snapshot: {record["path"]}')
    return expected


def directory_records(directory: Path, *, exclude: set[str] | None = None) -> list[dict]:
    records = []
    for path in sorted(directory.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'Release symlinks are not supported: {path}')
        if path.is_file():
            relative = canonical_path(path.relative_to(directory).as_posix())
            if relative not in (exclude or set()):
                records.append(file_record(path, relative))
    return records


def assert_matching_source(app: Path, source: Path) -> int:
    app_records, source_records = directory_records(app), directory_records(source)
    if app_records != source_records:
        raise ValueError('Source release and Windows app/ are not byte-identical.')
    return len(app_records)


def source_versions(root: Path) -> dict:
    versions = {}
    module = ast.parse((root / 'workbench/__init__.py').read_text('utf-8'))
    for statement in module.body:
        if isinstance(statement, ast.Assign) and any(isinstance(target, ast.Name) and target.id == '__version__'
                                                    for target in statement.targets):
            versions['python'] = ast.literal_eval(statement.value)
    versions['package'] = json.loads((root / 'package.json').read_text('utf-8')).get('version')
    return versions


def write_zip(directory: Path, destination: Path) -> dict:
    """Stable member metadata, exclusive creation, and complete CRC validation."""
    if destination.exists():
        raise ValueError(f'Refusing to replace archive: {destination}')
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6, allowZip64=True) as archive:
        for path in sorted(directory.rglob('*')):
            if path.is_symlink():
                raise ValueError('Cannot archive a symlink.')
            if not path.is_file():
                continue
            name = canonical_path(directory.name + '/' + path.relative_to(directory).as_posix())
            info = zipfile.ZipInfo(name, date_time=(2026, 11, 12, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            with path.open('rb') as stream, archive.open(info, 'w', force_zip64=True) as output:
                shutil.copyfileobj(stream, output, 1024 * 1024)
    with zipfile.ZipFile(destination) as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError(f'ZIP CRC failure: {bad}')
        count = len(archive.infolist())
    return {'file': destination.name, 'bytes': destination.stat().st_size,
            'sha256': sha256(destination), 'entries': count, 'crc_verified': True}


START_HERE = '''图研 · Figure Workbench 0.4.0 — Windows x64 便携版

1. 将 Figure_Workbench_0.4.0_Windows_Main.zip 完整解压。
2. 打开解压后的文件夹，双击 FigureWorkbench.exe。
3. 浏览器自动打开本机工作台，无需安装 Python、Node.js 或管理员权限。

不要在 ZIP 内启动或只复制 EXE；保留 FigureWorkbench.exe、runtime/、app/。
如需 Cellpose，沿用原 0.2.0 CPU 组件，将其完整解压到同一文件夹；
主包未重复包含此选件。普通组图和现有常规图像分析无需该选件。
工程和原图保存在 %LOCALAPPDATA%\\FigureWorkbench，升级时请保留该目录。
退出时按控制台 Ctrl+C；仅关闭浏览器不会停止本机服务。

0.4.0 组图功能及期刊规则边界请参阅 app/docs/JOURNAL_PRESETS.md。
独立免安装浏览器文件单独发布，不重复装入 Windows app/。
构建与验证范围：WINDOWS_BUILD.md；逐文件清单：WINDOWS_BUILD_MANIFEST.json。

这是便携目录包，不是单文件安装器。当前构建只做了字节、依赖、PE 架构与
归档校验，没有执行 Windows 程序，也没有完成真机启动、字体或 Office 验收。
'''


def build(base_main: Path, component: Path, output: Path, *, source_output: Path | None = None,
          project_root: Path = PROJECT_ROOT, make_zip: bool = True, dry_run: bool = False) -> dict:
    project_root, output = project_root.resolve(), output.resolve()
    source_output = (source_output or output.parent / f'Figure_Workbench_{VERSION}_Source').resolve()
    if output == source_output or output.parent != source_output.parent:
        raise ValueError('Main and source output must be different sibling directories.')
    for path in (output, source_output):
        canonical_path(path.name)
        if path == project_root or (path.is_relative_to(project_root)
                                    and path.relative_to(project_root).parts[0] in SOURCE_DIRS):
            raise ValueError('Build output must not be inside a packaged source directory; use dist/.')
    versions = source_versions(project_root)
    version_ready = versions == {'python': VERSION, 'package': VERSION}
    if dry_run:
        verified = verify_release_inputs(base_main, component, project_root)
        return {'application_version': VERSION, 'dry_run': True, 'output_written': False,
                'source_versions': versions, 'source_versions_ready': version_ready,
                'source_files_selected': len(source_paths(project_root)),
                'base_release_verification': verified['base_verification'],
                'pe_architecture_scan': verified['pe_scan'],
                'optional_cellpose_component': verified['optional_component'],
                'windows_runtime_executed': False}
    if not version_ready:
        raise ValueError(f'Source version must be {VERSION} in Python and package.json before release: {versions}')
    destinations = [output, source_output]
    main_zip = output.parent / f'Figure_Workbench_{VERSION}_Windows_Main.zip'
    source_zip = output.parent / f'Figure_Workbench_{VERSION}_Source.zip'
    if make_zip:
        destinations.extend([main_zip, source_zip])
    if any(path.exists() for path in destinations):
        raise ValueError('Use new output paths; existing release directories or archives are never overwritten.')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.figure-workbench-0.4-build-', dir=output.parent) as temporary:
        staging = Path(temporary)
        main_directory, source_directory = staging / output.name, staging / source_output.name
        main_directory.mkdir()
        verified = verify_release_inputs(base_main, component, project_root, main_directory)
        app = main_directory / 'app'
        initial_records = snapshot_source(project_root, app)
        if source_versions(app) != {'python': VERSION, 'package': VERSION}:
            raise ValueError('Source version changed during the build.')
        shutil.copy2(app / 'LICENSE', main_directory / 'LICENSE')
        shutil.copy2(app / 'docs/WINDOWS_0_4_BUILD.md', main_directory / 'WINDOWS_BUILD.md')
        (main_directory / 'WINDOWS_START_HERE.txt').write_text(START_HERE, 'utf-8')
        created = datetime.now(timezone.utc).isoformat()
        launcher = main_directory / 'FigureWorkbench.exe'
        limitations = [
            'Windows startup, browser association, font and Office rendering have not been executed.',
            'Static architecture and byte checks do not prove Windows DLL import closure.',
            'Optional imagecodecs HEIF/JPEG-XS/Jetraw codecs are not supplied.',
            'The unchanged Cellpose CPU 0.2.0 archive remains a separate optional component.',
            'The self-contained browser HTML is distributed separately, not duplicated in app/.',
        ]
        report = {
            'version': VERSION, 'build': 'windows-x64-portable-4', 'recorded_at_utc': created,
            'status': 'assembled_static_checks_passed', 'distribution': 'core_with_optional_cellpose_cpu',
            'windows_runtime_executed': False,
            'base_release_verification': verified['base_verification'],
            'native_launcher': {'path': 'FigureWorkbench.exe', 'bytes': launcher.stat().st_size,
                                'sha256': sha256(launcher), **PE_AMD64, 'windows_runtime_executed': False},
            'dependencies': {'core_count': len(verified['prior']['dependencies']),
                             'current_requirements_and_windows_markers_verified': True,
                             'runtime_unchanged_from_verified_base': True},
            'pe_architecture_scan': verified['pe_scan'],
            'optional_cellpose_component': verified['optional_component'],
            'source_snapshot': {'input_files': len(initial_records), 'workspace_modified': False,
                                'generated_metadata': [REPORT, SOURCE_MANIFEST],
                                'source_release_and_windows_app_byte_identical': True,
                                'excluded_directory_names': sorted(EXCLUDED_DIRS)},
            'test_records': {'tests_executed_by_this_builder': False, 'execution_platform': 'None',
                             'note': 'Application test reports retain their own recorded version and platform.'},
            'limitations': limitations,
        }
        report_path = app / REPORT
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_bytes(json_bytes(report))
        records = directory_records(app, exclude={SOURCE_MANIFEST})
        snapshot_hash = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        source_manifest = {
            'application': 'Figure Workbench', 'version': VERSION, 'created_at_utc': created,
            'files': records, 'file_count': len(records), 'files_snapshot_sha256': snapshot_hash,
            'manifest_self_excluded': True, 'matches_windows_app': True,
            'windows_runtime_executed': False,
        }
        (app / SOURCE_MANIFEST).write_bytes(json_bytes(source_manifest))
        shutil.copytree(app, source_directory)
        source_count = assert_matching_source(app, source_directory)
        manifest = {
            'application': 'Figure Workbench', 'version': VERSION, 'build': report['build'],
            'built_at_utc': created, 'distribution': report['distribution'],
            'python': verified['prior']['python'], 'microsoft_runtime': verified['prior']['microsoft_runtime'],
            'dependencies': verified['prior']['dependencies'], 'windows_dependency_markers_verified': True,
            'runtime_provenance': verified['base_verification'],
            'optional_components': {'cellpose': verified['optional_component']},
            'application_source': {'files': source_count, 'manifest': f'app/{SOURCE_MANIFEST}',
                                   'manifest_sha256': sha256(app / SOURCE_MANIFEST),
                                   'files_snapshot_sha256': snapshot_hash,
                                   'source_archive': source_zip.name if make_zip else None,
                                   'source_directory': source_output.name, 'byte_identical': True},
            'windows_runtime_execution_verified': False, 'validation_report': f'app/{REPORT}',
            'files': directory_records(main_directory, exclude={MANIFEST}), 'limitations': limitations,
        }
        (main_directory / MANIFEST).write_bytes(json_bytes(manifest))
        result = {'directory': str(output), 'source_directory': str(source_output),
                  'application_version': VERSION, 'main_files': len(manifest['files']) + 1,
                  'source_files': source_count, 'source_app_byte_identical': True,
                  'manifest_sha256': sha256(main_directory / MANIFEST),
                  'optional_cellpose_component': verified['optional_component'],
                  'windows_runtime_executed': False}
        moves = [(main_directory, output), (source_directory, source_output)]
        if make_zip:
            staged_main_zip, staged_source_zip = staging / main_zip.name, staging / source_zip.name
            result['archive'] = write_zip(main_directory, staged_main_zip)
            result['source_archive'] = write_zip(source_directory, staged_source_zip)
            moves.extend([(staged_main_zip, main_zip), (staged_source_zip, source_zip)])
        if any(path.exists() for path in destinations):
            raise ValueError('An output appeared during the build; refusing to replace it.')
        for source, destination in moves:
            source.rename(destination)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-main', type=Path, required=True)
    parser.add_argument('--cellpose-component-zip', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-output', type=Path)
    parser.add_argument('--no-zip', action='store_true')
    parser.add_argument('--dry-run', action='store_true', help='Verify inputs and report readiness without writing release files.')
    parser.add_argument('--record', type=Path, help='Write a new external JSON build receipt; never overwrite an existing one.')
    options = parser.parse_args()
    if options.record and options.record.exists():
        parser.error('--record must name a new file.')
    result = build(options.base_main, options.cellpose_component_zip, options.output,
                   source_output=options.source_output, make_zip=not options.no_zip, dry_run=options.dry_run)
    if options.record:
        with options.record.open('xb') as stream:
            stream.write(json_bytes(result))
    print(json.dumps(result, ensure_ascii=False, indent=2))
