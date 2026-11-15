"""The client ships program resources while developer data stays outside it."""
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'packaging'))
import assemble_windows
from runtime_payload import is_application_source, verify_application_payload, verify_complete_payload


@pytest.mark.parametrize('name', [
    'run.py', 'launch.py', 'LICENSE', 'requirements.txt', 'requirements-desktop.txt',
    'package.json', 'workbench/app.py', 'workbench/preview_cache.py',
    'web/index.html', 'web/app.js', 'web/modules/icons.js', 'web/data/ladders.json',
    'web/modules/package.json', 'resources/app.ico', 'resources/fonts/DejaVuSans.ttf',
    'resources/fonts/DejaVuSans-Bold.ttf', 'resources/fonts/NotoSansCJK-Regular.ttc',
    'resources/fonts/NotoSansCJK-SC.woff2', 'licenses/Noto-CJK.txt',
])
def test_application_code_references_icons_fonts_and_licenses_remain_allowed(name):
    assert is_application_source(name)
    verify_application_payload([name])


@pytest.mark.parametrize('name', [
    'docs/screenshots/0.6.1/wb-quantification.png', 'docs/screenshots_0.3/wb_automatic_figure.png',
    'tests/browser_wb_workflow.py', 'benchmarks/results.json', '.github/workflows/test.yml',
    'web/uploads/wb.tif', 'web/workspace.json', 'web/data/experiment.csv',
    'workbench/workspace/project.json', 'workbench/__pycache__/app.pyc',
    'workbench/cache/old.py', 'resources/experiment.png', 'resources/fonts/wb.tif',
    'project.json', 'autosave.json', '../run.py', '/run.py', 'web//app.js', 'web\\app.js',
])
def test_scientific_inputs_development_assets_and_workspace_state_are_not_runtime_files(name):
    assert not is_application_source(name)
    with pytest.raises(ValueError, match='non-runtime file or workspace data'):
        verify_application_payload([name])


def test_complete_payload_keeps_verified_dependencies_and_model_weights():
    verify_complete_payload([
        'FigureWorkbench.exe', 'app/run.py', 'app/resources/app.ico',
        'app/resources/fonts/NotoSansCJK-Regular.ttc', 'app/SOURCE_MANIFEST.json',
        'app/packaging/windows_validation.json', 'WINDOWS_BUILD_MANIFEST.json',
        'runtime/python.exe', 'runtime/Lib/site-packages/matplotlib/mpl-data/fonts/ttf/DejaVuSans.ttf',
        'cellpose/models/cyto3', 'cellpose/models/nucleitorch_0',
        'cellpose/models/MODEL_SOURCES.json', 'cellpose/runtime/python.exe',
        'CELLPOSE_BUILD_MANIFEST.json', 'CELLPOSE_INTEGRATION.json', 'CELLPOSE_START_HERE.txt',
    ])


@pytest.mark.parametrize('name', [
    'docs/screenshots/wb.png', 'uploads/wb.tif', 'workspace/project.json', 'project.json',
    'runtime/cache/image.png', 'runtime/instance.json', 'cellpose/workspace/project.json',
    'app/web/data/experiment.csv', 'app/resources/experiment.png',
])
def test_complete_payload_rejects_data_outside_as_well_as_inside_app(name):
    with pytest.raises(ValueError, match='payload contains'):
        verify_complete_payload([name])


def test_legacy_assembler_uses_the_same_runtime_selection(tmp_path, monkeypatch):
    source, destination = tmp_path / 'source', tmp_path / 'output'
    contents = {
        'run.py': b'# entry point', 'workbench/__init__.py': b'# application',
        'web/index.html': b'<!doctype html>', 'web/data/ladders.json': b'{}',
        'resources/app.ico': b'icon', 'resources/fonts/DejaVuSans.ttf': b'font',
        'docs/screenshots/wb.png': b'synthetic documentation',
        'tests/fixtures/input.tif': b'synthetic test input',
        'web/uploads/input.tif': b'synthetic workspace input',
    }
    for name, data in contents.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    monkeypatch.setattr(assemble_windows, 'PROJECT_ROOT', source)
    count = assemble_windows.copy_source(destination)
    expected = {name for name in contents if is_application_source(name)}
    actual = {path.relative_to(destination).as_posix() for path in destination.rglob('*') if path.is_file()}
    assert count == len(expected) and actual == expected
    assert len(expected) == 6
