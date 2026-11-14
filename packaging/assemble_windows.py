"""Assemble an offline Windows distribution from verified Windows wheel files.

This is a builder, not an installer run by end users. It never executes a Windows
binary on the build host. The resulting folder includes all runtime dependencies.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import zipfile
import csv
import io
import re
import struct

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import canonicalize_name, parse_wheel_filename

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = '3.12.10'
PYTHON_URL = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip'
PYTHON_SHA256 = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'
SOURCE_DIRS = {'workbench', 'web', 'resources', 'licenses', 'docs', 'tests', 'benchmarks', 'scripts', 'packaging', '.github'}
SOURCE_FILES = {'README.md', 'LICENSE', 'launch.py', 'run.py', 'start_windows.bat', 'start_unix.sh',
                'requirements.txt', 'requirements-dev.txt', 'requirements-desktop.txt', 'pytest.ini', 'package.json',
                'package-lock.json', '.gitignore', '.gitattributes', '.oxlintrc.json'}
EXCLUDED_DIRS = {'__pycache__', '.pytest_cache', 'node_modules', '.venv', '.git', 'test-results'}
CELLPOSE_ROOT_REQUIREMENTS = ['cellpose==3.1.1.3', 'torch==2.7.1+cpu', 'numpy==2.0.2']
CELLPOSE_MODELS = {
    'cyto3': {'sha256': '2dc3087a8abd7da46d1ab0ddd5824639933cc3ff63b382af3fa1939a392db93c',
              'bytes': 26566255, 'source_url': 'https://www.cellpose.org/models/cyto3'},
    'nucleitorch_0': {'sha256': '89ca45e4a45048d5010d29621466b1274b91b3dcb9714dce9f9e90a9a8671303',
                    'bytes': 26563614, 'source_url': 'https://www.cellpose.org/models/nucleitorch_0'},
}
CELLPOSE_MANIFEST = 'CELLPOSE_BUILD_MANIFEST.json'
CELLPOSE_NOTES = 'Cellpose_CPU_使用说明.txt'


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def safe_parts(name: str) -> tuple[str, ...]:
    # Some Windows wheels use backslashes in RECORD even when ZIP members use
    # forward slashes. Normalize before validating, never after joining paths.
    path = PurePosixPath(name.replace('\\', '/'))
    if path.is_absolute() or '..' in path.parts or ':' in name or '\x00' in name:
        raise ValueError(f'Unsafe archive member: {name!r}')
    return path.parts


def distribution_member(archive: zipfile.ZipFile, filename: str) -> str:
    # Vendored distributions may carry their own nested METADATA/RECORD files.
    # Wheel installation must verify the top-level distribution's RECORD.
    matches = [name for name in archive.namelist() if len(safe_parts(name)) == 2
               and safe_parts(name)[0].endswith('.dist-info') and safe_parts(name)[1] == filename]
    if len(matches) != 1:
        raise ValueError(f'Expected exactly one top-level wheel {filename}.')
    return matches[0]


def wheel_metadata(path: Path) -> dict:
    _, _, _, tags = parse_wheel_filename(path.name)
    supported = set(cpython_tags((3, 12), abis=['cp312'], platforms=['win_amd64']))
    supported.update(compatible_tags((3, 12), interpreter='cp312', platforms=['win_amd64']))
    if not tags.intersection(supported):
        raise ValueError(f'Wheel is not a CPython 3.12 Windows x64 dependency: {path.name}')
    with zipfile.ZipFile(path) as archive:
        metadata_name = distribution_member(archive, 'METADATA')
        metadata = BytesParser().parsebytes(archive.read(metadata_name))
    if metadata.get('Requires-Python') and PYTHON_VERSION not in SpecifierSet(metadata['Requires-Python']):
        raise ValueError(f'Wheel requires a different Python version: {path.name}')
    return {'name': metadata['Name'], 'version': metadata['Version'],
            'requires_dist': metadata.get_all('Requires-Dist', []),
            'file': path.name, 'size_bytes': path.stat().st_size, 'sha256': sha256(path)}


def validate_dependencies(records: list[dict], root_requirements: list[str] | None = None) -> None:
    environment = default_environment()
    environment.update(os_name='nt', sys_platform='win32', platform_system='Windows',
                       platform_machine='AMD64', python_version='3.12',
                       platform_release='10', platform_version='10.0.19045',
                       python_full_version=PYTHON_VERSION, implementation_name='cpython',
                       implementation_version=PYTHON_VERSION, extra='')
    packages = {canonicalize_name(item['name']): item for item in records}
    if len(packages) != len(records):
        raise ValueError('Multiple versions of the same package were supplied.')
    requirements = [(None, line) for line in (root_requirements if root_requirements is not None else (PROJECT_ROOT / 'requirements.txt').read_text().splitlines())
                    if line.strip() and not line.lstrip().startswith('#')]
    requirements += [(item['name'], requirement) for item in records for requirement in item['requires_dist']]
    for parent, expression in requirements:
        requirement = Requirement(expression)
        if requirement.marker and not requirement.marker.evaluate(environment):
            continue
        package = packages.get(canonicalize_name(requirement.name))
        if not package or package['version'] not in requirement.specifier:
            raise ValueError(f'Unsatisfied Windows dependency for {parent or "application"}: {expression}')


def install_wheel(path: Path, runtime: Path) -> int:
    site_packages = runtime / 'Lib' / 'site-packages'
    count = 0
    with zipfile.ZipFile(path) as archive:
        names = {'/'.join(safe_parts(name)): name for name in archive.namelist() if not name.endswith('/')}
        if len(names) != len([item for item in archive.infolist() if not item.is_dir()]):
            raise ValueError(f'Duplicate normalized wheel paths: {path.name}')
        record_name = distribution_member(archive, 'RECORD')
        for name, digest, size in csv.reader(io.StringIO(archive.read(record_name).decode('utf-8'))):
            normalized_name = '/'.join(safe_parts(name))
            if normalized_name not in names:
                raise ValueError(f'Missing wheel RECORD entry: {path.name}: {name}')
            if digest:
                algorithm, expected = digest.split('=', 1)
                if algorithm not in {'sha256', 'sha384', 'sha512'}:
                    raise ValueError(f'Unsupported wheel digest: {algorithm}')
                content = archive.read(names[normalized_name])
                actual = base64.urlsafe_b64encode(hashlib.new(algorithm, content).digest()).rstrip(b'=').decode('ascii')
                if actual != expected or (size and len(content) != int(size)):
                    raise ValueError(f'Wheel RECORD integrity failure: {path.name}: {name}')
        for item in archive.infolist():
            parts = safe_parts(item.filename)
            if item.is_dir():
                continue
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Wheel symlinks are not supported.')
            destination = site_packages
            if parts[0].endswith('.data'):
                scheme = parts[1]
                destinations = {'purelib': site_packages, 'platlib': site_packages,
                                'scripts': runtime / 'Scripts', 'headers': runtime / 'Include', 'data': runtime}
                if scheme not in destinations:
                    raise ValueError(f'Unsupported wheel installation scheme: {scheme}')
                destination = destinations[scheme]
                parts = parts[2:]
            target = destination.joinpath(*parts)
            if target.exists():
                raise ValueError(f'Wheel collision: {target}')
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output)
            count += 1
    return count


def copy_source(app: Path) -> int:
    count = 0
    for path in sorted(PROJECT_ROOT.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(PROJECT_ROOT)
        if any(part in EXCLUDED_DIRS for part in relative.parts) or path.suffix in {'.pyc', '.exe'}:
            continue
        if len(relative.parts) == 1 and relative.name not in SOURCE_FILES:
            continue
        if len(relative.parts) > 1 and relative.parts[0] not in SOURCE_DIRS:
            continue
        target = app / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        count += 1
    return count


def component_member(name: str) -> str:
    """Accept canonical, portable paths that cannot alias on Windows."""
    if not isinstance(name, str) or not name or '\\' in name:
        raise ValueError('Component paths must be nonempty canonical POSIX relative paths.')
    parts = safe_parts(name)
    if not parts or '/'.join(parts) != name:
        raise ValueError(f'Noncanonical component path: {name!r}')
    for part in parts:
        if (part.rstrip(' .') != part or any(ord(char) < 32 or char in '<>"|?*' for char in part)
                or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', part.split('.')[0])):
            raise ValueError(f'Unsafe Windows component path: {name!r}')
    if parts[0] != 'cellpose' and name not in {CELLPOSE_MANIFEST, CELLPOSE_NOTES}:
        raise ValueError(f'Unexpected component root file: {name!r}')
    return name


def pe_identity(path: Path) -> dict:
    with path.open('rb') as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b'MZ':
            raise ValueError(f'Not a Windows executable: {path.name}')
        offset = struct.unpack_from('<I', header, 0x3c)[0]
        if offset < 64 or offset > path.stat().st_size - 26:
            raise ValueError(f'Invalid PE header offset: {path.name}')
        stream.seek(offset)
        pe = stream.read(26)
    if pe[:4] != b'PE\0\0' or struct.unpack_from('<H', pe, 24)[0] not in (0x10b, 0x20b):
        raise ValueError(f'Invalid PE signature or optional header: {path.name}')
    return {'machine_hex': hex(struct.unpack_from('<H', pe, 4)[0]),
            'format': 'PE32+' if struct.unpack_from('<H', pe, 24)[0] == 0x20b else 'PE32'}


def verify_amd64_pe(path: Path) -> None:
    identity = pe_identity(path)
    if identity != {'machine_hex': '0x8664', 'format': 'PE32+'}:
        raise ValueError(f'Component entry must be an AMD64 PE32+ file: {path.name}')


def validate_cellpose_component(source: Path, vc_runtime: Path) -> dict:
    """Read-only verification before an existing component is copied anywhere."""
    if source.is_symlink() or not source.is_dir():
        raise ValueError('Cellpose component must be an existing real directory, not a symlink.')
    source = source.resolve()
    inventory = {}
    folded_paths = set()
    for path in sorted(source.rglob('*')):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f'Component symlink or special file is not supported: {path}')
        relative = component_member(path.relative_to(source).as_posix())
        if relative.casefold() in folded_paths:
            raise ValueError(f'Case-insensitive Windows path collision: {relative}')
        folded_paths.add(relative.casefold())
        if path.is_file():
            inventory[relative] = path
    manifest_path = inventory.get(CELLPOSE_MANIFEST)
    if manifest_path is None or manifest_path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('Component must contain a bounded CELLPOSE_BUILD_MANIFEST.json.')
    manifest = json.loads(manifest_path.read_text('utf-8'))
    if (not isinstance(manifest, dict) or manifest.get('application') != 'Figure Workbench Cellpose CPU component'
            or manifest.get('version') != '0.2.0' or manifest.get('platform') != 'Windows x64'
            or manifest.get('python_version') != PYTHON_VERSION
            or manifest.get('python_sha256') != PYTHON_SHA256 or manifest.get('python_source') != PYTHON_URL):
        raise ValueError('Component manifest has an unsupported application, platform or Python identity.')
    files = manifest.get('files')
    if not isinstance(files, list) or not 1 <= len(files) <= 100000:
        raise ValueError('Component manifest must list 1–100,000 files.')
    expected = {}
    folded_files = set()
    for record in files:
        if not isinstance(record, dict):
            raise ValueError('Invalid component manifest file record.')
        name = component_member(record.get('path'))
        if name == CELLPOSE_MANIFEST or name.casefold() in folded_files:
            raise ValueError(f'Duplicate or self-referencing component manifest entry: {name}')
        if (type(record.get('bytes')) is not int or record['bytes'] < 0
                or not isinstance(record.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', record['sha256'])):
            raise ValueError(f'Invalid component size/hash record: {name}')
        expected[name] = {'path': name, 'bytes': record['bytes'], 'sha256': record['sha256']}
        folded_files.add(name.casefold())
    if set(inventory) != set(expected) | {CELLPOSE_MANIFEST}:
        missing = sorted(set(expected) - set(inventory))[:5]
        extra = sorted(set(inventory) - set(expected) - {CELLPOSE_MANIFEST})[:5]
        raise ValueError(f'Component file inventory differs from its manifest; missing={missing}, extra={extra}.')
    for name, record in expected.items():
        if inventory[name].stat().st_size != record['bytes'] or sha256(inventory[name]) != record['sha256']:
            raise ValueError(f'Component file hash/size mismatch: {name}')
    expected[CELLPOSE_MANIFEST] = {'path': CELLPOSE_MANIFEST, 'bytes': manifest_path.stat().st_size,
                                   'sha256': sha256(manifest_path)}
    required = ['cellpose/runtime/python.exe', 'cellpose/runtime/python312.dll',
                'cellpose/runtime/python312.zip', 'cellpose/runtime/python312._pth',
                'cellpose/runtime/LICENSE.txt', 'cellpose/models/MODEL_SOURCES.json',
                'cellpose/models/CELLPOSE-LICENSE.txt', 'cellpose/windows-packages.lock', CELLPOSE_NOTES]
    if any(name not in expected for name in required):
        raise ValueError('Component is missing its interpreter, standard library, model provenance or licenses.')
    for filename in ('python.exe', 'python312.dll'):
        verify_amd64_pe(source / 'cellpose' / 'runtime' / filename)
    paths = [line.strip() for line in (source / 'cellpose/runtime/python312._pth').read_text('utf-8-sig').splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    if paths != ['python312.zip', '.', 'Lib/site-packages', 'import site']:
        raise ValueError('Cellpose runtime paths must remain isolated from the application and system Python.')
    dependencies = manifest.get('dependencies')
    if not isinstance(dependencies, list) or not 1 <= len(dependencies) <= 128 or not all(isinstance(item, dict) for item in dependencies):
        raise ValueError('Component dependencies must be recorded.')
    validate_dependencies(dependencies, CELLPOSE_ROOT_REQUIREMENTS)
    package_map = {canonicalize_name(item['name']): item for item in dependencies}
    installed = {}
    for name, path in inventory.items():
        parts = PurePosixPath(name).parts
        if (len(parts) == 6 and parts[:4] == ('cellpose', 'runtime', 'Lib', 'site-packages')
                and parts[4].endswith('.dist-info') and parts[5] == 'METADATA'):
            if path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError('Installed component package metadata exceeds its size limit.')
            metadata = BytesParser().parsebytes(path.read_bytes())
            package_name = canonicalize_name(metadata['Name'] or '')
            if not package_name or package_name in installed:
                raise ValueError('Duplicate or invalid installed package metadata.')
            installed[package_name] = metadata
    if set(installed) != set(package_map):
        raise ValueError('Installed component packages differ from the dependency manifest.')
    for name, record in package_map.items():
        metadata = installed[name]
        if (metadata['Version'] != record['version']
                or sorted(metadata.get_all('Requires-Dist', [])) != sorted(record.get('requires_dist', []))):
            raise ValueError(f'Installed component dependency metadata differs: {name}')
    for module in ('cellpose/models.py', 'torch/__init__.py', 'numpy/__init__.py'):
        if 'cellpose/runtime/Lib/site-packages/' + module not in expected:
            raise ValueError(f'Component Python package entry is missing: {module}')
    lock_lines = {line.strip() for line in (source / 'cellpose/windows-packages.lock').read_text('utf-8').splitlines() if line.strip()}
    wanted_lock = {f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}" for item in dependencies}
    if lock_lines != wanted_lock:
        raise ValueError('Component dependency lock differs from its recorded packages.')
    wanted_models = {name: record['sha256'] for name, record in CELLPOSE_MODELS.items()}
    if manifest.get('models') != wanted_models:
        raise ValueError('Component manifest does not identify the pinned official models.')
    provenance_path = source / 'cellpose/models/MODEL_SOURCES.json'
    if provenance_path.stat().st_size > 1024 * 1024:
        raise ValueError('Model provenance exceeds its size limit.')
    provenance = json.loads(provenance_path.read_text('utf-8'))
    if not isinstance(provenance, dict):
        raise ValueError('Model provenance must be an object.')
    source_models = provenance.get('models', [])
    if (not isinstance(source_models, list) or len(source_models) != len(CELLPOSE_MODELS)
            or any(not isinstance(item, dict) for item in source_models)):
        raise ValueError('Component model source records are incomplete.')
    source_models = {item.get('filename'): item for item in source_models}
    for name, spec in CELLPOSE_MODELS.items():
        entry = expected.get('cellpose/models/' + name)
        declared = source_models.get(name, {})
        if (not entry or entry['sha256'] != spec['sha256'] or entry['bytes'] != spec['bytes']
                or any(declared.get(key) != value for key, value in spec.items())):
            raise ValueError(f'Official model bytes or provenance do not match: {name}')
    library = provenance.get('library', {})
    if (not isinstance(library, dict) or library.get('version') != '3.1.1.3' or library.get('license_file') != 'CELLPOSE-LICENSE.txt'
            or library.get('wheel_sha256') != package_map['cellpose']['sha256']):
        raise ValueError('Cellpose library provenance differs from the component dependency lock.')
    # The main package already preserves the complete original Microsoft notices.
    # Sharing them is permitted only after verifying identical DLL bytes in both
    # runtimes, never by treating Microsoft code as application-licensed code.
    shared_notices = vc_runtime / 'licenses' / 'Microsoft-VC-Redist-License.txt'
    if not shared_notices.is_file() or not shared_notices.stat().st_size:
        raise ValueError('The merged distribution requires the complete original Microsoft runtime license.')
    runtime_dlls = list(vc_runtime.glob('*.dll'))
    if not runtime_dlls:
        raise ValueError('Microsoft runtime files are missing.')
    for path in runtime_dlls:
        entry = expected.get('cellpose/runtime/' + path.name)
        if not entry or sha256(path) != entry['sha256']:
            raise ValueError(f'The component and main package do not share identical Microsoft runtime bytes: {path.name}')
    return {'source': source, 'manifest': manifest, 'files': expected,
            'source_manifest_sha256': expected[CELLPOSE_MANIFEST]['sha256']}


def copy_cellpose_component(verified: dict, output: Path) -> dict:
    source = verified['source']
    if output.resolve().is_relative_to(source):
        raise ValueError('The application output cannot be created inside its component source.')
    destinations = [(source / name, output / name) for name in verified['files']]
    if any(destination.exists() for _, destination in destinations):
        raise ValueError('Cellpose files collide with an existing application output.')
    for original, destination in destinations:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, destination)
    report = {'component': 'Cellpose CPU', 'version': '0.2.0', 'integration': 'same_folder_offline_bundle',
              'source_manifest': CELLPOSE_MANIFEST, 'source_manifest_sha256': verified['source_manifest_sha256'],
              'imported_files': len(verified['files']),
              'imported_bytes': sum(item['bytes'] for item in verified['files'].values()),
              'runtime': 'cellpose/runtime/python.exe', 'worker': 'app/workbench/cellpose_worker.py',
              'separate_python_numpy_environment': True, 'numpy_version': '2.0.2',
              'numba_threading_layer': 'workqueue', 'models': CELLPOSE_MODELS,
              'dependency_count': len(verified['manifest']['dependencies']),
              'shared_microsoft_notices': 'licenses/Microsoft-VC-Runtime',
              'shared_microsoft_dll_hashes_verified': True, 'source_files_modified': False,
              'windows_runtime_execution_verified': False,
              'original_component_instructions': CELLPOSE_NOTES}
    if not (output / report['worker']).is_file():
        raise ValueError('The main application is missing its independent Cellpose worker.')
    (output / 'CELLPOSE_INTEGRATION.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    return report


def verify_component_coverage(files: list[dict], verified: dict) -> None:
    by_path = {record['path']: record for record in files}
    if len(by_path) != len(files):
        raise ValueError('The application manifest contains duplicate file paths.')
    for name, expected in verified['files'].items():
        if by_path.get(name) != expected:
            raise ValueError(f'Merged component file is missing or changed: {name}')


def write_validation_report(output: Path, manifest: dict, component: dict | None) -> dict:
    """Record this build's real checks without relabeling old test results."""
    relative = 'app/packaging/windows_validation.json'
    checked_files = [record for record in manifest['files'] if record['path'] != relative]
    pe_files = [record for record in checked_files if Path(record['path']).suffix.lower() in {'.exe', '.dll', '.pyd'}]
    architectures = {}
    unused_templates = []
    template_machines = {'cli.exe': '0x14c', 'gui.exe': '0x14c', 'cli-32.exe': '0x14c', 'gui-32.exe': '0x14c',
                         'cli-arm64.exe': '0xaa64', 'gui-arm64.exe': '0xaa64'}
    template_directories = {'runtime/Lib/site-packages/setuptools', 'cellpose/runtime/Lib/site-packages/setuptools'}
    for record in pe_files:
        identity = pe_identity(output / record['path'])
        machine = identity['machine_hex']
        architectures[machine] = architectures.get(machine, 0) + 1
        if identity != {'machine_hex': '0x8664', 'format': 'PE32+'}:
            path = PurePosixPath(record['path'])
            if path.parent.as_posix() not in template_directories or template_machines.get(path.name) != machine:
                raise ValueError(f'Unexpected non-AMD64 executable in the application payload: {record["path"]}')
            unused_templates.append({'path': record['path'], **identity, 'sha256': record['sha256'],
                                     'role': 'Unmodified setuptools launcher template; not executed by this application.'})
    launcher = next(record for record in checked_files if record['path'] == 'FigureWorkbench.exe')
    report = {'version': manifest['version'], 'build': manifest['build'],
              'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
              'status': 'assembled_static_checks_passed', 'distribution': manifest['distribution'],
              'windows_runtime_executed': False,
              'native_launcher': {**launcher, 'architecture': 'AMD64', 'format': 'PE32+',
                                  'windows_runtime_executed': False},
              'windows_wheel_validation': {'core_count': len(manifest['dependencies']),
                                           'cellpose_count': len(component['manifest']['dependencies']) if component else 0,
                                           'windows_markers_satisfied': True, 'target_python': PYTHON_VERSION,
                                           'platform': 'win_amd64', 'wheel_record_hashes_verified_at_install': True},
              'windows_payload_pe_scan': {'binaries': len(pe_files), 'machine_counts': architectures,
                                         'non_amd64_binaries': len(unused_templates),
                                         'unexpected_non_amd64_binaries': 0,
                                         'vendored_unused_launcher_templates': unused_templates,
                                         'scope': 'PE signatures and architecture; runtime entries require AMD64 PE32+; known unused setuptools templates retained.',
                                         'full_dll_import_closure_checked_by_this_builder': False},
              'payload_hashes': {'files_verified_excluding_this_report_and_main_manifest': len(checked_files),
                                 'main_manifest': 'WINDOWS_BUILD_MANIFEST.json'},
              'cellpose': manifest['optional_components'].get('cellpose'),
              'test_records': {'application_validation': 'app/docs/VALIDATION.md',
                               'real_cellpose_cpu_smoke': 'app/benchmarks/cellpose_cpu_smoke.json' if component else None,
                               'platform_of_those_execution_tests': 'Linux',
                               'tests_executed_by_this_builder': False},
              'archive_verification': 'Final ZIP integrity and hash are reported separately after archive creation.',
              'limitations': ['No Windows dynamic execution was performed.',
                              'PE architecture checks do not prove DLL import closure or successful Windows startup.',
                              'Optional imagecodecs HEIF/JPEG-XS/Jetraw libraries are not supplied.',
                              'Cellpose uses the workqueue backend; optional Numba TBB dependencies are not supplied.']}
    destination = output / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    manifest['files'] = sorted([*checked_files, {'path': relative, 'bytes': destination.stat().st_size,
                                               'sha256': sha256(destination)}], key=lambda record: record['path'])
    manifest['validation_report'] = relative
    manifest['pe_architecture_scan'] = report['windows_payload_pe_scan']
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python-zip', type=Path, required=True)
    parser.add_argument('--wheels', type=Path, required=True)
    parser.add_argument('--launcher', type=Path, required=True)
    parser.add_argument('--vc-runtime', type=Path, required=True,
                        help='Official Microsoft x64 app-local redistributable DLLs and source/license records')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cellpose-component', type=Path,
                        help='Verified standalone CPU component directory to merge beside FigureWorkbench.exe')
    parser.add_argument('--no-zip', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise ValueError('Output must be a new directory; existing application/data folders are never deleted.')
    if args.cellpose_component and output.is_relative_to(args.cellpose_component.resolve()):
        raise ValueError('Output must not be inside the component source directory.')
    if sha256(args.python_zip) != PYTHON_SHA256:
        raise ValueError('Python embedded archive does not match the recorded release digest.')
    runtime_dlls = {path.name.lower(): path for path in args.vc_runtime.glob('*.dll')}
    for filename in ['msvcp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll', 'vcomp140.dll']:
        if filename not in runtime_dlls:
            raise ValueError(f'Missing required Microsoft x64 runtime: {filename}')
    wheels = sorted(args.wheels.glob('*.whl'))
    records = [wheel_metadata(path) for path in wheels]
    validate_dependencies(records)
    component = validate_cellpose_component(args.cellpose_component, args.vc_runtime) if args.cellpose_component else None
    output.mkdir(parents=True)
    runtime = output / 'runtime'
    runtime.mkdir()
    with zipfile.ZipFile(args.python_zip) as archive:
        for item in archive.infolist():
            safe_parts(item.filename)
        archive.extractall(runtime)
    for wheel in wheels:
        install_wheel(wheel, runtime)
    for path in runtime_dlls.values():
        shutil.copy2(path, runtime / path.name)
    microsoft_notices = output / 'licenses' / 'Microsoft-VC-Runtime'
    microsoft_notices.mkdir(parents=True)
    for path in args.vc_runtime.iterdir():
        if path.is_file() and path.suffix.lower() != '.dll':
            shutil.copy2(path, microsoft_notices / path.name)
    if (args.vc_runtime / 'licenses').is_dir():
        shutil.copytree(args.vc_runtime / 'licenses', microsoft_notices / 'licenses')
    # Explicit paths prevent accidental use of system Python or user packages.
    (runtime / 'python312._pth').write_text('python312.zip\n.\nLib/site-packages\n../app\nimport site\n', 'ascii')
    source_count = copy_source(output / 'app')
    shutil.copy2(args.launcher, output / 'FigureWorkbench.exe')
    shutil.copy2(PROJECT_ROOT / 'LICENSE', output / 'LICENSE')
    for file_name in ['WINDOWS_START_HERE.txt', 'WINDOWS_BUILD.md']:
        source = PROJECT_ROOT / 'docs' / file_name
        if source.is_file():
            shutil.copy2(source, output / file_name)
    (output / 'windows-packages.lock').write_text('\n'.join(
        f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}" for item in records) + '\n', 'utf-8')
    component_report = copy_cellpose_component(component, output) if component else None
    manifest = {'application': 'Figure Workbench', 'version': '0.3.0', 'build': 'windows-x64-portable-3',
                'built_at_utc': datetime.now(timezone.utc).isoformat(),
                'python': {'version': PYTHON_VERSION, 'source': PYTHON_URL, 'sha256': PYTHON_SHA256},
                'microsoft_runtime': [{'file': path.name, 'sha256': sha256(path)} for path in runtime_dlls.values()],
                'dependencies': records, 'windows_dependency_markers_verified': True,
                'wheel_record_hashes_verified': True, 'source_files': source_count,
                'distribution': 'core_with_cellpose_cpu' if component else 'core',
                'optional_components': {'cellpose': component_report} if component_report else {},
                'windows_runtime_execution_verified': False,
                'limitations': ['Windows native launch, browser integration and Office rendering still need Windows acceptance.'],
                'files': [{'path': file.relative_to(output).as_posix(), 'bytes': file.stat().st_size,
                           'sha256': sha256(file)} for file in sorted(output.rglob('*')) if file.is_file()]}
    if component:
        verify_component_coverage(manifest['files'], component)
    validation = write_validation_report(output, manifest, component)
    (output / 'WINDOWS_BUILD_MANIFEST.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    result = {'directory': str(output), 'files': len(manifest['files']) + 1,
              'uncompressed_bytes': sum(item['bytes'] for item in manifest['files']) + (output / 'WINDOWS_BUILD_MANIFEST.json').stat().st_size,
              'dependencies': len(records), 'windows_runtime_execution_verified': False,
              'manifest_sha256': sha256(output / 'WINDOWS_BUILD_MANIFEST.json'),
              'validation_report': str(output / manifest['validation_report']),
              'pe_binaries': validation['windows_payload_pe_scan']['binaries']}
    if component_report:
        result['cellpose_component'] = component_report
    if not args.no_zip:
        archive_path = output.with_name(output.name + '.zip')
        with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for file in sorted(output.rglob('*')):
                if file.is_file():
                    archive.write(file, output.name + '/' + file.relative_to(output).as_posix())
        with zipfile.ZipFile(archive_path) as archive:
            if archive.testzip() is not None:
                raise ValueError('Final ZIP integrity check failed.')
        result.update(archive=str(archive_path), archive_bytes=archive_path.stat().st_size, sha256=sha256(archive_path))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
