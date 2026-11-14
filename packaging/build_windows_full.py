"""Build and verify the complete Windows toolkit, including its CPU models.

The delivered ZIP is the sole native acceptance input. Its manifest covers the
desktop runtime, application, isolated Cellpose runtime, official weights and
licenses. Static verification does not claim Windows execution has passed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from zipfile import ZipFile

from assemble_windows import PROJECT_ROOT, sha256
from build_cellpose_candidate import (MANIFEST as CPU_MANIFEST, INTEGRATION as CPU_INTEGRATION,
                                      _files, verify_candidate)
from build_desktop_launcher import pe_metadata
from upgrade_windows import canonical_path, inventory, verify_member
from upgrade_windows_0_4 import (MANIFEST, SOURCE_MANIFEST, REPORT, assert_matching_source,
                                 directory_records, json_bytes, manifest_records,
                                 read_json_member, snapshot_source, source_versions, write_zip)
from upgrade_windows_0_5 import verify_base, install_desktop, verify_launcher

sys.path.insert(0, str(PROJECT_ROOT))
from scripts.release_config import (VERSION, WINDOWS_DIRECTORY, SOURCE_DIRECTORY,
                                     FULL, SOURCE, CPU_INTERMEDIATE)
from scripts.run_acceptance import source_identity


def archive_identity(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError('A regular archive file is required.')
    return {'file': path.name, 'bytes': path.stat().st_size, 'sha256': sha256(path)}


def component_records(directory: Path) -> tuple[dict, dict]:
    manifest = json.loads((directory / CPU_MANIFEST).read_text('utf-8'))
    records = manifest_records(manifest, CPU_MANIFEST)
    return manifest, records


def verify_full_directory(directory: Path, *, expected_manifest_sha256: str | None = None) -> dict:
    """Verify the complete payload, including both independent environments."""
    files = _files(directory)
    if MANIFEST not in files:
        raise ValueError('The complete toolkit manifest is missing.')
    manifest_hash = sha256(files[MANIFEST])
    if expected_manifest_sha256 is not None and manifest_hash != expected_manifest_sha256:
        raise ValueError('Complete toolkit manifest differs from the tested build.')
    manifest = json.loads(files[MANIFEST].read_text('utf-8'))
    expected = manifest_records(manifest, MANIFEST)
    if (manifest.get('application') != 'Figure Workbench' or manifest.get('version') != VERSION
            or manifest.get('edition') != 'complete' or manifest.get('platform') != 'Windows x64'
            or manifest.get('desktop') != {'engine': 'WebView2', 'host': 'WinForms', 'external_browser_launch': False}
            or type(manifest.get('file_count')) is not int or manifest['file_count'] != len(expected)):
        raise ValueError('Invalid complete toolkit identity or inventory declaration.')
    if set(files) != set(expected) | {MANIFEST}:
        raise ValueError('Complete toolkit inventory differs from its manifest.')
    for name, record in expected.items():
        path = files[name]
        if path.stat().st_size != record['bytes'] or sha256(path) != record['sha256']:
            raise ValueError('Complete toolkit file was modified: ' + name)
    required = {'FigureWorkbench.exe', 'runtime/python.exe', 'runtime/pythonw.exe', 'runtime/python312.dll',
                'runtime/python312.zip', 'runtime/python312._pth', 'app/run.py', 'app/launch.py',
                'app/workbench/cellpose_worker.py', 'app/' + SOURCE_MANIFEST, CPU_MANIFEST, CPU_INTEGRATION,
                'cellpose/runtime/python.exe', 'cellpose/runtime/python312._pth',
                'cellpose/models/cyto3', 'cellpose/models/nucleitorch_0'}
    if not required.issubset(files):
        raise ValueError('Complete toolkit is missing application, runtime or model files.')
    if source_versions(directory / 'app') != {'python': VERSION, 'package': VERSION}:
        raise ValueError('Packaged application version differs from the complete toolkit.')
    pe_metadata(files['FigureWorkbench.exe'])
    launcher = manifest.get('native_launcher', {})
    if (launcher.get('application_version') != VERSION
            or launcher.get('sha256') != expected['FigureWorkbench.exe']['sha256']):
        raise ValueError('Complete toolkit launcher is not bound to this version.')
    source_snapshot = sha256(files['app/' + SOURCE_MANIFEST])
    if source_snapshot != manifest.get('source_snapshot_sha256'):
        raise ValueError('Complete toolkit source snapshot binding differs.')
    source_manifest = json.loads(files['app/' + SOURCE_MANIFEST].read_text('utf-8'))
    source_records = manifest_records(source_manifest, SOURCE_MANIFEST)
    if (source_manifest.get('version') != VERSION or source_manifest.get('file_count') != len(source_records)
            or {'app/' + name for name in source_records} !=
            {name for name in expected if name.startswith('app/') and name != 'app/' + SOURCE_MANIFEST}):
        raise ValueError('Complete toolkit source inventory differs from its source manifest.')
    for name, record in source_records.items():
        outer = expected['app/' + name]
        if record['bytes'] != outer['bytes'] or record['sha256'] != outer['sha256']:
            raise ValueError('Source and complete toolkit manifests disagree: ' + name)
    cpu_manifest, cpu_records = component_records(directory)
    cpu_names = set(cpu_records) | {CPU_MANIFEST, CPU_INTEGRATION}
    component = verify_candidate(directory, additional_files=frozenset(set(files) - cpu_names))
    declared = manifest.get('components', {}).get('cellpose_cpu', {})
    if (declared.get('bundled') is not True
            or declared.get('source_manifest_sha256') != component['source_manifest_sha256']
            or declared.get('integration_sha256') != component['integration_sha256']
            or declared.get('verified_files') != component['verified_files']
            or cpu_manifest.get('worker_sha256') != expected['app/workbench/cellpose_worker.py']['sha256']):
        raise ValueError('The bundled Cellpose component is not bound to this application.')
    return {'version': VERSION, 'edition': 'complete', 'manifest_sha256': manifest_hash,
            'verified_files': len(files), 'source_snapshot_sha256': source_snapshot,
            'source_identity': manifest.get('source_identity'),
            'launcher_sha256': launcher['sha256'],
            'cellpose_component_manifest_sha256': component['source_manifest_sha256'],
            'cellpose_component_verified_files': component['verified_files']}


def verify_full_archive(archive: Path, record: dict, *, destination: Path | None = None) -> dict:
    """Authenticate the final ZIP and optionally extract only its checked members."""
    expected_archives = record.get('archives', [])
    full = [item for item in expected_archives if item.get('file') == FULL]
    actual = archive_identity(archive)
    if (record.get('version') != VERSION or record.get('edition') != 'complete' or len(full) != 1
            or any(full[0].get(key) != actual[key] for key in ('file', 'bytes', 'sha256'))):
        raise ValueError('The final complete ZIP does not match its build record.')
    with ZipFile(archive) as zipped:
        files = inventory(zipped, WINDOWS_DIRECTORY + '/')
        if MANIFEST not in files:
            raise ValueError('Complete ZIP is missing its manifest.')
        manifest, manifest_bytes = read_json_member(zipped, files[MANIFEST])
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        expected = manifest_records(manifest, MANIFEST)
        if (manifest_hash != record.get('full_toolkit_manifest_sha256')
                or manifest.get('source_identity') != record.get('source_identity')
                or set(files) != set(expected) | {MANIFEST}):
            raise ValueError('Complete ZIP inventory, source or manifest binding differs.')
        if destination is not None:
            if destination.exists() or destination.is_symlink():
                raise ValueError('Extract the final ZIP into a new directory.')
            destination.mkdir(parents=True)
        for name, entry in expected.items():
            verify_member(zipped, files[name], entry, destination / name if destination is not None else None)
        if destination is not None:
            (destination / MANIFEST).write_bytes(manifest_bytes)
    return {'archive': actual, 'full_toolkit_manifest_sha256': manifest_hash,
            'verified_files': len(files), 'source_identity': record['source_identity']}


def extract_final_archive(archive: Path, build_record: Path, destination: Path, receipt: Path) -> dict:
    """Create one fresh installation from the delivered ZIP, then bind the evidence."""
    if any(path.is_symlink() for path in (archive, destination, receipt)):
        raise ValueError('Extraction inputs and output roots may not be symlinks.')
    archive, destination = archive.resolve(), destination.resolve()
    if destination.exists() or receipt.exists():
        raise ValueError('Extraction and receipt output paths must be new.')
    record = json.loads(build_record.read_text('utf-8'))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.complete-toolkit-extract-', dir=destination.parent) as temporary:
        staging = Path(temporary) / 'installation'
        staged_app = staging / WINDOWS_DIRECTORY
        verified_archive = verify_full_archive(archive, record, destination=staged_app)
        verified_directory = verify_full_directory(staged_app,
            expected_manifest_sha256=verified_archive['full_toolkit_manifest_sha256'])
        if archive_identity(archive) != verified_archive['archive']:
            raise ValueError('Complete ZIP changed during extraction.')
        os.replace(staging, destination)
    result = {'passed': True, 'extracted_from_final_archive': True,
              'application_directory': str(destination / WINDOWS_DIRECTORY),
              **verified_archive, 'verified_directory': verified_directory}
    receipt.parent.mkdir(parents=True, exist_ok=True)
    with receipt.open('xb') as output:
        output.write(json_bytes(result))
    return result


def verify_extraction_binding(app_dir: Path, archive: Path, receipt_path: Path) -> dict:
    receipt = json.loads(receipt_path.read_text('utf-8'))
    actual = archive_identity(archive)
    if (receipt.get('passed') is not True or receipt.get('extracted_from_final_archive') is not True
            or receipt.get('archive') != actual or actual['file'] != FULL
            or Path(receipt.get('application_directory', '')).resolve() != app_dir.resolve()):
        raise ValueError('Acceptance must use the fresh extraction of the final complete ZIP.')
    verified = verify_full_directory(app_dir,
        expected_manifest_sha256=receipt.get('full_toolkit_manifest_sha256'))
    if verified != receipt.get('verified_directory') or verified['source_identity'] != receipt.get('source_identity'):
        raise ValueError('The complete installation changed after verified extraction.')
    return {'archive': actual, **verified, 'receipt_sha256': sha256(receipt_path)}


START_HERE = '''图研 · Figure Workbench {version} — Windows 完整工具包

1. 完整解压本压缩包，运行 FigureWorkbench.exe。
2. 在首页选择 WB、显微与组织图像、qPCR 或组图排版。
3. 保存项目可保留原始数据、分析参数与图稿；导出用于生成最终文件。

工具包已内置主程序、专用运行环境、Cellpose CPU 分析引擎及 nuclei/cyto3 模型。
无需单独安装 Python、Node，或下载、合并模型组件。请保留 runtime/、app/、cellpose/
及随附的许可与清单文件。首次模型分析包含运行环境和模型加载时间。

关闭窗口时会先保存当前编辑，再停止本地计算服务。
工作区位于 %LOCALAPPDATA%\\FigureWorkbench；升级时保留此目录。

Windows 10/11 需要 Microsoft WebView2 Runtime。缺少运行时会显示安装提示：
https://developer.microsoft.com/microsoft-edge/webview2/

版本说明、完整验收记录与 SHA-256 校验和见对应 GitHub 发行页面。
'''


def build(base_main: Path, launcher: Path, wheels: Path, component: Path, component_record: Path,
          output: Path, *, project_root: Path = PROJECT_ROOT) -> dict:
    project_root, output, component = project_root.resolve(), output.resolve(), component.resolve()
    source_output = output.parent / SOURCE_DIRECTORY
    archives = [output.parent / FULL, output.parent / SOURCE]
    if output.name != WINDOWS_DIRECTORY:
        raise ValueError('Use the versioned complete toolkit directory name: ' + WINDOWS_DIRECTORY)
    for path in (output, source_output, *archives):
        if path.exists() or path.is_symlink():
            raise ValueError('Existing candidates are never overwritten: ' + str(path))
        canonical_path(path.name)
        if path == project_root or (path.is_relative_to(project_root) and path.relative_to(project_root).parts[0] != 'dist'):
            raise ValueError('Build outputs must be outside source folders, or inside dist/.')
    if source_versions(project_root) != {'python': VERSION, 'package': VERSION}:
        raise ValueError('Source version declarations must match the complete toolkit.')
    source_input_identity = source_identity()
    cpu_record = json.loads(component_record.read_text('utf-8'))
    cpu_verified = verify_candidate(component)
    if (cpu_record.get('version') != VERSION
            or cpu_record.get('source_manifest_sha256') != cpu_verified['source_manifest_sha256']
            or cpu_record.get('worker_sha256') != sha256(project_root / 'workbench/cellpose_worker.py')
            or cpu_record.get('archive', {}).get('file') != CPU_INTERMEDIATE):
        raise ValueError('Cellpose build record does not match the verified component and current worker.')
    cpu_archive = component.parent / CPU_INTERMEDIATE
    if archive_identity(cpu_archive) != {key: cpu_record['archive'][key] for key in ('file', 'bytes', 'sha256')}:
        raise ValueError('The component intermediate archive changed after its build.')
    launcher_record = verify_launcher(launcher, project_root, version=VERSION)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.figure-full-build-', dir=output.parent) as temporary:
        staging = Path(temporary)
        main_dir, source_dir = staging / WINDOWS_DIRECTORY, staging / SOURCE_DIRECTORY
        main_dir.mkdir()
        prior, provenance = verify_base(base_main, main_dir, project_root)
        dependencies, desktop = install_desktop(wheels, main_dir / 'runtime', prior, project_root)
        shutil.copy2(launcher, main_dir / 'FigureWorkbench.exe')
        app = main_dir / 'app'
        original_snapshot = snapshot_source(project_root, app)
        created = datetime.now(timezone.utc).isoformat()
        bundled = {'bundled': True, 'version': VERSION, **cpu_verified,
                   'sha256': cpu_record['archive']['sha256'], 'bytes': cpu_record['archive']['bytes'],
                   'intermediate_archive': CPU_INTERMEDIATE}
        report = {'version': VERSION, 'edition': 'complete', 'build': 'windows-complete-toolkit-candidate',
                  'recorded_at_utc': created, 'status': 'static_checks_only_pending_complete_toolkit_acceptance',
                  'windows_runtime_executed': False, 'release_gate_passed': False,
                  'base_release_verification': provenance, 'native_launcher': launcher_record,
                  'desktop_dependencies': desktop, 'source_input_files': len(original_snapshot),
                  'source_identity': source_input_identity, 'cellpose_component': bundled,
                  'source_release_and_windows_app_byte_identical': True,
                  'test_records': {'tests_executed_by_this_builder': False},
                  'requirements': ['Actual final ZIP extraction, both bundled CPU models and native workflows must pass.',
                                   'Requires Microsoft WebView2 Runtime.']}
        (app / REPORT).write_bytes(json_bytes(report))
        source_records = directory_records(app, exclude={SOURCE_MANIFEST})
        (app / SOURCE_MANIFEST).write_bytes(json_bytes({'application': 'Figure Workbench', 'version': VERSION,
            'created_at_utc': created, 'files': source_records, 'file_count': len(source_records),
            'manifest_self_excluded': True}))
        shutil.copytree(app, source_dir)
        source_count = assert_matching_source(app, source_dir)
        for record in directory_records(component):
            target = main_dir / record['path']
            if target.exists():
                raise ValueError('Cellpose component overlaps the desktop runtime: ' + record['path'])
            target.parent.mkdir(parents=True, exist_ok=True)
            with (component / record['path']).open('rb') as source, target.open('xb') as destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)
            if target.stat().st_size != record['bytes'] or sha256(target) != record['sha256']:
                raise ValueError('Cellpose component changed during assembly: ' + record['path'])
        if verify_candidate(component) != cpu_verified:
            raise ValueError('Cellpose component changed during complete-toolkit assembly.')
        shutil.copy2(app / 'LICENSE', main_dir / 'LICENSE')
        (main_dir / 'WINDOWS_START_HERE.txt').write_text(START_HERE.format(version=VERSION), 'utf-8')
        (main_dir / 'DESKTOP_BUILD.json').write_bytes(json_bytes(report))
        (main_dir / 'desktop-packages.lock').write_text('\n'.join(f"{x['name']}=={x['version']}" for x in dependencies) + '\n')
        records = directory_records(main_dir, exclude={MANIFEST})
        snapshot_hash = sha256(app / SOURCE_MANIFEST)
        manifest = {'application': 'Figure Workbench', 'version': VERSION, 'edition': 'complete',
                    'created_at_utc': created, 'platform': 'Windows x64', 'python': prior['python'],
                    'dependencies': dependencies, 'native_launcher': launcher_record,
                    'source_identity': source_input_identity, 'source_snapshot_sha256': snapshot_hash,
                    'desktop': {'engine': 'WebView2', 'host': 'WinForms', 'external_browser_launch': False},
                    'components': {'cellpose_cpu': bundled}, 'files': records, 'file_count': len(records),
                    'windows_runtime_execution_verified': False, 'release_gate_passed': False}
        (main_dir / MANIFEST).write_bytes(json_bytes(manifest))
        full_verified = verify_full_directory(main_dir)
        result = {'version': VERSION, 'edition': 'complete', 'output': str(output),
                  'source_output': str(source_output), 'source_files': source_count,
                  'native_launcher': launcher_record, 'source_snapshot_sha256': snapshot_hash,
                  'source_identity': source_input_identity, 'cellpose_component': bundled,
                  'full_toolkit_manifest_sha256': full_verified['manifest_sha256'],
                  'full_toolkit_files': full_verified['verified_files'],
                  'desktop_dependency_count': len(desktop['added']),
                  'windows_runtime_executed': False, 'release_gate_passed': False}
        result['archives'] = [write_zip(main_dir, staging / FULL), write_zip(source_dir, staging / SOURCE)]
        verify_full_archive(staging / FULL, result)
        if source_identity() != source_input_identity:
            raise ValueError('The source checkout changed during packaging; rebuild from a stable revision.')
        for source, destination in ((main_dir, output), (source_dir, source_output),
                                    (staging / FULL, archives[0]), (staging / SOURCE, archives[1])):
            shutil.move(str(source), str(destination))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('build')
    for name in ('base-main', 'launcher', 'desktop-wheels', 'component', 'component-record', 'output', 'record'):
        command.add_argument('--' + name, type=Path, required=True)
    command = commands.add_parser('extract')
    for name in ('archive', 'build-record', 'destination', 'receipt'):
        command.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'build':
        result = build(args.base_main, args.launcher, args.desktop_wheels, args.component,
                       args.component_record, args.output)
        args.record.parent.mkdir(parents=True, exist_ok=True)
        args.record.write_bytes(json_bytes(result))
    else:
        result = extract_final_archive(args.archive, args.build_record, args.destination, args.receipt)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
