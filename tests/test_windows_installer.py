"""Verify installer payload integrity and the Windows PowerShell host boundary."""
import base64
import hashlib
import json
from pathlib import Path, PureWindowsPath
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


@pytest.fixture
def recorded_shortcut(monkeypatch, tmp_path):
    # Replay the actual Windows ACP 1252 COM results against local real files.
    # Only the disposable runner prefix/separators change; Unicode vs '?' stays.
    fixture = json.loads((ROOT / 'tests/fixtures/windows_shortcut_acp1252.json').read_text('utf-8'))
    readings = fixture['readers']
    assert readings['ansi_code_page'] == 1252
    for key, encoded in readings['transport_utf16le_base64'].items():
        reader, field = key.split('.', 1)
        assert base64.b64decode(encoded).decode('utf-16le') == readings[reader][field]
    prefix = PureWindowsPath(fixture['sandbox'])
    for reader in ('wscript', 'shell_link_w'):
        for field in ('target', 'working_directory', 'raw_target'):
            if readings[reader].get(field):
                relative = PureWindowsPath(readings[reader][field]).relative_to(prefix)
                readings[reader][field] = str(tmp_path.joinpath(*relative.parts))
    for key in readings['transport_utf16le_base64']:
        reader, field = key.split('.', 1)
        readings['transport_utf16le_base64'][key] = base64.b64encode(
            readings[reader][field].encode('utf-16le')).decode('ascii')
    payload = Path(readings['shell_link_w']['working_directory'])
    payload.mkdir(parents=True)
    (payload / 'FigureWorkbench.exe').write_bytes(b'Synthetic executable identity fixture')
    shortcut = tmp_path / 'Programs/Figure Workbench.lnk'
    readings['path'] = str(shortcut)
    monkeypatch.setattr(installer_smoke, 'inspect_shortcut', lambda path: readings)
    return SimpleNamespace(readings=readings, payload=payload, shortcut=shortcut, result={})


def test_unicode_shortcut_identity_survives_the_observed_wscript_ansi_loss(recorded_shortcut):
    case = recorded_shortcut
    assert 'Application ????' in case.readings['wscript']['target']
    assert not Path(case.readings['wscript']['target']).exists()
    assert 'Application 安装目录' in case.readings['shell_link_w']['target']
    for phase in ('per_user_install', 'reinstall'):
        installer_smoke.verify_shortcut(case.shortcut, case.payload, case.result, phase)
    assert [item['phase'] for item in case.result['shortcut_checks']] == ['per_user_install', 'reinstall']
    for observation in case.result['shortcut_checks']:
        assert observation['passed'] is True
        assert observation['readers'] is case.readings
        assert all(item['samefile'] is True for item in observation['native_identity'].values())


@pytest.mark.parametrize(('scope', 'field', 'bad_value'), [
    ('shell_link_w', 'target_hresult', 1),
    ('shell_link_w', 'working_directory_hresult', -2147467259),
    ('shell_link_w', 'error', 'IPersistFile.Load failed'),
    ('', 'transport_matches', False),
    ('shortcut_file', 'unchanged_after_read', False),
])
def test_shortcut_reader_failures_keep_original_diagnostics(recorded_shortcut, scope, field, bad_value):
    case = recorded_shortcut
    values = case.readings[scope] if scope else case.readings
    values[field] = bad_value
    with pytest.raises(ValueError):
        installer_smoke.verify_shortcut(case.shortcut, case.payload, case.result, 'per_user_install')
    observation = case.result['shortcut_checks'][0]
    assert observation['passed'] is False
    assert observation['readers'] is case.readings
    assert 'Application ????' in observation['readers']['wscript']['target']
    assert 'Application 安装目录' in observation['readers']['shell_link_w']['target']


@pytest.mark.parametrize('replacement', ['missing', 'different_file', 'outside_hardlink', 'wrong_directory'])
def test_shortcut_requires_the_existing_canonical_payload_and_file_identity(recorded_shortcut, replacement):
    case = recorded_shortcut
    native = case.readings['shell_link_w']
    target = case.payload / 'FigureWorkbench.exe'
    # A correct legacy reading must never excuse a wrong native target.
    case.readings['wscript']['target'] = str(target)
    if replacement == 'missing':
        target.unlink()
    elif replacement in ('different_file', 'outside_hardlink'):
        other = case.payload.parent / 'Outside.exe'
        if replacement == 'outside_hardlink':
            other.hardlink_to(target)
            assert other.samefile(target)
        else:
            other.write_bytes(b'Different file')
        native['target'] = str(other)
    else:
        native['working_directory'] = str(case.payload.parent)
    with pytest.raises(ValueError, match='not an existing installed path|does not identify the installed payload'):
        installer_smoke.verify_shortcut(case.shortcut, case.payload, case.result, 'per_user_install')
    observation = case.result['shortcut_checks'][0]
    assert observation['passed'] is False
    assert observation['readers']['shell_link_w'] == native


@pytest.fixture
def compiler_probe(monkeypatch, tmp_path):
    machine = tmp_path / 'Program Files (x86)'
    user = tmp_path / 'LocalAppData'
    locations = {
        'machine': machine / 'Inno Setup 6/ISCC.exe',
        'user': user / 'Programs/Inno Setup 6/ISCC.exe',
        'path': tmp_path / 'PathTools/ISCC.exe',
    }
    for name, path in locations.items():
        path.parent.mkdir(parents=True)
        path.write_bytes(b'MZ compiler fixture ' + name.encode())
        path.with_name('ISCmplr.dll').write_bytes(b'MZ engine fixture ' + name.encode())
    monkeypatch.setenv('ProgramFiles(x86)', str(machine))
    monkeypatch.setenv('LOCALAPPDATA', str(user))
    monkeypatch.setattr(setup.shutil, 'which', lambda name: str(locations['path']))
    monkeypatch.setattr(setup, 'windows_file_metadata', lambda path: {
        'file_version': '0.0.0.0', 'product_version': '0.0.0.0', 'status': 'Valid',
    })
    state = {'locations': locations, 'calls': [], 'returncode': 0,
             'stdout': b'Compiler engine version: Inno Setup 6.7.1\r\n', 'emit_output': False}

    def compile_probe(arguments, **kwargs):
        state['calls'].append((arguments, kwargs))
        assert arguments[1:] == ['/O-', '-']
        assert not kwargs.get('shell')
        script = kwargs['input'].decode('utf-8')
        assert 'Output=no\n' in script
        output = Path(next(line.removeprefix('OutputDir=') for line in script.splitlines()
                           if line.startswith('OutputDir=')))
        temporary = Path(kwargs['cwd'])
        assert output.is_dir() and output.is_relative_to(temporary)
        assert not temporary.is_relative_to(ROOT)
        if state['emit_output']:
            (output / 'unexpected.exe').write_bytes(b'Unexpected output')
        return subprocess.CompletedProcess(arguments, state['returncode'], state['stdout'], b'')

    monkeypatch.setattr(setup.subprocess, 'run', compile_probe)
    return state


@pytest.mark.parametrize('preferred', ['machine', 'user', 'path'])
def test_compiler_prefers_real_installs_and_records_the_loaded_engine(compiler_probe, preferred):
    locations = compiler_probe['locations']
    for name in list(locations)[:list(locations).index(preferred)]:
        locations[name].unlink()
    compiler, identity = setup.find_compiler()
    assert compiler == locations[preferred].resolve()
    assert identity['file'] == str(compiler)
    assert identity['version'] == '6.7.1'
    assert identity['sha256'] == hashlib.sha256(compiler.read_bytes()).hexdigest()
    assert identity['pe_metadata']['file_version'] == '0.0.0.0'
    engine = compiler.with_name('ISCmplr.dll')
    assert identity['engine']['file'] == str(engine)
    assert identity['engine']['sha256'] == hashlib.sha256(engine.read_bytes()).hexdigest()
    assert identity['engine']['pe_metadata']['file_version'] == '0.0.0.0'
    assert identity['version_probe']['returncode'] == 0
    assert identity['version_probe']['banner'] == 'Compiler engine version: Inno Setup 6.7.1'
    assert compiler_probe['calls'][0][0][0] == str(compiler)


def test_explicit_compiler_has_priority_and_never_silently_falls_back(compiler_probe):
    explicit = compiler_probe['locations']['path']
    assert setup.find_compiler(explicit)[0] == explicit.resolve()
    explicit.with_name('ISCmplr.dll').unlink()
    with pytest.raises(ValueError, match='real Inno Setup compiler'):
        setup.find_compiler(explicit)
    assert len(compiler_probe['calls']) == 1


def test_an_isolated_path_shim_is_not_executed_or_recorded_as_the_compiler(compiler_probe):
    locations = compiler_probe['locations']
    locations['machine'].unlink()
    locations['user'].unlink()
    locations['path'].with_name('ISCmplr.dll').unlink()
    with pytest.raises(ValueError, match='real Inno Setup compiler'):
        setup.find_compiler()
    assert compiler_probe['calls'] == []


@pytest.mark.parametrize(('stdout', 'returncode'), [
    (b'Compiler engine version: Inno Setup 6.7.2\n', 0),
    (b'Compiler engine version: Inno Setup 6.7.1-beta\n', 0),
    (b'Inno Setup 6 Command-Line Compiler\n', 0),
    (b'Compiler engine version: Inno Setup 6.7.1\n' * 2, 0),
    (b'Compiler engine version: Inno Setup 6.7.1\n', 1),
])
def test_compiler_requires_one_exact_engine_banner_and_success(compiler_probe, stdout, returncode):
    compiler_probe.update(stdout=stdout, returncode=returncode)
    with pytest.raises(ValueError, match='6.7.1 compiler engine'):
        setup.find_compiler()


def test_compiler_version_probe_rejects_unexpected_installer_output(compiler_probe):
    compiler_probe['emit_output'] = True
    with pytest.raises(ValueError, match='unexpectedly emitted installer files'):
        setup.find_compiler()
