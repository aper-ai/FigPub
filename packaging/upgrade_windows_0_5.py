"""Build a Windows desktop acceptance candidate from the verified 0.4 runtime.

The existing console launcher is never reused. New native shell dependencies
are verified against official-source hashes and installed beside the retained
scientific dependencies. This script does not declare Windows acceptance passed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

from packaging.utils import canonicalize_name

from assemble_windows import (PROJECT_ROOT, PYTHON_VERSION, PYTHON_URL, PYTHON_SHA256,
                              install_wheel, validate_dependencies, wheel_metadata, sha256)
from build_desktop_launcher import pe_metadata
from upgrade_windows import canonical_path, inventory, verify_member
from upgrade_windows_0_4 import (MANIFEST, SOURCE_MANIFEST, REPORT, assert_matching_source,
                                 directory_records, file_record, json_bytes, manifest_records,
                                 read_json_member, snapshot_source, source_versions, write_zip)

VERSION = '0.5.0'
BASE_VERSION = '0.4.0'
BASE_MAIN_SHA256 = '5aac14044bbd5000f4fc31754a6bd1d08f2e9809d501393af86359cf24f03fb8'


def verify_base(source: Path, destination: Path, project_root: Path):
    if source.is_symlink() or sha256(source) != BASE_MAIN_SHA256:
        raise ValueError('The input must be the verified 0.4.0 Windows main ZIP.')
    with zipfile.ZipFile(source) as archive:
        files = inventory(archive, f'Figure_Workbench_{BASE_VERSION}_Windows_x64/')
        prior, manifest_bytes = read_json_member(archive, files[MANIFEST])
        if (prior.get('application') != 'Figure Workbench' or prior.get('version') != BASE_VERSION
                or prior.get('python') != {'version': PYTHON_VERSION, 'source': PYTHON_URL, 'sha256': PYTHON_SHA256}):
            raise ValueError('Unexpected base application or Python identity.')
        expected = manifest_records(prior, MANIFEST)
        if set(files) != set(expected) | {MANIFEST}:
            raise ValueError('Base ZIP inventory differs from its manifest.')
        validate_dependencies(prior['dependencies'], (project_root / 'requirements.txt').read_text().splitlines())
        kept = []
        for name, record in expected.items():
            retain = name.startswith(('runtime/', 'licenses/')) or name == 'windows-packages.lock'
            verify_member(archive, files[name], record, destination / name if retain else None)
            if retain:
                kept.append(record)
        for required in ('runtime/python.exe', 'runtime/pythonw.exe', 'runtime/python312.dll',
                         'runtime/python312._pth', 'runtime/python312.zip'):
            if required not in {item['path'] for item in kept}:
                raise ValueError('Base runtime is incomplete: ' + required)
    return prior, {'base_version': BASE_VERSION, 'base_zip_sha256': BASE_MAIN_SHA256,
                   'base_manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
                   'verified_source_files': len(expected), 'retained_runtime_and_license_files': len(kept),
                   'old_console_launcher_reused': False}


def install_desktop(wheels: Path, runtime: Path, prior: dict, project_root: Path):
    lock_path = project_root / 'packaging/desktop-sources.lock.json'
    specification = json.loads(lock_path.read_text('utf-8'))
    installed = {canonicalize_name(item['name']): item for item in prior['dependencies']}
    added, reused = [], []
    for source in specification['sources']:
        expected = source['prepared_wheel'] if source['kind'] == 'sdist' else source
        name = canonical_path(expected['filename'])
        if '/' in name:
            raise ValueError('Wheel filenames must be single path components.')
        path = wheels / name
        if (path.is_symlink() or path.stat().st_size != expected['bytes']
                or sha256(path) != expected['sha256']):
            raise ValueError('Desktop dependency differs from the official-source lock: ' + name)
        metadata = wheel_metadata(path)
        if canonicalize_name(metadata['name']) != canonicalize_name(source['name']) or metadata['version'] != source['version']:
            raise ValueError('Desktop package identity differs from the lock: ' + name)
        prior_package = installed.get(canonicalize_name(metadata['name']))
        if prior_package:
            if prior_package['version'] != metadata['version']:
                raise ValueError('Desktop dependency would replace a scientific runtime package: ' + name)
            reused.append({'name': metadata['name'], 'version': metadata['version']})
            continue
        metadata['installed_files'] = install_wheel(path, runtime)
        metadata['source'] = source
        installed[canonicalize_name(metadata['name'])] = metadata
        added.append(metadata)
    combined = list(installed.values())
    requirements = ((project_root / 'requirements.txt').read_text() + '\n' +
                    (project_root / 'requirements-desktop.txt').read_text()).splitlines()
    validate_dependencies(combined, requirements)
    # These are actual shipped files, not an import-success claim from Linux.
    site = runtime / 'Lib/site-packages'
    required = ['webview/platforms/edgechromium.py', 'webview/platforms/winforms.py',
                'webview/lib/Microsoft.Web.WebView2.Core.dll', 'webview/lib/Microsoft.Web.WebView2.WinForms.dll',
                'webview/lib/runtimes/win-x64/native/WebView2Loader.dll', 'pythonnet/runtime/Python.Runtime.dll']
    missing = [name for name in required if not (site / name).is_file()]
    if missing:
        raise ValueError('Required native desktop components were not installed: ' + ', '.join(missing))
    return combined, {'source_lock_sha256': sha256(lock_path), 'added': added, 'reused_from_base': reused,
                      'dependency_closure_checked': True, 'native_files_checked': required,
                      'webview2_engine': 'System Evergreen Runtime, or supplied fixed runtime in webview2/'}


def verify_launcher(launcher: Path, project_root: Path, *, version: str = VERSION):
    record = json.loads(launcher.with_suffix('.build.json').read_text('utf-8'))
    if (record.get('application_version') != version or record.get('sha256') != sha256(launcher)
            or record.get('bytes') != launcher.stat().st_size
            or record.get('source_sha256') != sha256(project_root / 'packaging/windows_launcher.c')
            or record.get('icon_sha256') != sha256(project_root / 'resources/app.ico')
            or record.get('compiler_version') != '0.15.2'):
        raise ValueError('Launcher does not match the current source/icon/version build record.')
    return {**record, **pe_metadata(launcher)}


START_HERE = '''图研 · Figure Workbench 0.5.0 — Windows 桌面客户端

完整解压，双击 FigureWorkbench.exe，打开独立的图研窗口。
请保留 runtime/ 和 app/；无需自行安装 Python 或 Node。
退出时关闭图研窗口：程序会先确认保存，再停止本地计算服务。
工程和原图仍保存在 %LOCALAPPDATA%\\FigureWorkbench，升级时保留该目录。

客户端使用 Microsoft Edge WebView2 嵌入式渲染。Windows 10/11 需要已安装
WebView2 Runtime，或使用配有 webview2/ 固定运行时的包。缺少运行时会显示明确提示，
不会自动打开浏览器或退回旧版 IE。下载入口：https://developer.microsoft.com/microsoft-edge/webview2/

版本下载及实际验收记录见 GitHub 对应发行页面。构建清单只记录打包阶段；
完整验证还包括 Windows 原生窗口、导入、分析、保存、导出及关闭流程。
'''


def build(base_main: Path, launcher: Path, wheels: Path, output: Path, *,
          source_output: Path | None = None, project_root: Path = PROJECT_ROOT,
          make_zip: bool = True, cellpose_record: Path | None = None):
    project_root, output = project_root.resolve(), output.resolve()
    sys.path.insert(0, str(project_root))
    from scripts.run_acceptance import source_identity
    source_input_identity = source_identity()
    optional_component = {'version': VERSION, 'status': 'candidate_not_built'}
    if cellpose_record is not None:
        component = json.loads(cellpose_record.read_text('utf-8'))
        archive = component.get('archive', {})
        if (component.get('version') != VERSION
                or component.get('worker_sha256') != sha256(project_root / 'workbench/cellpose_worker.py')
                or archive.get('file') != f'Figure_Workbench_{VERSION}_Cellpose_CPU.zip'
                or len(component.get('source_manifest_sha256', '')) != 64
                or len(archive.get('sha256', '')) != 64):
            raise ValueError('Optional CPU candidate does not match this version and model worker.')
        optional_component = {'version': VERSION, 'archive': archive['file'], 'sha256': archive['sha256'],
                              'bytes': archive['bytes'], 'source_manifest_sha256': component['source_manifest_sha256'],
                              'status': 'separately_tested_candidate_required'}
    source_output = (source_output or output.parent / f'Figure_Workbench_{VERSION}_Source').resolve()
    if output == source_output or output.parent != source_output.parent:
        raise ValueError('Main and source outputs must be distinct sibling directories.')
    if source_versions(project_root) != {'python': VERSION, 'package': VERSION}:
        raise ValueError('Both source version declarations must be 0.5.0.')
    archives = [output.parent / f'Figure_Workbench_{VERSION}_Windows_Main.zip',
                output.parent / f'Figure_Workbench_{VERSION}_Source.zip']
    for path in [output, source_output, *(archives if make_zip else [])]:
        if path.exists():
            raise ValueError('Use new output paths; existing candidates are not overwritten: ' + str(path))
        canonical_path(path.name)
        if path == project_root or (path.is_relative_to(project_root) and path.relative_to(project_root).parts[0] != 'dist'):
            raise ValueError('Build outputs must be outside source folders, or inside dist/.')
    launcher_record = verify_launcher(launcher, project_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.figure-desktop-build-', dir=output.parent) as temporary:
        stage = Path(temporary)
        main_dir, source_dir = stage / output.name, stage / source_output.name
        main_dir.mkdir()
        prior, provenance = verify_base(base_main, main_dir, project_root)
        dependencies, desktop = install_desktop(wheels, main_dir / 'runtime', prior, project_root)
        shutil.copy2(launcher, main_dir / 'FigureWorkbench.exe')
        app = main_dir / 'app'
        original_snapshot = snapshot_source(project_root, app)
        if source_versions(app) != {'python': VERSION, 'package': VERSION}:
            raise ValueError('Source changed during the candidate snapshot.')
        created = datetime.now(timezone.utc).isoformat()
        report = {'version': VERSION, 'build': 'windows-native-desktop-candidate', 'recorded_at_utc': created,
                  'status': 'static_checks_only_pending_native_acceptance', 'windows_runtime_executed': False,
                  'release_gate_passed': False, 'base_release_verification': provenance,
                  'native_launcher': launcher_record, 'desktop_dependencies': desktop,
                  'source_input_files': len(original_snapshot),
                  'source_identity': source_input_identity,
                  'source_release_and_windows_app_byte_identical': True,
                  'native_acceptance_command': 'python tests/desktop_windows_smoke.py --app-dir PATH --output test-results/desktop',
                  'test_records': {'tests_executed_by_this_builder': False},
                  'limitations': ['Native Windows and WebView2 execution is not established by this builder.',
                                  'Requires Microsoft WebView2 Runtime or a separately supplied fixed runtime.',
                                  'Optional Cellpose retains its separate runtime and official model requirements.']}
        (app / REPORT).write_bytes(json_bytes(report))
        records = directory_records(app, exclude={SOURCE_MANIFEST})
        (app / SOURCE_MANIFEST).write_bytes(json_bytes({'application': 'Figure Workbench', 'version': VERSION,
                                                        'created_at_utc': created, 'files': records,
                                                        'file_count': len(records), 'manifest_self_excluded': True}))
        shutil.copytree(app, source_dir)
        source_count = assert_matching_source(app, source_dir)
        shutil.copy2(app / 'LICENSE', main_dir / 'LICENSE')
        (main_dir / 'WINDOWS_START_HERE.txt').write_text(START_HERE, 'utf-8')
        (main_dir / 'DESKTOP_BUILD.json').write_bytes(json_bytes(report))
        (main_dir / 'desktop-packages.lock').write_text('\n'.join(f"{x['name']}=={x['version']}" for x in dependencies) + '\n')
        manifest = {'application': 'Figure Workbench', 'version': VERSION, 'created_at_utc': created,
                    'platform': 'Windows x64', 'python': prior['python'], 'dependencies': dependencies,
                    'desktop': {'engine': 'WebView2', 'host': 'WinForms', 'external_browser_launch': False},
                    'optional_components': {'cellpose_cpu': optional_component},
                    'windows_runtime_execution_verified': False, 'release_gate_passed': False,
                    'files': directory_records(main_dir, exclude={MANIFEST})}
        (main_dir / MANIFEST).write_bytes(json_bytes(manifest))
        shutil.move(str(main_dir), str(output))
        shutil.move(str(source_dir), str(source_output))
    result = {'version': VERSION, 'output': str(output), 'source_output': str(source_output),
              'source_files': source_count, 'native_launcher': launcher_record,
              'source_snapshot_sha256': sha256(output / 'app' / SOURCE_MANIFEST),
              'source_identity': source_input_identity,
              'cellpose_component': optional_component,
              'desktop_dependency_count': len(desktop['added']), 'windows_runtime_executed': False,
              'release_gate_passed': False}
    if make_zip:
        result['archives'] = [write_zip(output, archives[0]), write_zip(source_output, archives[1])]
    if source_identity() != source_input_identity:
        raise ValueError('The source checkout changed during packaging; rebuild from a stable revision.')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-main', type=Path, required=True)
    parser.add_argument('--launcher', type=Path, required=True)
    parser.add_argument('--desktop-wheels', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-output', type=Path)
    parser.add_argument('--record', type=Path)
    parser.add_argument('--cellpose-record', type=Path)
    parser.add_argument('--no-zip', action='store_true')
    args = parser.parse_args()
    result = build(args.base_main, args.launcher, args.desktop_wheels, args.output,
                   source_output=args.source_output, make_zip=not args.no_zip, cellpose_record=args.cellpose_record)
    if args.record:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        args.record.write_bytes(json_bytes(result))
    print(json.dumps(result, ensure_ascii=False, indent=2))
