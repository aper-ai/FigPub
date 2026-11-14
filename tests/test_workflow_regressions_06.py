"""Failure recovery and input fidelity across import, save and restart."""
from __future__ import annotations

import hashlib
from io import BytesIO
import json
from pathlib import Path
import time
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient
import fitz
from openpyxl import Workbook
from PIL import Image
import pytest

from workbench import exporting, qpcr, storage
from workbench.jobs import JobManager
from workbench.server import create_app
from workbench.storage import ProjectStore, json_bytes


def image_bytes(color):
    output = BytesIO()
    Image.new('RGB', (12, 8), color).save(output, format='PNG')
    return output.getvalue()


@pytest.mark.parametrize('operation', ['new', 'open'])
def test_failed_project_replacement_preserves_active_and_persisted_project(tmp_path, monkeypatch, operation):
    candidate = ProjectStore(tmp_path / 'candidate')
    candidate.add_files([('replacement.png', image_bytes('blue'))])
    archive = candidate.export_archive()
    app = create_app(tmp_path / 'active')
    with TestClient(app, raise_server_exceptions=False) as client:
        old = client.post('/api/assets', files={'files': ('existing.png', image_bytes('red'))})
        assert old.status_code == 200
        before = client.get('/api/project').json()
        current_file = app.state.store.root / 'current.json'
        persisted = current_file.read_bytes()
        write = storage.atomic_write
        attempts = 0

        def fail_once(path, data):
            nonlocal attempts
            if Path(path) == current_file and attempts == 0:
                attempts += 1
                raise OSError('simulated disk full')
            return write(path, data)

        monkeypatch.setattr(storage, 'atomic_write', fail_once)
        if operation == 'new':
            response = client.post('/api/project/new', json={'title': 'Replacement project'})
        else:
            response = client.post('/api/project/open', files={'file': ('replacement.figwork', archive)})
        assert client.get('/api/project').json() == before
        assert current_file.read_bytes() == persisted
        assert ProjectStore(app.state.store.root).get_project() == before
        assert response.status_code == 503
        assert response.json()['code'] == 'local_io_error'
        if operation == 'new':
            retry = client.post('/api/project/new', json={'title': 'Replacement project'})
        else:
            retry = client.post('/api/project/open', files={'file': ('replacement.figwork', archive)})
        assert retry.status_code == 200
        after = retry.json()
        assert after['id'] != before['id']
        assert len(after['assets']) == (0 if operation == 'new' else 1)
        assert ProjectStore(app.state.store.root).get_project() == after


def test_failed_original_registry_write_can_retry_and_survive_restart(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path)
    store.add_files([('existing.png', image_bytes('red'))])
    before = store.get_project()
    original = image_bytes('blue')
    digest = hashlib.sha256(original).hexdigest()
    write = storage.atomic_write
    failed = False

    def fail_registry_once(path, data):
        nonlocal failed
        if Path(path).name == 'assets.json' and not failed:
            failed = True
            raise OSError('simulated registry write failure')
        return write(path, data)

    monkeypatch.setattr(storage, 'atomic_write', fail_registry_once)
    with pytest.raises(OSError, match='registry write failure'):
        store.add_files([('new.png', original)])
    assert store.get_project() == before
    store.add_files([('new.png', original)])
    expected = store.get_project()
    restored = ProjectStore(tmp_path)
    assert restored.get_project() == expected
    assert digest in json.loads((tmp_path / 'assets.json').read_text())
    assert restored.resolve(digest).read_bytes() == original


def test_oversized_analysis_result_cannot_replace_a_reopenable_project(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path)
    before = store.get_project()
    persisted = (tmp_path / 'current.json').read_bytes()
    monkeypatch.setattr(storage, 'MAX_PROJECT_BYTES', len(json_bytes(before)) + 1024)
    manager = JobManager(store, runner=lambda kind, params: {'kind': kind, 'values': 'x' * 4096})
    try:
        job = manager.submit('microscopy', {})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = manager.get(job['id'])
            if result['status'] not in {'queued', 'running'}:
                break
            time.sleep(.005)
        assert result['status'] == 'error'
        assert 'size limit' in result['error']
        assert store.get_project() == before
        assert (tmp_path / 'current.json').read_bytes() == persisted
        assert ProjectStore(tmp_path).get_project() == before
    finally:
        manager.shutdown(wait=True)


@pytest.mark.parametrize('suffix,delimiter', [('csv', ','), ('tsv', '\t')])
def test_delimited_import_rejects_nonempty_cells_beyond_header_without_losing_project(tmp_path, suffix, delimiter):
    source = f'name{delimiter}value\nalpha{delimiter}1{delimiter}unlabelled value\n'.encode()
    with TestClient(create_app(tmp_path)) as client:
        before = client.get('/api/project').json()
        response = client.post('/api/assets', files={'files': (f'overflow.{suffix}', source)})
        assert response.status_code == 400
        assert '表头' in response.json()['detail']
        assert client.get('/api/project').json() == before


def test_delimited_import_keeps_quoted_delimiters_and_empty_trailing_columns(tmp_path):
    source = b'name,value\n"alpha, beta",1,,\n"gamma",2,\n'
    store = ProjectStore(tmp_path)
    asset = store.add_files([('measurements.csv', source)])[0]
    table = store.read_table(asset['id'])
    assert table['columns'] == ['name', 'value']
    assert table['rows'] == [{'name': 'alpha, beta', 'value': '1'}, {'name': 'gamma', 'value': '2'}]
    restored = ProjectStore(tmp_path / 'restored')
    restored.open_archive(store.export_archive())
    assert restored.resolve(asset['id']).read_bytes() == source
    assert restored.read_table(asset['id']) == table


@pytest.mark.parametrize('invalid_ct', [True, False])
def test_excel_boolean_ct_is_excluded_without_changing_known_fold_result(tmp_path, invalid_ct):
    book = Workbook()
    for row in [
        ['Sample', 'Target', 'Ct', 'Group', 'Well'],
        ['c1', 'Ref', 20, 'C', 'A1'],
        ['c1', 'X', 22, 'C', 'A2'],
        ['t1', 'Ref', 20, 'T', 'A3'],
        ['t1', 'X', 21, 'T', 'A4'],
        ['c1', 'Ref', invalid_ct, 'C', 'A5'],
    ]:
        book.active.append(row)
    source = tmp_path / 'ct.xlsx'
    book.save(source)
    table = qpcr.read_qpcr_table(source)
    assert table['rows'][-1]['ct'] is invalid_ct
    result = qpcr.analyze_qpcr(table['rows'], {'reference_gene': 'Ref', 'control_group': 'C'})
    # ΔCt(C)=22−20=2; ΔCt(T)=21−20=1; 2^−(1−2)=2.
    assert {row['group']: row['fold_change'] for row in result['rows']} == {'C': 1., 'T': 2.}
    excluded = result['source_rows'][-1]
    assert excluded['source_row'] == 6
    assert excluded['ct'] is None and not excluded['included']
    assert 'undetermined_or_invalid_ct' in excluded['qc_flags']


def test_qpcr_unselected_large_metadata_sheet_does_not_block_results(tmp_path):
    book = Workbook()
    book.active.title = 'Metadata'
    book.active['A1'] = 'Instrument settings'
    book.active['A200001'] = 'Metadata footer'
    results = book.create_sheet('Results')
    results.append(['Sample', 'Target', 'Ct', 'Group'])
    results.append(['c1', 'Ref', 20, 'C'])
    results.append(['c1', 'X', 22, 'C'])
    source = tmp_path / 'sparse-instrument.xlsx'
    book.save(source)
    table = qpcr.read_qpcr_table(source)
    assert table['sheet'] == 'Results'
    assert table['sheets'] == ['Metadata', 'Results']
    assert [row['source_row'] for row in table['rows']] == [2, 3]
    assert [row['ct'] for row in table['rows']] == [20, 22]


@pytest.mark.parametrize('suffix', ['csv', 'xlsx'])
@pytest.mark.parametrize('limit,value', [('MAX_QPCR_ROWS', 3), ('MAX_QPCR_COLUMNS', 3), ('MAX_QPCR_CELLS', 12)])
def test_qpcr_reader_bounds_selected_table_before_accepting_it(tmp_path, monkeypatch, suffix, limit, value):
    rows = [['Sample', 'Target', 'Ct', 'Group'],
            ['c1', 'Ref', 20, 'C'], ['c1', 'X', 22, 'C'], ['t1', 'Ref', 20, 'T']]
    source = tmp_path / f'limited.{suffix}'
    if suffix == 'xlsx':
        book = Workbook()
        for row in rows:
            book.active.append(row)
        book.save(source)
    else:
        source.write_text('\n'.join(','.join(map(str, row)) for row in rows), encoding='utf-8')
    monkeypatch.setattr(qpcr, limit, value, raising=False)
    with pytest.raises(ValueError, match='超过'):
        qpcr.read_qpcr_table(source)


def test_qpcr_reader_rejects_unclosed_csv_quotes(tmp_path):
    source = tmp_path / 'unclosed.csv'
    source.write_text('Sample,Target,Ct,Group\nc1,Ref,"20,C\nc1,X,22,C\n', encoding='utf-8')
    with pytest.raises(ValueError, match='引号|格式'):
        qpcr.read_qpcr_table(source)


def test_general_csv_import_rejects_unclosed_quotes_before_saving_source(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        before = client.get('/api/project').json()
        response = client.post('/api/assets', files={'files': ('unclosed.csv', b'name,value\nalpha,"1\nbeta,2\n')})
        assert response.status_code == 400
        assert client.get('/api/project').json() == before


def test_excel_numeric_zero_header_keeps_its_source_column_identity(tmp_path):
    book = Workbook()
    book.active.append([0, 'value'])
    book.active.append(['alpha', 1])
    buffer = BytesIO()
    book.save(buffer)
    store = ProjectStore(tmp_path)
    asset = store.add_files([('zero-header.xlsx', buffer.getvalue())])[0]
    table = store.read_table(asset['id'])
    assert table['columns'] == ['0', 'value']
    assert table['rows'] == [{'0': 'alpha', 'value': 1}]


@pytest.mark.parametrize('format', ['pdf', 'svg'])
def test_saved_large_canvas_exports_vectors_at_exact_physical_size(tmp_path, format):
    store = ProjectStore(tmp_path / 'saved')
    project = store.get_project()
    project['figure'].update(width_mm=2000, height_mm=120)
    store.update_project(project)
    restored = ProjectStore(tmp_path / 'restored')
    restored.open_archive(store.export_archive())
    data, _, _ = exporting.export_project(restored.get_project(), restored.resolve, format)
    if format == 'pdf':
        with fitz.open(stream=data, filetype='pdf') as document:
            assert document[0].rect.width / exporting.MM_TO_PT == pytest.approx(2000, abs=.001)
            assert document[0].rect.height / exporting.MM_TO_PT == pytest.approx(120, abs=.001)
    else:
        root = ET.fromstring(data)
        assert root.get('width') == '2000mm' and root.get('height') == '120mm'


@pytest.mark.parametrize('format', ['png', 'tiff'])
def test_large_canvas_raster_limit_rejects_before_rendering(tmp_path, monkeypatch, format):
    store = ProjectStore(tmp_path)
    project = store.get_project()
    project['figure'].update(width_mm=2000, height_mm=2000)
    store.update_project(project)

    def must_not_render(*args, **kwargs):
        raise AssertionError('An oversized raster must be rejected before rendering')

    monkeypatch.setattr(exporting, '_pdf_bytes', must_not_render)
    with pytest.raises(ValueError, match='像素'):
        exporting.export_project(store.get_project(), store.resolve, format, dpi=1200)


@pytest.mark.parametrize('width,height', [(10, 120), (180, 10), (1500, 120)])
def test_pptx_unsupported_sizes_have_explicit_error_and_preserve_project(tmp_path, width, height):
    with TestClient(create_app(tmp_path)) as client:
        project = client.get('/api/project').json()
        project['figure'].update(width_mm=width, height_mm=height)
        response = client.put('/api/project', json=project)
        assert response.status_code == 200
        before = response.json()
        export = client.post('/api/export', json={'format': 'pptx'})
        assert export.status_code == 400
        assert 'PPTX' in export.json()['detail']
        assert '25.4' in export.json()['detail'] and '1422.4' in export.json()['detail']
        assert client.get('/api/project').json() == before


@pytest.mark.parametrize('width,height', [(25.4, 25.4), (1422.4, 180)])
def test_pptx_boundary_sizes_roundtrip_without_scaling(tmp_path, width, height):
    from pptx import Presentation

    store = ProjectStore(tmp_path)
    project = store.get_project()
    project['figure'].update(width_mm=width, height_mm=height)
    data, _, _ = exporting.export_project(project, store.resolve, 'pptx')
    restored = Presentation(BytesIO(data))
    assert restored.slide_width.mm == pytest.approx(width, abs=.001)
    assert restored.slide_height.mm == pytest.approx(height, abs=.001)
