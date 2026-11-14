"""Build a portable Windows CPU candidate from the actually tested environment.

Use the pinned 0.4 embedded Python foundation, never its scientific packages.
Reinstall exact-version Windows wheels with verified RECORDs, then compare their
module/resource bytes with the tested Windows venv. Generated venv launchers,
absolute paths, pip metadata and bytecode are not copied into the component.

This is a static builder. Both official models MUST subsequently run using this
candidate's embedded interpreter; successful venv inference is not acceptance
of the portable component. No release is published by this command.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import zipfile

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

from assemble_windows import (CELLPOSE_MODELS, CELLPOSE_ROOT_REQUIREMENTS, PROJECT_ROOT,
                              PYTHON_SHA256, PYTHON_URL, PYTHON_VERSION, install_wheel,
                              sha256, validate_dependencies, verify_amd64_pe, wheel_metadata)
from upgrade_windows import canonical_path, inventory, verify_member
from upgrade_windows_0_4 import (directory_records, file_record, json_bytes,
                                 manifest_records, read_json_member)

sys.path.insert(0, str(PROJECT_ROOT))
from scripts.release_config import VERSION, CPU_INTERMEDIATE
BASE_MAIN_SHA256 = '5aac14044bbd5000f4fc31754a6bd1d08f2e9809d501393af86359cf24f03fb8'
MANIFEST = 'CELLPOSE_BUILD_MANIFEST.json'
INTEGRATION = 'CELLPOSE_INTEGRATION.json'
ISOLATED_PTH = b'python312.zip\n.\nLib/site-packages\nimport site\n'
# The sole reviewed executable .pth is setuptools' local distutils shim.
# Any upstream change requires explicit review, not a wider executable allowlist.
DISTUTILS_PTH = (b"import os; var = 'SETUPTOOLS_USE_DISTUTILS'; enabled = os.environ.get(var, 'local') == 'local'; "
                 b"enabled and __import__('_distutils_hack').add_shim(); \n")
GENERATED_METADATA = {'RECORD', 'INSTALLER', 'REQUESTED', 'direct_url.json'}


def _files(root: Path, *, ignore_bytecode=False) -> dict[str, Path]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Expected a real directory: ' + str(root))
    result, seen = {}, set()
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError('Symlinks and special files are not portable: ' + str(path))
        name = canonical_path(path.relative_to(root).as_posix())
        if name.casefold() in seen:
            raise ValueError('Windows path collision: ' + name)
        seen.add(name.casefold())
        if ignore_bytecode and ('__pycache__' in path.parts or path.suffix.lower() in {'.pyc', '.pyo'}):
            continue
        if path.is_file():
            result[name] = path
    return result


def _generated_metadata(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return len(parts) == 2 and parts[0].endswith('.dist-info') and parts[1] in GENERATED_METADATA


def inspect_environment(home: Path) -> dict:
    if home.is_symlink():
        raise ValueError('The tested environment root may not be a symlink.')
    home = home.resolve()
    venv, site = home / 'venv', home / 'venv/Lib/site-packages'
    if any(path.is_symlink() for path in (venv, venv / 'Lib', venv / 'Scripts')):
        raise ValueError('The tested environment must not redirect runtime directories outside its root.')
    config = dict(line.split('=', 1) for line in (venv / 'pyvenv.cfg').read_text('utf-8').splitlines() if '=' in line)
    config = {key.strip(): value.strip() for key, value in config.items()}
    if config.get('version') != PYTHON_VERSION or config.get('include-system-site-packages', '').lower() != 'false':
        raise ValueError('The tested environment must be isolated Windows Python ' + PYTHON_VERSION + '.')
    verify_amd64_pe(venv / 'Scripts/python.exe')
    freeze_path = home / 'dependencies.txt'
    frozen = {}
    for line in freeze_path.read_text('utf-8').splitlines():
        if not line.strip():
            continue
        requirement = Requirement(line.strip())
        versions = list(requirement.specifier)
        if (requirement.url or requirement.marker or requirement.extras or len(versions) != 1
                or versions[0].operator != '==' or '*' in versions[0].version):
            raise ValueError('Freeze must contain only exact package==version records: ' + line)
        name = canonicalize_name(requirement.name)
        if name in frozen or name == 'pip':
            raise ValueError('Duplicate or installer-only package in runtime freeze: ' + name)
        frozen[name] = versions[0].version
    if not frozen:
        raise ValueError('The tested dependency freeze is empty.')
    files = _files(site, ignore_bytecode=True)
    installed, bootstrap = {}, set()
    for name, path in files.items():
        parts = PurePosixPath(name).parts
        if len(parts) != 2 or not parts[0].endswith('.dist-info') or parts[1] != 'METADATA':
            continue
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError('Installed package metadata is too large.')
        metadata = BytesParser().parsebytes(path.read_bytes())
        package = canonicalize_name(metadata.get('Name', ''))
        if not package or package in installed:
            raise ValueError('Missing or duplicate installed package identity.')
        if package == 'pip':
            bootstrap.update({parts[0], 'pip'})
            continue
        if metadata.get('Requires-Python') and PYTHON_VERSION not in SpecifierSet(metadata['Requires-Python']):
            raise ValueError('Installed dependency does not support embedded Python: ' + package)
        record, wheel = site / parts[0] / 'RECORD', site / parts[0] / 'WHEEL'
        if not record.is_file() or not wheel.is_file():
            raise ValueError('Every runtime dependency must have wheel metadata and an installed RECORD.')
        direct = site / parts[0] / 'direct_url.json'
        if direct.is_file():
            origin = json.loads(direct.read_text('utf-8'))
            if origin.get('dir_info') is not None or str(origin.get('url', '')).startswith('file:'):
                raise ValueError('Local/editable runtime installations cannot form a portable candidate.')
        installed[package] = {'name': metadata['Name'], 'version': metadata['Version'],
                              'requires_dist': metadata.get_all('Requires-Dist', []),
                              'dist_info': parts[0], 'installed_metadata_sha256': sha256(path),
                              'installed_record_sha256': sha256(record), 'installed_wheel_metadata_sha256': sha256(wheel)}
    if {name: value['version'] for name, value in installed.items()} != frozen:
        raise ValueError('Installed runtime packages differ from the complete tested freeze (pip is excluded).')
    validate_dependencies(list(installed.values()), CELLPOSE_ROOT_REQUIREMENTS)
    return {'home': home, 'venv': venv, 'site': site, 'files': files, 'bootstrap': bootstrap,
            'packages': installed, 'freeze_sha256': sha256(freeze_path)}


def download_wheels(environment: dict, destination: Path) -> None:
    if os.name != 'nt':
        raise ValueError('Downloading through the tested Windows venv requires Windows; use --wheels for static offline builds.')
    destination.mkdir()
    common = [str(environment['venv'] / 'Scripts/python.exe'), '-m', 'pip', 'download',
              '--disable-pip-version-check', '--no-input', '--only-binary=:all:', '--no-deps',
              '--platform', 'win_amd64', '--implementation', 'cp', '--python-version', PYTHON_VERSION,
              '--abi', 'cp312', '--dest', str(destination)]
    packages = environment['packages']
    torch = packages['torch']
    subprocess.run([*common, '--index-url', 'https://download.pytorch.org/whl/cpu',
                    f"torch=={torch['version']}"], check=True)
    ordinary = [f"{item['name']}=={item['version']}" for name, item in sorted(packages.items()) if name != 'torch']
    subprocess.run([*common, '--index-url', 'https://pypi.org/simple', *ordinary], check=True)


def copy_python_foundation(base_main: Path, component: Path) -> dict:
    if base_main.is_symlink() or sha256(base_main) != BASE_MAIN_SHA256:
        raise ValueError('The Python foundation must come from the verified 0.4.0 main ZIP.')
    retained = []
    with zipfile.ZipFile(base_main) as archive:
        files = inventory(archive, 'Figure_Workbench_0.4.0_Windows_x64/')
        manifest, manifest_bytes = read_json_member(archive, files['WINDOWS_BUILD_MANIFEST.json'])
        if (manifest.get('application') != 'Figure Workbench' or manifest.get('version') != '0.4.0'
                or manifest.get('python') != {'version': PYTHON_VERSION, 'source': PYTHON_URL, 'sha256': PYTHON_SHA256}):
            raise ValueError('Unexpected base Python or application identity.')
        expected = manifest_records(manifest, 'WINDOWS_BUILD_MANIFEST.json')
        if set(files) != set(expected) | {'WINDOWS_BUILD_MANIFEST.json'}:
            raise ValueError('Base ZIP inventory differs from its manifest.')
        for name, record in expected.items():
            parts = PurePosixPath(name).parts
            if (parts[0] == 'runtime' and len(parts) == 2) or name.startswith('licenses/'):
                verify_member(archive, files[name], record, component / name)
                retained.append(record)
    required = ['runtime/python.exe', 'runtime/pythonw.exe', 'runtime/python312.dll',
                'runtime/python312.zip', 'runtime/python312._pth', 'runtime/LICENSE.txt',
                'licenses/Microsoft-VC-Runtime/licenses/Microsoft-VC-Redist-License.txt']
    if any(not (component / name).is_file() for name in required):
        raise ValueError('The base Python foundation or original Microsoft license is incomplete.')
    for name in ('python.exe', 'python312.dll'):
        verify_amd64_pe(component / 'runtime' / name)
    (component / 'runtime/python312._pth').write_bytes(ISOLATED_PTH)
    return {'archive_sha256': BASE_MAIN_SHA256, 'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
            'retained_files': retained, 'main_scientific_packages_copied': False,
            'python_pth_replaced_with_isolated_paths': True}


def _validate_wheel_inventory(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = inventory(archive)
        records = [name for name in names if len(PurePosixPath(name).parts) == 2
                   and PurePosixPath(name).parts[0].endswith('.dist-info') and name.endswith('/RECORD')]
        if len(records) != 1:
            raise ValueError('Wheel needs one top-level RECORD: ' + path.name)
        declared = set()
        for row in csv.reader(io.StringIO(archive.read(records[0]).decode('utf-8'))):
            if len(row) != 3:
                raise ValueError('Invalid wheel RECORD: ' + path.name)
            name = canonical_path(row[0].replace('\\', '/'))
            if name in declared or (not row[1] and name != records[0]):
                raise ValueError('Duplicate or unhashed wheel payload: ' + name)
            declared.add(name)
        if declared != set(names):
            raise ValueError('Every wheel payload must appear in RECORD: ' + path.name)
        if any('__pycache__' in PurePosixPath(name).parts or Path(name).suffix.lower() in {'.pyc', '.pyo'} for name in names):
            raise ValueError('Wheel contains precompiled Python bytecode: ' + path.name)


def install_tested_wheels(environment: dict, wheels: Path, runtime: Path) -> tuple[list[dict], dict]:
    wheel_files = [path for name, path in _files(wheels).items() if '/' not in name and path.suffix == '.whl']
    records = [wheel_metadata(path) for path in wheel_files]
    wanted = environment['packages']
    if (len(records) != len(wanted) or {canonicalize_name(item['name']): item['version'] for item in records}
            != {name: item['version'] for name, item in wanted.items()}):
        raise ValueError('Exactly one matching wheel is required for every tested runtime dependency.')
    validate_dependencies(records, CELLPOSE_ROOT_REQUIREMENTS)
    for path, record in zip(wheel_files, records):
        _validate_wheel_inventory(path)
        record['installed_files'] = install_wheel(path, runtime)
        original = wanted[canonicalize_name(record['name'])]
        if sorted(record['requires_dist']) != sorted(original['requires_dist']):
            raise ValueError('Downloaded wheel dependencies differ from the tested installation.')
        record['tested_environment_metadata'] = {key: original[key] for key in
            ('installed_metadata_sha256', 'installed_record_sha256', 'installed_wheel_metadata_sha256')}
    site = runtime / 'Lib/site-packages'
    candidate = {name: path for name, path in _files(site).items() if not _generated_metadata(name)}
    source = {name: path for name, path in environment['files'].items()
              if PurePosixPath(name).parts[0] not in environment['bootstrap'] and not _generated_metadata(name)}
    if set(candidate) != set(source):
        raise ValueError('Tested installed module/resource inventory differs from the pristine wheels; '
                         f'missing={sorted(set(source)-set(candidate))[:5]}, extra={sorted(set(candidate)-set(source))[:5]}.')
    for name, path in candidate.items():
        if path.stat().st_size != source[name].stat().st_size or sha256(path) != sha256(source[name]):
            raise ValueError('Tested module/resource differs from downloaded wheel: ' + name)
    for module in ('cellpose/models.py', 'torch/__init__.py', 'numpy/__init__.py'):
        if module not in candidate:
            raise ValueError('A required CPU package entry is missing: ' + module)
    hooks = audit_portable_paths(site)
    return records, {'module_resource_files_compared': len(candidate), 'module_resource_bytes_equal': True,
                     'venv_bytecode_and_generated_entrypoints_copied': False,
                     'ignored_installer_metadata': sorted(GENERATED_METADATA), 'pth_files': hooks}


def audit_portable_paths(site: Path) -> list[dict]:
    reviewed = []
    for name, path in _files(site).items():
        if path.suffix == '.egg-link' or name.endswith('.egg-info'):
            raise ValueError('Editable/legacy import links are not portable: ' + name)
        if path.suffix.lower() != '.pth':
            continue
        content = path.read_bytes()
        if name == 'distutils-precedence.pth' and content == DISTUTILS_PTH:
            reviewed.append({**file_record(path, name), 'kind': 'reviewed_setuptools_local_distutils_shim'})
            continue
        for line in content.decode('utf-8-sig').splitlines():
            value = line.strip()
            if not value or value.startswith('#'):
                continue
            if value.startswith(('import ', 'import\t')):
                raise ValueError('Unreviewed executable .pth: ' + name)
            relative = canonical_path(value.replace('\\', '/'))
            target = path.parent / relative
            if not target.resolve().is_relative_to(site.resolve()) or not target.is_dir():
                raise ValueError('External or missing .pth directory: ' + name)
        reviewed.append({**file_record(path, name), 'kind': 'relative_internal_paths'})
    return reviewed


def copy_models(environment: dict, component: Path, dependencies: list[dict]) -> dict:
    home = environment['home']
    receipt_path = home / 'models.json'
    receipt = json.loads(receipt_path.read_text('utf-8'))
    expected_receipt = {('nuclei' if name == 'nucleitorch_0' else name):
                        {'bytes': spec['bytes'], 'sha256': spec['sha256'], 'url': spec['source_url']}
                        for name, spec in CELLPOSE_MODELS.items()}
    if (not isinstance(receipt, list) or len(receipt) != len(expected_receipt)
            or any(not isinstance(item, dict) for item in receipt)
            or {item.get('model'): {key: item.get(key) for key in ('bytes', 'sha256', 'url')} for item in receipt}
            != expected_receipt):
        raise ValueError('The tested model receipt does not match both official weights.')
    models = component / 'models'
    models.mkdir()
    for name, spec in CELLPOSE_MODELS.items():
        path = home / 'models' / name
        if path.is_symlink() or path.stat().st_size != spec['bytes'] or sha256(path) != spec['sha256']:
            raise ValueError('Official model hash/length mismatch: ' + name)
        shutil.copy2(path, models / name)
    package = next(item for item in dependencies if canonicalize_name(item['name']) == 'cellpose')
    dist_info = environment['packages']['cellpose']['dist_info']
    licenses = component / 'runtime/Lib/site-packages' / dist_info
    license_path = next((path for path in [licenses / 'licenses/LICENSE', licenses / 'LICENSE'] if path.is_file()), None)
    if license_path is None:
        raise ValueError('The original Cellpose distribution license is missing.')
    shutil.copy2(license_path, models / 'CELLPOSE-LICENSE.txt')
    provenance = {'models': [{'id': 'nuclei' if name == 'nucleitorch_0' else name,
                              'filename': name, **spec} for name, spec in CELLPOSE_MODELS.items()],
                  'library': {'name': 'cellpose', 'version': package['version'], 'wheel': package['file'],
                              'wheel_sha256': package['sha256'], 'license_file': 'CELLPOSE-LICENSE.txt'},
                  'automatic_downloads': False}
    (models / 'MODEL_SOURCES.json').write_bytes(json_bytes(provenance))
    return {'input_receipt_sha256': sha256(receipt_path), 'models': provenance['models']}


def verify_candidate(directory: Path, *, additional_files: frozenset[str] = frozenset()) -> dict:
    files = _files(directory)
    manifest = json.loads(files[MANIFEST].read_text('utf-8'))
    expected = manifest_records(manifest, MANIFEST)
    if (manifest.get('application') != 'Figure Workbench Cellpose CPU component'
            or manifest.get('version') != VERSION or manifest.get('platform') != 'Windows x64'
            or manifest.get('python_version') != PYTHON_VERSION
            or manifest.get('python_sha256') != PYTHON_SHA256
            or manifest.get('portable_runtime_executed') is not False
            or manifest.get('release_gate_passed') is not False):
        raise ValueError('Invalid candidate identity or unverified runtime claim.')
    component_files = set(expected) | {MANIFEST, INTEGRATION}
    if (set(additional_files) & component_files or
            any(name.startswith('cellpose/') for name in additional_files)):
        raise ValueError('Additional container files may not replace or extend the isolated component.')
    if set(files) != component_files | set(additional_files):
        raise ValueError('Candidate file inventory differs from its manifest.')
    for name, record in expected.items():
        if files[name].stat().st_size != record['bytes'] or sha256(files[name]) != record['sha256']:
            raise ValueError('Candidate file was modified: ' + name)
    if files['cellpose/runtime/python312._pth'].read_bytes() != ISOLATED_PTH:
        raise ValueError('Candidate Python paths are not isolated.')
    manifest_hash = sha256(files[MANIFEST])
    integration = json.loads(files[INTEGRATION].read_text('utf-8'))
    if (integration.get('source_manifest_sha256') != manifest_hash
            or integration.get('runtime') != 'cellpose/runtime/python.exe'
            or integration.get('portable_runtime_executed') is not False):
        raise ValueError('Candidate integration metadata is not bound to the manifest.')
    validate_dependencies(manifest['dependencies'], CELLPOSE_ROOT_REQUIREMENTS)
    for name, spec in CELLPOSE_MODELS.items():
        record = expected.get('cellpose/models/' + name, {})
        if record.get('sha256') != spec['sha256'] or record.get('bytes') != spec['bytes']:
            raise ValueError('Candidate does not contain the pinned official weights.')
    audit_portable_paths(directory / 'cellpose/runtime/Lib/site-packages')
    return {'source_manifest_sha256': manifest_hash, 'integration_sha256': sha256(files[INTEGRATION]),
            'verified_files': len(component_files), 'portable_runtime_executed': False, 'release_gate_passed': False}


def _write_zip(directory: Path, destination: Path) -> dict:
    # Deliberately no outer version directory: cellpose/ is extracted beside EXE.
    files = _files(directory)
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for name, path in sorted(files.items()):
            member = zipfile.ZipInfo(name, date_time=(2026, 11, 12, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            member.external_attr = 0o100644 << 16
            with path.open('rb') as source, archive.open(member, 'w', force_zip64=True) as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
    with zipfile.ZipFile(destination) as archive:
        if set(inventory(archive)) != set(files) or archive.testzip() is not None:
            raise ValueError('Candidate ZIP integrity verification failed.')
    return {'file': destination.name, 'bytes': destination.stat().st_size, 'sha256': sha256(destination),
            'entries': len(files), 'crc_verified': True}


def build(base_main: Path, tested_environment: Path, output: Path, *, wheels: Path | None = None,
          make_zip: bool = True) -> dict:
    if tested_environment.is_symlink() or output.is_symlink():
        raise ValueError('Environment and output roots may not be symlinks.')
    output, home = output.resolve(), tested_environment.resolve()
    if output.is_relative_to(home) or home.is_relative_to(output):
        raise ValueError('Candidate and tested environment must be separate directories.')
    archive_path = output.parent / CPU_INTERMEDIATE
    if output.exists() or (make_zip and archive_path.exists()):
        raise ValueError('Candidate outputs already exist; choose new paths without overwriting.')
    environment = inspect_environment(home)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.cellpose-candidate-', dir=output.parent) as temporary:
        stage = Path(temporary)
        candidate, component = stage / 'candidate', stage / 'candidate/cellpose'
        component.mkdir(parents=True)
        foundation = copy_python_foundation(base_main, component)
        wheelhouse = wheels if wheels is not None else stage / 'wheels'
        if wheels is None:
            download_wheels(environment, wheelhouse)
        dependencies, comparison = install_tested_wheels(environment, wheelhouse, component / 'runtime')
        models = copy_models(environment, component, dependencies)
        shutil.copy2(home / 'dependencies.txt', component / 'dependencies.txt')
        (component / 'windows-packages.lock').write_text(''.join(
            f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}\n"
            for item in sorted(dependencies, key=lambda item: canonicalize_name(item['name']))), 'utf-8')
        (candidate / 'CELLPOSE_START_HERE.txt').write_text(
            f'图研 · Figure Workbench {VERSION} — 内置 Cellpose CPU 分析引擎\n\n'
            'Windows 完整工具包已包含独立运行环境和 nuclei、cyto3 官方模型。\n'
            '完整解压工具包后运行 FigureWorkbench.exe，在显微分析中选择 Cellpose。\n'
            '无需额外下载组件、安装 Python 或配置模型路径。\n\n'
            '推理在本机独立进程中运行，不自动下载权重。\n'
            '请勿把 cellpose/runtime 的文件复制进主程序的 runtime 文件夹。\n'
            '首次推理会加载模型；请在界面中查看进度，并复核候选分割结果。\n\n'
            '下载版本及实际验收记录以 GitHub 对应发行页面为准。构建清单只记录打包阶段。\n', 'utf-8')
        manifest = {'application': 'Figure Workbench Cellpose CPU component', 'version': VERSION,
                    'platform': 'Windows x64', 'created_at_utc': datetime.now(timezone.utc).isoformat(),
                    'python_version': PYTHON_VERSION, 'python_source': PYTHON_URL, 'python_sha256': PYTHON_SHA256,
                    'portable_runtime_executed': False, 'release_gate_passed': False,
                    'runtime_abi_isolated': True, 'models': {name: spec['sha256'] for name, spec in CELLPOSE_MODELS.items()},
                    'dependencies': dependencies, 'foundation': foundation, 'tested_environment_comparison': comparison,
                    'input_freeze_sha256': environment['freeze_sha256'], 'model_provenance': models,
                    'builder_sha256': sha256(Path(__file__)),
                    'worker_sha256': sha256(PROJECT_ROOT / 'workbench/cellpose_worker.py'),
                    'files': directory_records(candidate)}
        (candidate / MANIFEST).write_bytes(json_bytes(manifest))
        integration = {'component': 'Cellpose CPU', 'version': VERSION, 'source_manifest': MANIFEST,
                       'source_manifest_sha256': sha256(candidate / MANIFEST),
                       'runtime': 'cellpose/runtime/python.exe', 'worker': 'app/workbench/cellpose_worker.py',
                       'separate_python_numpy_environment': True, 'portable_runtime_executed': False,
                       'release_gate_passed': False}
        (candidate / INTEGRATION).write_bytes(json_bytes(integration))
        verified = verify_candidate(candidate)
        result = {'version': VERSION, 'output': str(output), 'cellpose_home': str(output / 'cellpose'),
                  'dependency_count': len(dependencies), 'source_freeze_sha256': environment['freeze_sha256'],
                  'worker_sha256': manifest['worker_sha256'], **verified}
        if make_zip:
            staged_zip = stage / archive_path.name
            result['archive'] = _write_zip(candidate, staged_zip)
        shutil.move(str(candidate), str(output))
        if make_zip:
            shutil.move(str(staged_zip), str(archive_path))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-main', type=Path, required=True)
    parser.add_argument('--environment', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=PROJECT_ROOT / f'dist/Figure_Workbench_{VERSION}_Cellpose_CPU_Windows_x64')
    parser.add_argument('--wheels', type=Path, help='Existing exact-version Windows wheels; omit to download official wheels.')
    parser.add_argument('--record', type=Path)
    parser.add_argument('--no-zip', action='store_true')
    args = parser.parse_args()
    result = build(args.base_main, args.environment, args.output, wheels=args.wheels, make_zip=not args.no_zip)
    if args.record:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        args.record.write_bytes(json_bytes(result))
    print(json.dumps(result, ensure_ascii=False, indent=2))
