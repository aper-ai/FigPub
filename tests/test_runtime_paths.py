"""Resource/data separation without claiming a real Windows runtime test."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys

from fastapi.testclient import TestClient
import pytest

from workbench import runtime_paths
from workbench.server import create_app


def test_portable_app_root_is_independent_of_cwd(tmp_path, monkeypatch):
    application = tmp_path / 'portable' / 'app'
    module_path = application / 'workbench' / 'runtime_paths.py'
    module_path.parent.mkdir(parents=True)
    shutil.copyfile(runtime_paths.__file__, module_path)
    unrelated = tmp_path / 'opened-from-here'
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    specification = importlib.util.spec_from_file_location('portable_runtime_paths_test', module_path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    assert module.bundle_root() == application


def test_pyinstaller_resource_root_only_when_frozen(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    source_root = Path(runtime_paths.__file__).resolve().parents[1]
    assert runtime_paths.bundle_root() == source_root
    monkeypatch.setattr(sys, 'frozen', True)
    assert runtime_paths.bundle_root() == tmp_path


def test_windows_user_data_does_not_use_program_directory(tmp_path, monkeypatch):
    user_home = tmp_path / 'user'
    program = tmp_path / 'readonly-program'
    program.mkdir()
    monkeypatch.chdir(program)
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: user_home))
    monkeypatch.delenv('FIGURE_WORKBENCH_DATA', raising=False)
    monkeypatch.setenv('LOCALAPPDATA', str(user_home / 'Local'))
    assert runtime_paths.default_data_dir() == user_home / 'Local' / 'FigureWorkbench'
    monkeypatch.delenv('LOCALAPPDATA')
    assert runtime_paths.default_data_dir() == user_home / 'AppData' / 'Local' / 'FigureWorkbench'
    assert list(program.iterdir()) == []


def test_relative_user_override_is_explicit_and_resolved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('FIGURE_WORKBENCH_DATA', 'chosen-working-folder')
    assert runtime_paths.default_data_dir() == tmp_path / 'chosen-working-folder'
    assert not (tmp_path / 'chosen-working-folder').exists()


def test_server_uses_frozen_bundle_resources_and_separate_autosave(tmp_path, monkeypatch):
    from workbench import server
    bundle, data = tmp_path / 'bundle', tmp_path / 'user-data'
    (bundle / 'web').mkdir(parents=True)
    (bundle / 'resources' / 'fonts').mkdir(parents=True)
    (bundle / 'benchmarks').mkdir()
    (bundle / 'web' / 'index.html').write_text('portable-resource-sentinel')
    (bundle / 'resources' / 'fonts' / 'DejaVuSans.ttf').write_bytes(b'font-route-sentinel')
    (bundle / 'benchmarks' / 'results.json').write_text(json.dumps({'source': 'bundled-measurement'}))
    monkeypatch.setattr(server, 'bundle_root', lambda: bundle)
    with TestClient(create_app(data, instance_id='a' * 32)) as client:
        assert client.get('/').text == 'portable-resource-sentinel'
        assert client.get('/fonts/DejaVuSans.ttf').content == b'font-route-sentinel'
        assert client.get('/api/benchmarks').json()['source'] == 'bundled-measurement'
        assert client.get('/api/health').json()['instance_id'] == 'a' * 32
    assert (data / 'current.json').is_file()
    assert not (bundle / 'current.json').exists()


def test_missing_bundle_resources_fail_before_blank_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_paths, 'bundle_root', lambda: tmp_path)
    with pytest.raises(FileNotFoundError, match='web/index.html'):
        runtime_paths.check_resources()
