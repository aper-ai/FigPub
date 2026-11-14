"""0.4 release provenance, source parity, and archive boundaries; no Windows execution."""
from io import BytesIO
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys
import zipfile

import pytest

PACKAGING = Path(__file__).resolve().parents[1] / 'packaging'
sys.path.insert(0, str(PACKAGING))
spec = importlib.util.spec_from_file_location('upgrade_windows_0_4', PACKAGING / 'upgrade_windows_0_4.py')
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


def digest(content):
    return hashlib.sha256(content).hexdigest()


def records(files):
    return [{'path': name, 'bytes': len(data), 'sha256': digest(data)} for name, data in sorted(files.items())]


def pe_bytes(machine=0x8664, magic=0x20b):
    content = bytearray(90)
    content[:2] = b'MZ'
    struct.pack_into('<I', content, 0x3c, 64)
    content[64:68] = b'PE\0\0'
    struct.pack_into('<H', content, 68, machine)
    struct.pack_into('<H', content, 88, magic)
    return bytes(content)


def zip_files(path, files, prefix=''):
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            archive.writestr(prefix + name, content)


@pytest.fixture
def release_fixture(tmp_path, monkeypatch):
    source = tmp_path / 'project'
    source.mkdir()
    inputs = {
        'workbench/__init__.py': b'__version__ = "0.4.0"\n',
        'package.json': b'{"name":"figure-workbench-development","version":"0.4.0"}\n',
        'requirements.txt': b'', 'LICENSE': b'Application license\n',
        'web/browser-studio.html': b'<html>Browser source</html>',
        'web/browser-studio.js': b'export const version = "0.4.0";',
        'web/browser/studio.js': b'export const browser = true;',
        'web/modules/layout-engine.js': b'export const layout = true;',
        'scripts/build_browser_studio.mjs': b'// Browser build source\n',
        'docs/WINDOWS_0_4_BUILD.md': b'# Windows 0.4.0 static build\n',
        'packaging/windows_validation.json': b'{"version":"0.3.0","historical":true}\n',
    }
    for name, content in inputs.items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    models = {
        'cyto3': {'sha256': digest(b'cyto model'), 'bytes': len(b'cyto model'), 'source_url': 'https://www.cellpose.org/models/cyto3'},
        'nucleitorch_0': {'sha256': digest(b'nuclei model'), 'bytes': len(b'nuclei model'), 'source_url': 'https://www.cellpose.org/models/nucleitorch_0'},
    }
    monkeypatch.setattr(upgrade, 'CELLPOSE_MODELS', models)

    def make(*, extra_component=None, integration_patch=None, corrupt_main=False):
        cpu_content = {
            'cellpose/models/cyto3': b'cyto model', 'cellpose/models/nucleitorch_0': b'nuclei model',
            'cellpose/runtime/python.exe': pe_bytes(), 'Cellpose_CPU_使用说明.txt': b'Unchanged optional component',
        }
        cpu_manifest = {
            'application': 'Figure Workbench Cellpose CPU component', 'version': '0.2.0',
            'platform': 'Windows x64', 'python_version': upgrade.PYTHON_VERSION,
            'python_source': upgrade.PYTHON_URL, 'python_sha256': upgrade.PYTHON_SHA256,
            'models': {name: record['sha256'] for name, record in models.items()},
            'dependencies': [{'name': name, 'version': version, 'requires_dist': []}
                             for name, version in [('cellpose', '3.1.1.3'), ('torch', '2.7.1+cpu'), ('numpy', '2.0.2')]],
            'files': records(cpu_content),
        }
        cpu_manifest_bytes = upgrade.json_bytes(cpu_manifest)
        integration = {
            'component': 'Cellpose CPU', 'version': '0.2.0',
            'source_manifest': upgrade.COMPONENT_MANIFEST,
            'source_manifest_sha256': digest(cpu_manifest_bytes),
            'source_files_modified': False, 'separate_python_numpy_environment': True,
        }
        integration.update(integration_patch or {})
        cpu_content[upgrade.COMPONENT_MANIFEST] = cpu_manifest_bytes
        cpu_content[upgrade.COMPONENT_INTEGRATION] = upgrade.json_bytes(integration)
        cpu_content.update(extra_component or {})
        cpu_zip = tmp_path / 'component.zip'
        zip_files(cpu_zip, cpu_content)
        monkeypatch.setattr(upgrade, 'COMPONENT_SHA256', upgrade.sha256(cpu_zip))
        main_content = {
            'FigureWorkbench.exe': pe_bytes(), 'runtime/python.exe': pe_bytes(),
            'runtime/python312.dll': pe_bytes(), 'runtime/python312.zip': b'stdlib',
            'runtime/python312._pth': b'python312.zip\n.\nLib/site-packages\nimport site\n',
            'windows-packages.lock': b'', 'licenses/runtime-notice.txt': b'Runtime license',
            'app/old-version.txt': b'0.3.0 app must not be retained',
        }
        main_manifest = {
            'application': 'Figure Workbench', 'version': '0.3.0',
            'python': {'version': upgrade.PYTHON_VERSION, 'source': upgrade.PYTHON_URL, 'sha256': upgrade.PYTHON_SHA256},
            'microsoft_runtime': [], 'dependencies': [],
            'optional_components': {'cellpose': {'archive_sha256': upgrade.COMPONENT_SHA256,
                                                'manifest_sha256': digest(cpu_manifest_bytes)}},
            'files': records(main_content),
        }
        main_content[upgrade.MANIFEST] = upgrade.json_bytes(main_manifest)
        if corrupt_main:
            main_content['runtime/python312.zip'] = b'STDLIB'
        main_zip = tmp_path / 'base-main.zip'
        zip_files(main_zip, main_content, 'Figure_Workbench_0.3.0_Windows_x64/')
        monkeypatch.setattr(upgrade, 'BASE_MAIN_SHA256', upgrade.sha256(main_zip))
        return source, main_zip, cpu_zip

    return make


def test_real_release_inputs_are_pinned_to_the_recovered_archives():
    assert upgrade.VERSION == '0.4.0'
    assert upgrade.BASE_VERSION == '0.3.0'
    assert upgrade.BASE_MAIN_SHA256 == 'd1d9e2f871054b2a563e789f149f68a0a55b60d9ce678db926ce3eeffa36861a'
    assert upgrade.COMPONENT_SHA256 == 'c758c1c8c80fe3d37b59980b5928cf2e50e1b144c83845f9c87eb1a2fff71b11'


def test_source_selection_keeps_browser_sources_and_prunes_nested_build_cache_trees(release_fixture):
    root, _, _ = release_fixture()
    excluded = ['dist/browser-studio/Figure.html', 'web/dist/compiled.html', 'web/node_modules/a/code.js',
                'workbench/__pycache__/compiled.pyc', '.venv/Lib/code.py', '.git/objects/file',
                'scripts/.cache/file.js', 'tests/test-results/large.png']
    for name in excluded:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'not source')
    selected = {path.relative_to(root).as_posix() for path in upgrade.source_paths(root)}
    assert not selected.intersection(excluded)
    assert {'web/browser-studio.html', 'web/browser/studio.js', 'scripts/build_browser_studio.mjs'} <= selected


def test_full_tiny_release_keeps_source_archive_and_windows_app_identical(release_fixture, tmp_path):
    root, main, cpu = release_fixture()
    historical_report = (root / upgrade.REPORT).read_bytes()
    output = tmp_path / 'release/Figure_Workbench_0.4.0_Windows_x64'
    result = upgrade.build(main, cpu, output, project_root=root)
    source = Path(result['source_directory'])
    assert upgrade.assert_matching_source(output / 'app', source) == result['source_files']
    assert (root / upgrade.REPORT).read_bytes() == historical_report
    assert not (root / upgrade.SOURCE_MANIFEST).exists()
    assert not (output / 'app/old-version.txt').exists()
    assert not (output / 'cellpose').exists()
    assert (output / 'FigureWorkbench.exe').read_bytes() == pe_bytes()
    manifest = json.loads((output / upgrade.MANIFEST).read_bytes())
    assert manifest['version'] == '0.4.0'
    assert manifest['windows_runtime_execution_verified'] is False
    assert manifest['application_source']['byte_identical'] is True
    optional = manifest['optional_components']['cellpose']
    assert optional['component_version'] == '0.2.0' and optional['files_modified'] is False
    assert optional['archive_sha256'] == upgrade.sha256(cpu)
    assert optional['bundled_in_main_archive'] is False
    assert '0.4.0' in optional['compatible_application_versions']
    for record in manifest['files']:
        assert upgrade.file_record(output / record['path'], record['path']) == record
    report = json.loads((output / 'app' / upgrade.REPORT).read_bytes())
    assert report['windows_runtime_executed'] is False
    assert report['test_records']['tests_executed_by_this_builder'] is False
    assert report['version'] == '0.4.0'
    with zipfile.ZipFile(output.parent / result['archive']['file']) as main_zip, \
            zipfile.ZipFile(output.parent / result['source_archive']['file']) as source_zip:
        app_prefix, source_prefix = output.name + '/app/', source.name + '/'
        app_files = {name[len(app_prefix):]: main_zip.read(name) for name in main_zip.namelist() if name.startswith(app_prefix)}
        source_files = {name[len(source_prefix):]: source_zip.read(name) for name in source_zip.namelist()}
        assert app_files == source_files
        assert upgrade.SOURCE_MANIFEST in app_files
    with pytest.raises(ValueError, match='never overwritten'):
        upgrade.build(main, cpu, output, project_root=root)


def test_known_component_integration_sidecar_is_authenticated_not_ignored(release_fixture):
    root, main, cpu = release_fixture(integration_patch={'source_manifest_sha256': '0' * 64})
    with pytest.raises(ValueError, match='integration sidecar'):
        upgrade.verify_release_inputs(main, cpu, root)


def test_component_extra_files_cannot_hide_behind_the_known_sidecar_exception(release_fixture):
    root, main, cpu = release_fixture(extra_component={'cellpose/unrecorded.py': b'unknown'})
    with pytest.raises(ValueError, match='inventory differs'):
        upgrade.verify_release_inputs(main, cpu, root)


def test_each_prior_file_is_rehashed_even_after_whole_archive_authentication(release_fixture):
    root, main, cpu = release_fixture(corrupt_main=True)
    with pytest.raises(ValueError, match='digest mismatch'):
        upgrade.verify_release_inputs(main, cpu, root)


def test_incorrect_fixed_hash_leaves_no_release_artifacts(release_fixture, tmp_path, monkeypatch):
    root, main, cpu = release_fixture()
    monkeypatch.setattr(upgrade, 'BASE_MAIN_SHA256', '0' * 64)
    output = tmp_path / 'release/Main'
    with pytest.raises(ValueError, match='Fixed release ZIP'):
        upgrade.build(main, cpu, output, project_root=root)
    assert not output.exists()
    assert not list(output.parent.glob('*.zip'))
    assert not list(output.parent.glob('.figure-workbench-*'))


def test_dry_run_reports_version_readiness_without_generating_release_files(release_fixture, tmp_path):
    root, main, cpu = release_fixture()
    (root / 'workbench/__init__.py').write_text('__version__ = "0.3.0"\n')
    output = tmp_path / 'dry-output/Main'
    result = upgrade.build(main, cpu, output, project_root=root, dry_run=True)
    assert result['output_written'] is False
    assert result['source_versions_ready'] is False
    assert result['windows_runtime_executed'] is False
    assert not output.parent.exists()
    with pytest.raises(ValueError, match='Source version must be 0.4.0'):
        upgrade.build(main, cpu, output, project_root=root)


def test_source_parity_rejects_missing_extra_or_changed_files(tmp_path):
    app, source = tmp_path / 'app', tmp_path / 'source'
    app.mkdir()
    source.mkdir()
    (app / 'file.txt').write_bytes(b'one')
    (source / 'file.txt').write_bytes(b'two')
    with pytest.raises(ValueError, match='not byte-identical'):
        upgrade.assert_matching_source(app, source)
    (source / 'file.txt').write_bytes(b'one')
    (source / 'extra.txt').write_bytes(b'extra')
    with pytest.raises(ValueError, match='not byte-identical'):
        upgrade.assert_matching_source(app, source)


@pytest.mark.parametrize('machine,magic', [(0x14c, 0x10b), (0x8664, 0x20b)])
def test_pe_headers_are_inspected_without_executing_any_binary(machine, magic):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('runtime/python.exe', pe_bytes(machine, magic))
    with zipfile.ZipFile(buffer) as archive:
        identity = upgrade.member_pe_identity(archive, archive.getinfo('runtime/python.exe'))
        assert (identity == upgrade.PE_AMD64) is (machine == 0x8664)


def test_packaged_source_symlinks_and_output_inside_source_are_rejected(release_fixture, tmp_path):
    root, main, cpu = release_fixture()
    outside = tmp_path / 'outside.js'
    outside.write_text('outside')
    (root / 'web/link.js').symlink_to(outside)
    with pytest.raises(ValueError, match='symlink'):
        upgrade.source_paths(root)
    with pytest.raises(ValueError, match='inside a packaged source'):
        upgrade.build(main, cpu, root / 'web/Main', source_output=root / 'web/Source', project_root=root)


@pytest.mark.parametrize('record', [
    {'path': '../escape', 'bytes': 1, 'sha256': '0' * 64},
    {'path': 'runtime/file', 'bytes': True, 'sha256': '0' * 64},
    {'path': 'runtime/file', 'bytes': 1, 'sha256': 'invalid'},
    {'path': upgrade.MANIFEST, 'bytes': 1, 'sha256': '0' * 64},
])
def test_invalid_manifest_records_are_rejected_before_file_use(record):
    with pytest.raises(ValueError):
        upgrade.manifest_records({'files': [record]}, upgrade.MANIFEST)
