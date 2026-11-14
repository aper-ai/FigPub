"""Exercise complete assembly and final-ZIP acceptance provenance with tiny inputs.

Synthetic PE headers and weights exercise packaging without claiming Windows or
model execution. The Windows release gate separately requires actual inference,
browser workflows and native operations using this exact extraction procedure.
"""
import copy
import json
from pathlib import Path
import struct
import sys
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'packaging'))
import build_windows_full as full
import upgrade_windows_0_5 as foundation
from test_cellpose_candidate_packaging import builder as cpu, fixture as cpu_inputs, pe_header, wheel


def json_file(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(full.json_bytes(value))


@pytest.fixture
def toolkit(tmp_path, monkeypatch):
    home, model_wheels, base, component = cpu_inputs(tmp_path, monkeypatch)
    # Use the real trusted-foundation verifier with a pinned synthetic archive.
    with ZipFile(base) as zipped:
        members = {name: zipped.read(name) for name in zipped.namelist()}
    manifest_name = 'Figure_Workbench_0.4.0_Windows_x64/' + full.MANIFEST
    prior = json.loads(members[manifest_name])
    prior['dependencies'] = []
    members[manifest_name] = full.json_bytes(prior)
    with ZipFile(base, 'w', compression=ZIP_DEFLATED) as zipped:
        for name, content in members.items():
            zipped.writestr(name, content)
    monkeypatch.setattr(cpu, 'BASE_MAIN_SHA256', full.sha256(base))
    monkeypatch.setattr(foundation, 'BASE_MAIN_SHA256', full.sha256(base))
    component_record = cpu.build(base, home, component, wheels=model_wheels)
    component_record_path = tmp_path / 'component-build.json'
    json_file(component_record_path, component_record)

    project = tmp_path / 'source'
    sources = {'run.py': b'# synthetic run entry\n', 'launch.py': b'# synthetic desktop entry\n',
        'LICENSE': b'Synthetic application license fixture\n', 'requirements.txt': b'',
        'requirements-desktop.txt': b'synthetic_desktop==1.0\n',
        'workbench/__init__.py': f'__version__ = "{full.VERSION}"\n'.encode(),
        'workbench/cellpose_worker.py': (ROOT / 'workbench/cellpose_worker.py').read_bytes(),
        'packaging/windows_launcher.c': b'/* synthetic launcher source */\n',
        'resources/app.ico': b'synthetic icon bytes',
        'package.json': full.json_bytes({'version': full.VERSION})}
    for name, content in sources.items():
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    desktop_wheels = tmp_path / 'desktop-wheels'
    desktop_wheels.mkdir()
    required_desktop_files = ('webview/platforms/edgechromium.py', 'webview/platforms/winforms.py',
        'webview/lib/Microsoft.Web.WebView2.Core.dll', 'webview/lib/Microsoft.Web.WebView2.WinForms.dll',
        'webview/lib/runtimes/win-x64/native/WebView2Loader.dll', 'pythonnet/runtime/Python.Runtime.dll')
    dependency = wheel(desktop_wheels, 'synthetic_desktop', '1.0',
        {name: b'synthetic native dependency bytes' for name in required_desktop_files})
    json_file(project / 'packaging/desktop-sources.lock.json', {'sources': [{
        'name': 'synthetic_desktop', 'version': '1.0', 'kind': 'wheel', 'filename': dependency.name,
        'bytes': dependency.stat().st_size, 'sha256': full.sha256(dependency)}]})
    launcher = tmp_path / 'FigureWorkbench.exe'
    executable = bytearray(pe_header())
    struct.pack_into('<H', executable, 156, 2)
    launcher.write_bytes(executable)
    json_file(launcher.with_suffix('.build.json'), {'application_version': full.VERSION,
        'sha256': full.sha256(launcher), 'bytes': len(executable), 'compiler_version': '0.15.2',
        'source_sha256': full.sha256(project / 'packaging/windows_launcher.c'),
        'icon_sha256': full.sha256(project / 'resources/app.ico')})
    identity = {'commit': 'a' * 40, 'tracked_files_sha256': 'b' * 64, 'clean': True}
    monkeypatch.setattr(full, 'source_identity', lambda: identity)
    directory = tmp_path / 'release' / full.WINDOWS_DIRECTORY
    record = full.build(base, launcher, desktop_wheels, component, component_record_path, directory,
        project_root=project)
    record_path = tmp_path / 'windows-build.json'
    json_file(record_path, record)
    return {'directory': directory, 'archive': directory.parent / full.FULL, 'record': record,
        'record_path': record_path, 'component': component, 'component_record': component_record,
        'source': project, 'destination': tmp_path / '完整工具包 验收',
        'receipt': tmp_path / 'full-toolkit-extraction.json', 'identity': identity}


def extract(toolkit):
    return full.extract_final_archive(toolkit['archive'], toolkit['record_path'],
        toolkit['destination'], toolkit['receipt'])


def replace_manifest(directory, mutate):
    path = directory / full.MANIFEST
    value = json.loads(path.read_text('utf-8'))
    mutate(value)
    json_file(path, value)


def refresh_file_record(value, directory, name):
    value['files'] = [item for item in value['files'] if item['path'] != name]
    path = directory / name
    value['files'].append({'path': name, 'bytes': path.stat().st_size, 'sha256': full.sha256(path)})
    value['file_count'] = len(value['files'])


def test_complete_assembly_preserves_isolation_and_acceptance_extracts_the_delivered_bytes(toolkit):
    directory, record = toolkit['directory'], toolkit['record']
    assert record['edition'] == 'complete' and record['cellpose_component']['bundled'] is True
    assert record['release_gate_passed'] is False and record['windows_runtime_executed'] is False
    assert {item['file'] for item in record['archives']} == {full.FULL, full.SOURCE}
    assert (directory / 'runtime/Lib/site-packages/numpy/__init__.py').read_bytes() == b'MUST NOT COPY MAIN NUMPY ABI'
    assert (directory / 'cellpose/runtime/Lib/site-packages/numpy/__init__.py').read_bytes() == b'# synthetic numpy entry\n'
    assert b'../app' not in (directory / 'cellpose/runtime/python312._pth').read_bytes()
    for model in ('cyto3', 'nucleitorch_0'):
        assert (directory / 'cellpose/models' / model).read_bytes() == (toolkit['component'] / 'cellpose/models' / model).read_bytes()
    source = directory.parent / full.SOURCE_DIRECTORY
    assert full.assert_matching_source(directory / 'app', source) == record['source_files']
    receipt = extract(toolkit)
    installed = toolkit['destination'] / full.WINDOWS_DIRECTORY
    assert installed != directory and full.directory_records(installed) == full.directory_records(directory)
    binding = full.verify_extraction_binding(installed, toolkit['archive'], toolkit['receipt'])
    assert binding['archive'] == full.archive_identity(toolkit['archive'])
    assert receipt['extracted_from_final_archive'] is True
    assert binding['manifest_sha256'] == record['full_toolkit_manifest_sha256']
    assert binding['cellpose_component_manifest_sha256'] == toolkit['component_record']['source_manifest_sha256']


@pytest.mark.parametrize('name', ['FigureWorkbench.exe', 'runtime/python312.dll',
    'runtime/Lib/site-packages/webview/lib/Microsoft.Web.WebView2.Core.dll',
    'cellpose/models/cyto3', 'cellpose/models/nucleitorch_0',
    'cellpose/runtime/Lib/site-packages/torch/__init__.py', 'app/workbench/cellpose_worker.py'])
def test_changed_native_model_library_or_source_file_invalidates_the_complete_installation(toolkit, name):
    extract(toolkit)
    installed = toolkit['destination'] / full.WINDOWS_DIRECTORY
    with (installed / name).open('ab') as target:
        target.write(b'changed after extraction')
    with pytest.raises(ValueError, match='Complete toolkit file was modified'):
        full.verify_extraction_binding(installed, toolkit['archive'], toolkit['receipt'])


@pytest.mark.parametrize('name', ['cellpose/models/cyto3', 'runtime/pythonw.exe', 'app/launch.py'])
def test_missing_required_component_cannot_be_reported_as_a_complete_toolkit(toolkit, name):
    (toolkit['directory'] / name).unlink()
    with pytest.raises(ValueError, match='inventory differs'):
        full.verify_full_directory(toolkit['directory'])


def test_outer_manifest_cannot_authorize_undeclared_files_in_the_isolated_runtime(toolkit):
    directory, name = toolkit['directory'], 'cellpose/runtime/foreign.py'
    (directory / name).write_text('# undeclared model dependency\n')
    replace_manifest(directory, lambda value: refresh_file_record(value, directory, name))
    with pytest.raises(ValueError, match='may not replace or extend'):
        full.verify_full_directory(directory)


def test_outer_manifest_cannot_replace_source_snapshot_or_inner_model_integrity(toolkit):
    directory, name = toolkit['directory'], 'app/run.py'
    (directory / name).write_text('# changed source\n')
    replace_manifest(directory, lambda value: refresh_file_record(value, directory, name))
    with pytest.raises(ValueError, match='Source and complete toolkit manifests disagree'):
        full.verify_full_directory(directory)
    (directory / name).write_bytes(b'# synthetic run entry\n')
    replace_manifest(directory, lambda value: refresh_file_record(value, directory, name))
    name = 'cellpose/models/cyto3'
    (directory / name).write_bytes(b'wrong model')
    replace_manifest(directory, lambda value: refresh_file_record(value, directory, name))
    with pytest.raises(ValueError, match='Candidate file was modified'):
        full.verify_full_directory(directory)


def test_matching_assembled_directory_cannot_substitute_for_final_zip_extraction(toolkit):
    extract(toolkit)
    with pytest.raises(ValueError, match='fresh extraction'):
        full.verify_extraction_binding(toolkit['directory'], toolkit['archive'], toolkit['receipt'])


@pytest.mark.parametrize('target', ['archive', 'receipt', 'installed_manifest'])
def test_modified_archive_receipt_or_manifest_cannot_reuse_extraction_evidence(toolkit, target):
    extract(toolkit)
    installed = toolkit['destination'] / full.WINDOWS_DIRECTORY
    if target == 'archive':
        with toolkit['archive'].open('ab') as stream:
            stream.write(b'changed after extraction')
    elif target == 'receipt':
        receipt = json.loads(toolkit['receipt'].read_text('utf-8'))
        receipt['verified_directory']['source_identity']['commit'] = 'c' * 40
        json_file(toolkit['receipt'], receipt)
    else:
        replace_manifest(installed, lambda value: value.update(created_at_utc='changed after extraction'))
    with pytest.raises(ValueError, match='fresh extraction|changed after verified|differs from the tested'):
        full.verify_extraction_binding(installed, toolkit['archive'], toolkit['receipt'])


@pytest.mark.parametrize('name', ['../escape.txt', 'wrong-root/file.txt',
    full.WINDOWS_DIRECTORY + '/../../escape.txt', full.WINDOWS_DIRECTORY + '/runtime/PYTHON.exe'])
def test_authenticated_zip_still_rejects_unsafe_roots_traversal_and_windows_collisions(toolkit, name):
    with ZipFile(toolkit['archive'], 'a') as zipped:
        zipped.writestr(name, b'hostile member')
    record = copy.deepcopy(toolkit['record'])
    record['archives'][0] = full.archive_identity(toolkit['archive'])
    json_file(toolkit['record_path'], record)
    with pytest.raises(ValueError, match='outside the declared root|Unsafe path|collision'):
        extract(toolkit)
    assert not toolkit['destination'].exists() and not toolkit['receipt'].exists()


def test_complete_zip_rejects_linked_members_without_following_them(toolkit):
    link = ZipInfo(full.WINDOWS_DIRECTORY + '/runtime/linked.py')
    link.create_system = 3
    link.external_attr = 0o120777 << 16
    with ZipFile(toolkit['archive'], 'a') as zipped:
        zipped.writestr(link, '/outside')
    record = copy.deepcopy(toolkit['record'])
    record['archives'][0] = full.archive_identity(toolkit['archive'])
    json_file(toolkit['record_path'], record)
    with pytest.raises(ValueError, match='symlink'):
        extract(toolkit)
    assert not toolkit['destination'].exists()


@pytest.mark.parametrize('target', ['destination', 'receipt'])
def test_previous_installations_and_receipts_are_not_overwritten(toolkit, target):
    if target == 'destination':
        toolkit[target].mkdir()
    else:
        toolkit[target].write_bytes(b'previous receipt')
    with pytest.raises(ValueError, match='must be new'):
        extract(toolkit)
    assert not (toolkit['destination'] / full.WINDOWS_DIRECTORY).exists()


def test_unrecorded_zip_modification_fails_before_creating_an_installation(toolkit):
    with toolkit['archive'].open('ab') as stream:
        stream.write(b'changed after build')
    with pytest.raises(ValueError, match='does not match its build record'):
        extract(toolkit)
    assert not toolkit['destination'].exists() and not toolkit['receipt'].exists()
