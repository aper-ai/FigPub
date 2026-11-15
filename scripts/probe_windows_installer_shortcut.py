"""Diagnose shortcut paths using the real installer template and a tiny payload.

This disposable Windows CI probe never starts the copied executable, publishes
a release, or certifies the complete scientific application. The production
installer, source verification and release gates are unchanged.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'packaging'))
from build_windows_installer import APP_ID, TEMPLATE, VERSION, digest, find_compiler
from fetch_webview2_bootstrapper import fetch
from scripts.check_windows_installer import execute, powershell_json, registration
from scripts.inspect_windows_shortcut import inspect_shortcut


def path_observation(value: str | None, expected: Path) -> dict:
    observed = {'value': value, 'repr': repr(value), 'ascii': ascii(value),
                'exists': False, 'samefile': False}
    if not isinstance(value, str):
        observed['error'] = 'No string path returned.'
        return observed
    observed.update(characters=len(value), codepoints=[ord(char) for char in value])
    if not value:
        observed['error'] = 'Empty path returned.'
        return observed
    path = Path(value)
    try:
        observed['exists'] = path.exists()
        resolved = str(path.resolve(strict=True))
        observed['strict_resolve'] = resolved
        observed['strict_resolve_repr'] = repr(resolved)
        observed['strict_resolve_ascii'] = ascii(resolved)
    except OSError as exc:
        observed['resolve_error'] = repr(exc)
    try:
        observed['samefile'] = path.samefile(expected)
    except OSError as exc:
        observed['samefile_error'] = repr(exc)
    return observed


def probe(output: Path) -> dict:
    if os.name != 'nt' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise OSError('Run this probe only on a disposable Windows GitHub Actions runner.')
    if os.environ.get('GITHUB_REPOSITORY') != 'aper-ai/figure-workbench':
        raise ValueError('This probe is restricted to the approved repository.')
    import winreg
    if registration(winreg.HKEY_CURRENT_USER) or registration(winreg.HKEY_LOCAL_MACHINE):
        raise ValueError('An existing application installation is present; leave it unchanged.')
    folders = powershell_json(
        "[ordered]@{programs=[Environment]::GetFolderPath('Programs');"
        "common_programs=[Environment]::GetFolderPath('CommonPrograms')}|ConvertTo-Json -Compress"
    )
    shortcut = Path(folders['programs']) / 'Figure Workbench.lnk'
    common_shortcut = Path(folders['common_programs']) / 'Figure Workbench.lnk'
    if shortcut.exists() or common_shortcut.exists():
        raise ValueError('An existing Start menu shortcut is present; leave it unchanged.')
    if output.exists() or output.is_symlink():
        raise ValueError('Choose a new probe evidence directory.')
    output = output.resolve()
    output.mkdir(parents=True)
    result = {'schema_version': 1, 'diagnostic_only': True, 'passed': False,
              'source_commit': os.environ['GITHUB_SHA'], 'application_id': APP_ID,
              'template_sha256': digest(TEMPLATE), 'program_folders': folders,
              'application_executed': False, 'complete_payload_acceptance': False,
              'full_installer_lifecycle_acceptance': False}
    try:
        bootstrapper = output / 'MicrosoftEdgeWebview2Setup.exe'
        result['webview2_bootstrapper'] = fetch(bootstrapper, output / 'webview-bootstrapper.json')
        compiler, result['compiler'] = find_compiler()
        with tempfile.TemporaryDirectory(prefix='FigureWorkbench installer ', dir=os.environ['RUNNER_TEMP']) as temporary:
            sandbox = Path(temporary).resolve()
            payload, compiled_output = sandbox / 'payload', sandbox / 'compiled'
            install_root = sandbox / 'Application 安装目录'
            (payload / 'app').mkdir(parents=True)
            compiled_output.mkdir()
            install_root.mkdir()
            shutil.copyfile(ROOT / 'LICENSE', payload / 'app/LICENSE')
            system_executable = Path(os.environ['SystemRoot']) / 'System32/cmd.exe'
            shutil.copyfile(system_executable, payload / 'FigureWorkbench.exe')
            result['fixture_executable'] = {'source': str(system_executable),
                                            'sha256': digest(payload / 'FigureWorkbench.exe'),
                                            'executed': False}
            build_directory = f'{VERSION}-{os.environ["GITHUB_SHA"][:12]}'
            installed_payload = install_root / 'versions' / build_directory
            expected_target = installed_payload / 'FigureWorkbench.exe'
            installer = compiled_output / 'shortcut-probe.exe'
            arguments = [str(compiler), '/Qp', f'/DPayloadDir={payload}',
                         f'/DBootstrapperPath={bootstrapper}', f'/DAppVersion={VERSION}',
                         f'/DBuildDirectory={build_directory}', f'/DOutputDirectory={compiled_output}',
                         '/DOutputName=shortcut-probe', str(TEMPLATE)]
            compiled = subprocess.run(arguments, capture_output=True, timeout=120)
            (output / 'compile.log').write_bytes(compiled.stdout + compiled.stderr)
            compiled.check_returncode()
            result['sandbox'] = str(sandbox)
            result['installer'] = {'bytes': installer.stat().st_size, 'sha256': digest(installer)}
            install_attempted = False
            try:
                install_attempted = True
                result['install_exit_code'] = execute(installer, [
                    '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-',
                    f'/DIR={install_root}', f'/LOG={output / "install.log"}',
                ], timeout=180)
                result['registration'] = {
                    'current_user': registration(winreg.HKEY_CURRENT_USER),
                    'local_machine': registration(winreg.HKEY_LOCAL_MACHINE),
                }
                current = result['registration']['current_user']
                if not current or Path(current['install_location']).resolve(strict=True) != install_root:
                    raise ValueError('Probe was not registered for the current user in the selected directory.')
                if result['registration']['local_machine'] is not None:
                    raise ValueError('Probe unexpectedly registered a machine-wide installation.')
                if digest(expected_target) != result['fixture_executable']['sha256']:
                    raise ValueError('Installed probe executable bytes differ from the copied system file.')
                result['expected'] = {
                    'target': path_observation(str(expected_target), expected_target),
                    'working_directory': path_observation(str(installed_payload), installed_payload),
                }
                result['shortcut'] = {'path': str(shortcut), 'bytes': shortcut.stat().st_size,
                                      'sha256': digest(shortcut), 'preexisting': False}
                shutil.copyfile(shortcut, output / 'installed-shortcut.lnk')
                result['readers'] = inspect_shortcut(shortcut)
                comparisons = {}
                for reader in ('wscript', 'shell_link_w'):
                    values = result['readers'][reader]
                    comparisons[reader] = {
                        'target': path_observation(values['target'], expected_target),
                        'working_directory': path_observation(values['working_directory'], installed_payload),
                    }
                    if 'raw_target' in values:
                        comparisons[reader]['raw_target'] = path_observation(values['raw_target'], expected_target)
                result['comparisons'] = comparisons
                native = result['readers']['shell_link_w']
                if not (not native.get('error')
                        and native.get('target_hresult') == 0
                        and native.get('working_directory_hresult') == 0
                        and result['readers'].get('transport_matches') is True
                        and result['readers'].get('shortcut_file', {}).get('unchanged_after_read') is True
                        and comparisons['shell_link_w']['target']['samefile'] is True
                        and comparisons['shell_link_w']['working_directory']['samefile'] is True):
                    raise ValueError('Explicit Unicode shortcut identity or read integrity checks failed.')
                result['unicode_shortcut_identity_passed'] = True
            finally:
                uninstaller = install_root / 'unins000.exe'
                if install_attempted and uninstaller.is_file():
                    result['uninstall_exit_code'] = execute(uninstaller, [
                        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', f'/LOG={output / "uninstall.log"}',
                    ], timeout=180)
                    for _ in range(40):
                        if not registration(winreg.HKEY_CURRENT_USER) and not shortcut.exists() and not expected_target.exists():
                            break
                        time.sleep(0.5)
                    result['normal_uninstall_complete'] = (not registration(winreg.HKEY_CURRENT_USER)
                                                          and not shortcut.exists() and not expected_target.exists())
                    if not result['normal_uninstall_complete']:
                        raise ValueError('Probe normal uninstall did not remove its registration, shortcut and executable.')
                elif install_attempted:
                    result['normal_uninstall_complete'] = False
            result['passed'] = result.get('unicode_shortcut_identity_passed') is True and result.get('normal_uninstall_complete') is True
    except Exception as exc:
        result['error'] = repr(exc)
        raise
    finally:
        (output / 'shortcut-probe.json').write_text(json.dumps(result, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(result, ensure_ascii=True, indent=2))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    probe(args.output)
