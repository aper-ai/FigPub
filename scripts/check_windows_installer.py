"""Exercise installation, reinstall and uninstall on a disposable Windows runner.

This checks installed bytes, per-user registration, the Start menu shortcut and
project preservation. It does not execute or certify the application's native
scientific workflows or either Cellpose model.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'packaging'))
from build_windows_full import MANIFEST, verify_full_directory
from build_windows_installer import APP_ID, digest

UNINSTALL_KEY = 'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\' + APP_ID + '_is1'


def powershell_json(script: str):
    program = "$ErrorActionPreference='Stop';[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);" + script
    encoded = base64.b64encode(program.encode('utf-16le')).decode('ascii')
    return json.loads(subprocess.check_output(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
        text=True, encoding='utf-8',
    ).lstrip('\ufeff'))


def registration(hive):
    import winreg
    try:
        with winreg.OpenKey(hive, UNINSTALL_KEY, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            return {'install_location': winreg.QueryValueEx(key, 'InstallLocation')[0]}
    except FileNotFoundError:
        return None


def execute(executable: Path, arguments: list[str], timeout=900) -> int:
    result = subprocess.run([str(executable), *arguments], timeout=timeout)
    if result.returncode not in (0, 3010):
        raise RuntimeError(f'{executable.name} failed with exit code {result.returncode}.')
    return result.returncode


def check(installer: Path, build_record: Path, output: Path) -> dict:
    if os.name != 'nt' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise OSError('Installer smoke checks run only on a disposable Windows GitHub Actions runner.')
    import winreg
    if registration(winreg.HKEY_CURRENT_USER) or registration(winreg.HKEY_LOCAL_MACHINE):
        raise ValueError('An existing installation is present; this check will not change it.')
    if output.exists() or output.is_symlink():
        raise ValueError('Choose a new installer smoke receipt.')
    build = json.loads(build_record.read_text('utf-8'))
    identity = build['installer']
    if (installer.is_symlink() or installer.name != identity['file']
            or installer.stat().st_size != identity['bytes'] or digest(installer) != identity['sha256']
            or build.get('application_id') != APP_ID):
        raise ValueError('Setup bytes do not match the installer build receipt.')
    relative = PurePosixPath(build['payload_subdirectory'])
    if relative.is_absolute() or '..' in relative.parts or len(relative.parts) != 2 or relative.parts[0] != 'versions':
        raise ValueError('Installer receipt has an invalid payload location.')
    sandbox = Path(tempfile.mkdtemp(prefix='FigureWorkbench installer ', dir=os.environ['RUNNER_TEMP'])).resolve()
    install_root = sandbox / 'Application 安装目录'
    install_root.mkdir()
    payload = install_root.joinpath(*relative.parts)
    external = sandbox / 'User Projects'
    external.mkdir()
    workspace = Path(os.environ['LOCALAPPDATA']) / 'FigureWorkbench'
    made_workspace = not workspace.exists()
    workspace.mkdir(exist_ok=True)
    marker_name = 'installer-preservation-' + uuid.uuid4().hex + '.figwork'
    markers = [workspace / marker_name, external / marker_name, install_root / marker_name]
    marker = b'Synthetic installer preservation fixture; not a user project.\n' + os.urandom(32)
    for path in markers:
        with path.open('xb') as stream:
            stream.write(marker)
    result = {
        'schema_version': 1, 'source_commit': build['source_identity']['commit'],
        'installer_sha256': identity['sha256'], 'passed': False, 'scenarios': [],
        'sandbox': str(sandbox), 'windows_installer_executed': True,
        'native_application_acceptance_passed': False,
        'packaged_model_inference_executed': False,
        'upgrade_scope': 'same-product reinstall; no previous installer release exists yet',
    }

    def preserved():
        if not all(path.is_file() and path.read_bytes() == marker for path in markers):
            raise ValueError('Installation or uninstall changed a synthetic user project.')

    def verify_installation():
        actual = verify_full_directory(payload, expected_manifest_sha256=build['payload_manifest_sha256'])
        if actual['source_identity'] != build['source_identity']:
            raise ValueError('Installed payload does not match the build source.')
        current = registration(winreg.HKEY_CURRENT_USER)
        if not current or Path(current['install_location']).resolve() != install_root:
            raise ValueError('The installer was not registered for the current user at the chosen path.')
        if registration(winreg.HKEY_LOCAL_MACHINE):
            raise ValueError('A per-machine application registration was unexpectedly created.')
        shortcut = powershell_json(
            "$programs=[Environment]::GetFolderPath('Programs');"
            "$path=Join-Path $programs 'Figure Workbench.lnk';"
            "if(-not(Test-Path -LiteralPath $path)){throw 'Start menu shortcut is missing.'};"
            "$link=(New-Object -ComObject WScript.Shell).CreateShortcut($path);"
            "[ordered]@{path=$path;target=$link.TargetPath;working_directory=$link.WorkingDirectory}|ConvertTo-Json -Compress"
        )
        if Path(shortcut['target']).resolve() != payload / 'FigureWorkbench.exe':
            raise ValueError('Start menu shortcut points outside the installed payload.')
        if Path(shortcut['working_directory']).resolve() != payload:
            raise ValueError('Start menu shortcut has the wrong working directory.')
        preserved()
        return actual, Path(shortcut['path'])

    try:
        arguments = ['/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', f'/DIR={install_root}']
        result['install_exit_code'] = execute(installer.resolve(), [*arguments, f'/LOG={sandbox / "install.log"}'])
        actual, shortcut = verify_installation()
        result['scenarios'].append({'name': 'per_user_install', 'passed': True,
                                    'verified_payload_files': actual['verified_files']})
        result['reinstall_exit_code'] = execute(installer.resolve(), [*arguments, f'/LOG={sandbox / "reinstall.log"}'])
        verify_installation()
        result['scenarios'].append({'name': 'reinstall', 'passed': True})
        manifest = json.loads((payload / MANIFEST).read_text('utf-8'))
        installed_names = [item['path'] for item in manifest['files']] + [MANIFEST]
        uninstaller = install_root / 'unins000.exe'
        if uninstaller.is_symlink() or not uninstaller.is_file():
            raise ValueError('A controlled uninstaller was not created.')
        result['uninstall_exit_code'] = execute(
            uninstaller, ['/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', f'/LOG={sandbox / "uninstall.log"}'])
        for attempt in range(40):
            if (not registration(winreg.HKEY_CURRENT_USER) and not shortcut.exists()
                    and not any((payload / name).exists() for name in installed_names)):
                break
            time.sleep(0.5)
        if registration(winreg.HKEY_CURRENT_USER) or shortcut.exists():
            raise ValueError('Uninstall left the product registration or Start menu shortcut.')
        if any((payload / name).exists() for name in installed_names):
            raise ValueError('Uninstall did not remove installer-owned payload files.')
        preserved()
        result['scenarios'].append({'name': 'uninstall', 'passed': True})
        result['scenarios'].append({'name': 'project_preservation', 'passed': True,
                                    'locations': ['default_workspace', 'external_projects', 'installation_user_file']})
        result['passed'] = True
    except Exception as exc:
        result['failure'] = str(exc)
        raise
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        for name in ('install.log', 'reinstall.log', 'uninstall.log'):
            log = sandbox / name
            if log.is_file():
                shutil.copyfile(log, output.with_name(output.stem + '.' + name))
        # Remove only this check's newly created marker, never existing user data.
        own_marker = workspace / marker_name
        if own_marker.is_file() and own_marker.read_bytes() == marker:
            own_marker.unlink()
        if made_workspace:
            try:
                workspace.rmdir()
            except OSError:
                pass
        if result['passed']:
            shutil.rmtree(sandbox)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installer', type=Path, required=True)
    parser.add_argument('--build-record', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.installer, args.build_record, args.output), ensure_ascii=False, indent=2))
