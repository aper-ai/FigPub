"""Verify installer payload integrity and the Windows PowerShell host boundary."""
import base64
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'packaging'))
import build_windows_installer as setup
import fetch_webview2_bootstrapper as bootstrapper
from scripts import check_windows_installer as installer_smoke
from test_full_toolkit_packaging import toolkit


def test_installer_requires_the_complete_exact_source_payload(toolkit):
    verified, directory = setup.verified_payload(toolkit['directory'], toolkit['identity']['commit'])
    assert verified['manifest_sha256'] == toolkit['record']['full_toolkit_manifest_sha256']
    assert verified['source_identity'] == toolkit['identity']
    assert directory == setup.VERSION + '-' + toolkit['identity']['commit'][:12]


@pytest.mark.parametrize('commit', ['b' * 40, 'a' * 12, '../source'])
def test_installer_rejects_a_different_or_abbreviated_source_revision(toolkit, commit):
    with pytest.raises(ValueError, match='source commit|Git SHA'):
        setup.verified_payload(toolkit['directory'], commit)


@pytest.mark.parametrize('path', [
    'cellpose/models/cyto3', 'runtime/python312.dll', 'app/workbench/cellpose_worker.py',
])
def test_installer_cannot_hide_a_changed_runtime_or_model(toolkit, path):
    target = toolkit['directory'] / path
    target.write_bytes(target.read_bytes() + b'changed')
    with pytest.raises(ValueError):
        setup.verified_payload(toolkit['directory'], toolkit['identity']['commit'])


def test_a_zip_or_naked_launcher_cannot_be_passed_as_a_complete_toolkit(toolkit):
    for path in (toolkit['archive'], toolkit['directory'] / 'FigureWorkbench.exe'):
        with pytest.raises(ValueError, match='complete toolkit directory'):
            setup.verified_payload(path, toolkit['identity']['commit'])


@pytest.mark.parametrize('helper', ['authenticode', 'shortcut'])
def test_installer_helpers_use_pwsh_with_encoded_unicode_input(monkeypatch, tmp_path, helper):
    # PS7 -> Python -> Windows PowerShell inherits incompatible PS7 module paths.
    # Exercise the actual subprocess boundary so a return to powershell.exe fails.
    monkeypatch.setattr(bootstrapper, 'os', SimpleNamespace(name='nt'))
    calls = []

    def checked_output(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return '\ufeff' + json.dumps({'value': '安装成功'}, ensure_ascii=False)

    monkeypatch.setattr(subprocess, 'check_output', checked_output)
    path = tmp_path / "Microsoft 安装器's [copy].exe"
    script = "[ordered]@{value='安装成功'}|ConvertTo-Json -Compress"
    if helper == 'authenticode':
        actual = bootstrapper.windows_file_metadata(path)
    else:
        actual = installer_smoke.powershell_json(script)
    assert actual == {'value': '安装成功'}
    assert len(calls) == 1
    arguments, options = calls[0]
    assert arguments[:4] == ['pwsh.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand']
    assert len(arguments) == 5
    assert options['text'] is True and options['encoding'] == 'utf-8'
    decoded = base64.b64decode(arguments[4], validate=True).decode('utf-16le')
    assert decoded.startswith("$ErrorActionPreference='Stop';")
    assert '[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);' in decoded
    if helper == 'authenticode':
        literal = str(path.resolve()).replace("'", "''")
        assert f"Get-AuthenticodeSignature -LiteralPath '{literal}';" in decoded
        assert 'status=[string]$signature.Status;' in decoded
        assert 'subject=[string]$signature.SignerCertificate.Subject;' in decoded
    else:
        assert decoded.endswith(script)


@pytest.mark.parametrize('helper', ['authenticode', 'shortcut'])
def test_installer_helpers_propagate_powershell_failures(monkeypatch, tmp_path, helper):
    monkeypatch.setattr(bootstrapper, 'os', SimpleNamespace(name='nt'))

    def failed_output(arguments, **kwargs):
        raise subprocess.CalledProcessError(1, arguments, stderr='Module load failed')

    monkeypatch.setattr(subprocess, 'check_output', failed_output)
    with pytest.raises(subprocess.CalledProcessError):
        if helper == 'authenticode':
            bootstrapper.windows_file_metadata(tmp_path / 'setup.exe')
        else:
            installer_smoke.powershell_json("throw 'Module load failed'")
