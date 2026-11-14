"""User correction and instrument-import workflows missed by the 0.4 checks."""
from copy import deepcopy
from io import BytesIO
import json
import time
import xml.etree.ElementTree as ET

import numpy as np
from openpyxl import Workbook
import pytest
from fastapi.testclient import TestClient

from workbench.analysis import analyze_wb
from workbench.charts import analysis_chart
from workbench.qpcr import analyze_qpcr, read_qpcr_table
from workbench.server import create_app


CT_TEXT = ('Sample,Sample,Instrument settings\n'
           'Well,Sample,Target,Ct,Group\n'
           'A1,c1,Ref,20,C\nA2,c1,X,22,C\n'
           'A3,t1,Ref,20,T\nA4,t1,X,21,T\n')


def test_qpcr_instrument_metadata_does_not_hide_later_header(tmp_path):
    path = tmp_path / 'instrument.csv'
    path.write_text(CT_TEXT, encoding='utf-8')
    table = read_qpcr_table(path)
    assert table['header_row'] == 2
    assert [row['source_row'] for row in table['rows']] == [3, 4, 5, 6]
    result = analyze_qpcr(table['rows'], {'reference_gene': 'Ref', 'control_group': 'C'})
    assert {row['group']: row['fold_change'] for row in result['rows']} == {'C': 1., 'T': 2.}


def test_qpcr_workbook_metadata_sheet_does_not_block_real_results(tmp_path):
    book = Workbook()
    book.active.title = 'Metadata'
    book.active.append(['Name', 'Sample', 'Settings'])
    results = book.create_sheet('Results')
    for line in CT_TEXT.splitlines()[1:]:
        results.append(line.split(','))
    path = tmp_path / 'instrument.xlsx'
    book.save(path)
    table = read_qpcr_table(path)
    assert table['sheet'] == 'Results'
    assert len(table['rows']) == 4
    assert table['rows'][0]['sample'] == 'c1'


def test_qpcr_ambiguous_real_header_remains_rejected(tmp_path):
    path = tmp_path / 'ambiguous.csv'
    path.write_text('Sample,Target,Ct,Group,Sample Name\nc1,Ref,20,C,c2\n')
    with pytest.raises(ValueError, match='多个 sample'):
        read_qpcr_table(path)


@pytest.mark.parametrize('kind', ['csv', 'csv_semicolon', 'csv_utf16', 'tsv', 'xlsx'])
def test_qpcr_instrument_import_reaches_saved_analysis_result(tmp_path, kind):
    extension, mime, data = 'csv', 'text/csv', CT_TEXT.encode()
    if kind == 'csv_semicolon':
        data = CT_TEXT.replace(',', ';').encode()
    elif kind == 'csv_utf16':
        data = CT_TEXT.replace(',', '\t').encode('utf-16')
    elif kind == 'tsv':
        extension, mime, data = 'tsv', 'text/tab-separated-values', CT_TEXT.replace(',', '\t').encode()
    elif kind == 'xlsx':
        book = Workbook()
        book.active.title = 'Metadata'
        book.active.append(['Name', 'Sample', 'Settings'])
        sheet = book.create_sheet('Results')
        for line in CT_TEXT.splitlines():
            sheet.append(line.split(','))
        stream = BytesIO()
        book.save(stream)
        extension, mime, data = 'xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', stream.getvalue()
    with TestClient(create_app(tmp_path)) as client:
        response = client.post('/api/assets', files={'files': (f'instrument.{extension}', data, mime)})
        assert response.status_code == 200, response.text
        asset = response.json()['assets'][0]
        preview = client.get(f'/api/assets/{asset["id"]}/qpcr').json()
        assert preview['header_row'] == 2 and len(preview['rows']) == 4
        started = client.post('/api/jobs', json={'kind': 'qpcr', 'params': {
            'asset_id': asset['id'], 'reference_gene': 'Ref', 'control_group': 'C'}})
        assert started.status_code == 200, started.text
        job_id = started.json()['id']
        for _ in range(300):
            job = client.get(f'/api/jobs/{job_id}').json()
            if job['status'] in {'done', 'error', 'cancelled'}:
                break
            time.sleep(.01)
        assert job['status'] == 'done' and job['committed'], job
        saved = client.get('/api/project').json()['measurements']
        assert saved[-1]['id'] == job_id
        assert {row['group']: row['fold_change'] for row in saved[-1]['result']['rows']} == {'C': 1., 'T': 2.}


@pytest.mark.parametrize('kind', ['csv', 'xlsx'])
def test_ordinary_non_qpcr_tables_keep_their_active_header(tmp_path, kind):
    data = b'name,value\nalpha,1\nbeta,2\n'
    if kind == 'xlsx':
        book = Workbook()
        book.active.title = 'Measurements'
        for row in [['name', 'value'], ['alpha', 1], ['beta', 2]]:
            book.active.append(row)
        stream = BytesIO()
        book.save(stream)
        data = stream.getvalue()
    with TestClient(create_app(tmp_path)) as client:
        response = client.post('/api/assets', files={'files': (f'table.{kind}', data)})
        assert response.status_code == 200, response.text
        asset = response.json()['assets'][0]
        table = client.get(f'/api/assets/{asset["id"]}/table').json()
        assert table['columns'] == ['name', 'value']
        assert [row['name'] for row in table['rows']] == ['alpha', 'beta']


def blot_roi(identifier, *, group, role='target', **values):
    return {'id': identifier, 'asset_id': identifier, 'x': 1, 'y': 1, 'width': 5, 'height': 5,
            'sample_id': 'sample1', 'biological_replicate': '1', 'technical_replicate': '1',
            'experiment_id': 'experiment1', 'group': group, 'role': role,
            'protein': 'Ref' if role == 'control' else 'X', **values}


def blot_result(rois):
    images = {row['asset_id']: np.full((8, 8), 50 if row['role'] == 'control' else 100, np.uint16)
              for row in rois}
    return analyze_wb(images, {'rois': rois, 'polarity': 'bright', 'background': 'none'})


@pytest.mark.parametrize('explicit', [True, False])
def test_wb_loading_control_cannot_cross_group_identity(explicit):
    target = blot_roi('target', group='Treatment', **({'control_roi_id': 'control'} if explicit else {}))
    control = blot_roi('control', group='Control', role='control')
    result = blot_result([target, control])
    assert result['rows'][0]['normalized'] is None
    assert ('control_sample_identity_mismatch' if explicit else 'control_missing') in result['rows'][0]['qc_flags']


def test_wb_reused_sample_labels_in_distinct_groups_normalize_independently():
    rois = [blot_roi(f'{group}-{role}', group=group, role=role)
            for group in ('Control', 'Treatment') for role in ('target', 'control')]
    result = blot_result(rois)
    targets = [row for row in result['rows'] if row['role'] == 'target']
    assert [row['normalized'] for row in targets] == [2., 2.]
    assert all(row['control_roi_id'] == f'{row["group"]}-control' for row in targets)
    assert {group['group'] for group in result['groups']} == {'Control', 'Treatment'}


def test_wb_deselecting_duplicate_roi_restores_the_retained_measurement():
    keep = blot_roi('target', group='Control')
    duplicate = {**keep, 'id': 'copy', 'biological_replicate': 'another'}
    unresolved = blot_result([keep, duplicate])
    assert unresolved['groups'] == []
    corrected = blot_result([keep, {**duplicate, 'included': False}])
    assert corrected['groups'][0]['n_biological'] == 1
    assert corrected['groups'][0]['n_technical'] == 1
    assert corrected['rows'][0]['analysis_included']
    assert 'duplicate_source_roi' not in corrected['rows'][0]['qc_flags']
    assert 'excluded_by_user' in corrected['rows'][1]['qc_flags']
    assert len(corrected['rows']) == 2


@pytest.mark.parametrize('exclude_by_row', [False, True])
def test_qpcr_deselecting_duplicate_well_restores_selected_measurement(exclude_by_row):
    rows = [{'sample': 'c1', 'target': 'Ref', 'ct': 20, 'group': 'C', 'well': 'A1', 'source_row': 2},
            {'sample': 'c1', 'target': 'X', 'ct': 22, 'group': 'C', 'well': 'A2', 'source_row': 3},
            {'sample': 'c1', 'target': 'X', 'ct': 25, 'group': 'C', 'well': 'A2', 'source_row': 4}]
    with pytest.raises(ValueError, match='没有可配对'):
        analyze_qpcr(rows, {'reference_gene': 'Ref', 'control_group': 'C'})
    params = {'reference_gene': 'Ref', 'control_group': 'C'}
    if exclude_by_row:
        params['excluded_source_rows'] = [4]
    else:
        rows[-1]['included'] = False
    result = analyze_qpcr(rows, params)
    assert result['rows'][0]['delta_ct'] == 2.
    assert result['rows'][0]['n_technical'] == 1
    assert result['source_rows'][1]['included']
    assert not result['source_rows'][2]['included']
    assert 'excluded_by_user' in result['source_rows'][2]['qc_flags']
    assert len(result['source_rows']) == 3


def chart_description(svg):
    root = ET.fromstring(svg)
    return json.loads(root.find('.//{http://purl.org/dc/elements/1.1/}description').text)


def test_chart_labels_keep_experiments_distinguishable_without_pooling():
    result = {'kind': 'wb', 'groups': [
        {'experiment_id': 'blot-A', 'protein': 'X', 'group': 'Control', 'metric': 'normalized', 'values': [1., 2.]},
        {'experiment_id': 'blot-B', 'protein': 'X', 'group': 'Control', 'metric': 'normalized', 'values': [3., 4.]},
    ]}
    before = deepcopy(result)
    plotted = chart_description(analysis_chart(result))['plot_groups']
    assert [group['label'] for group in plotted] == ['blot-A\nX\nControl', 'blot-B\nX\nControl']
    assert [group['values'] for group in plotted] == [[1., 2.], [3., 4.]]
    assert result == before


def test_chart_rejects_mixed_normalization_units_on_one_axis():
    result = {'kind': 'wb', 'groups': [
        {'group': 'Control', 'metric': 'fold', 'values': [1., 2.]},
        {'group': 'Treatment', 'metric': 'background_subtracted', 'values': [1000., 2000.]},
    ]}
    with pytest.raises(ValueError, match='不同计量方式'):
        analysis_chart(result)
