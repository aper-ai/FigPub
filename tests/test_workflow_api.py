"""Integration checks at the source, project revision and archive boundaries."""
from io import BytesIO
import copy
import time

import numpy as np
import pytest
import tifffile
from fastapi.testclient import TestClient

from workbench.analysis import source_range
from workbench.server import create_app


def upload(client, data, name='original.tiff', *, rgb=False):
    stream = BytesIO()
    tifffile.imwrite(stream, data, photometric='rgb' if rgb else 'minisblack')
    response = client.post('/api/assets', files={'files': (name, stream.getvalue(), 'image/tiff')})
    assert response.status_code == 200, response.text
    return response.json()['assets'][0]


def analyze(client, params):
    response = client.post('/api/jobs', json={'kind': 'microscopy', 'params': params})
    assert response.status_code == 200, response.text
    job_id = response.json()['id']
    for _ in range(500):
        job = client.get('/api/jobs/' + job_id).json()
        if job['status'] in {'done', 'error', 'cancelled'}:
            return job
        time.sleep(.01)
    raise AssertionError('Analysis did not finish')


def source(asset, channel=0):
    return {'asset_id': asset['id'], 'series': 0, 'frame': 0, 'channel': channel}


def context(client, **rest):
    project = client.get('/api/project').json()
    return {'project_id': project['id'], 'revision': project['revision'], **rest}


@pytest.mark.parametrize('value', [True, 12.5, '3.2', float('nan'), 0, 33])
def test_detector_bits_are_not_silently_truncated(value):
    with pytest.raises(ValueError):
        source_range(np.zeros((2, 2), np.uint16), {'adc_bits': value})


def test_explicit_black_level_has_priority_over_adc_default():
    image = np.zeros((2, 2), np.uint16)
    assert source_range(image, {'adc_bits': 12, 'black_level': 50}) == (50, 4095)
    assert source_range(image, {'adc_bits': 12, 'black_level': 50, 'saturation_value': 4000}) == (50, 4000)


def test_review_validates_channel_contract_before_loading_originals(tmp_path, monkeypatch):
    from workbench.instances import encode_instance_mask
    app = create_app(tmp_path)
    with TestClient(app) as client:
        asset = upload(client, np.zeros((8, 8), np.uint16))
        project = client.get('/api/project').json()
        record = {'id': 'saved', 'kind': 'microscopy', 'params': {'method': 'nuclei'},
                  'result': {'instance_mask': encode_instance_mask(np.zeros((8, 8), np.uint32))}}
        project['measurements'] = [record]
        monkeypatch.setattr(app.state.store, 'get_project', lambda: copy.deepcopy(project))
        monkeypatch.setattr(app.state.store, 'read_plane', lambda *_: pytest.fail('Invalid channels must fail before original image decoding'))
        channel = {'id': 'c0', 'source': source(asset)}
        for channels in ([], 'invalid', [None], [channel] * 33, [channel, {**channel, 'id': 'c1'}]):
            record['params']['channels'] = channels
            response = client.post('/api/instances/review', json={
                'project_id': project['id'], 'revision': project['revision'], 'measurement_id': 'saved'})
            assert response.status_code == 400, response.text


def test_review_caps_accumulated_raw_channels_and_keeps_project_unchanged(tmp_path, monkeypatch):
    from workbench import workflow_api
    from workbench.instances import encode_instance_mask
    app = create_app(tmp_path)
    with TestClient(app) as client:
        asset = upload(client, np.zeros((4, 4), np.uint16))
        project = client.get('/api/project').json()
        project['measurements'] = [{'id': 'saved', 'kind': 'microscopy',
            'params': {'method': 'myotube', 'channels': [
                {'id': f'c{i}', 'source': source(asset)} for i in range(3)]},
            'result': {'instance_mask': encode_instance_mask(np.zeros((4, 4), np.uint32))}}]
        original = copy.deepcopy(project)
        reads = []
        def read_plane(identity):
            reads.append(identity)
            return np.zeros((4, 4), np.uint16)
        monkeypatch.setattr(app.state.store, 'get_project', lambda: copy.deepcopy(project))
        monkeypatch.setattr(app.state.store, 'read_plane', read_plane)
        monkeypatch.setattr(workflow_api, 'MAX_REVIEW_SOURCE_BYTES', 40)
        response = client.post('/api/instances/review', json={
            'project_id': project['id'], 'revision': project['revision'], 'measurement_id': 'saved'})
        assert response.status_code == 400 and '512 MiB' in response.text
        assert len(reads) == 2  # Further planes were never loaded.
        assert project == original


def test_instance_review_is_read_only_and_round_trips_its_recipe(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        raw = np.zeros((50, 70), np.uint16)
        raw[9:21, 10:22] = 300
        raw[28:41, 42:55] = 600
        asset = upload(client, raw)
        job = analyze(client, {'method': 'nuclei', 'source': source(asset), 'threshold': 100,
                               'min_area': 10, 'sigma': 0, 'split_touching': False})
        assert job['status'] == 'done', job
        base = job['result']
        assert base['instance_mask']
        ids = [item['id'] for item in base['objects']]
        assert len(ids) == 2
        operation = {'id': 'merge_checked', 'type': 'merge', 'ids': ids, 'allow_disconnected': True}
        payload = context(client, measurement_id=job['id'], base_mask_sha256=base['instance_mask']['sha256'],
                          operations=[operation], cursor=1)
        before = client.get('/api/project').json()
        response = client.post('/api/instances/review', json=payload)
        assert response.status_code == 200, response.text
        reviewed = response.json()
        assert reviewed['summary']['count'] == 1
        assert sum(item['area_px'] for item in reviewed['objects']) == sum(item['area_px'] for item in base['objects'])
        assert client.get('/api/project').json() == before
        exported = client.post('/api/instances/export', json=payload)
        assert exported.status_code == 200, exported.text
        labels = tifffile.imread(BytesIO(exported.content))
        assert labels.dtype == np.uint32 and labels.shape == raw.shape
        assert np.count_nonzero(labels) == np.count_nonzero(raw)

        before['settings']['instance_reviews'] = {job['id']: {
            'base_mask_sha256': base['instance_mask']['sha256'], 'operations': [operation], 'cursor': 1}}
        assert client.put('/api/project', json=before).status_code == 200
        archive = client.post('/api/export', json={'format': 'project'})
        assert archive.status_code == 200
        client.post('/api/project/new', json={'title': 'another'})
        opened = client.post('/api/project/open', files={'file': ('review.figwork', archive.content, 'application/zip')})
        assert opened.status_code == 200, opened.text
        restored = opened.json()
        assert restored['measurements'][0]['result']['instance_mask'] == base['instance_mask']
        recipe = restored['settings']['instance_reviews'][job['id']]
        replay = client.post('/api/instances/review', json=context(client, measurement_id=job['id'], **recipe))
        assert replay.status_code == 200, replay.text
        assert replay.json()['instance_mask']['sha256'] == reviewed['instance_mask']['sha256']
        original = client.get(f'/api/assets/{asset["id"]}/original')
        np.testing.assert_array_equal(tifffile.imread(BytesIO(original.content)), raw)


def test_review_rejects_changed_project_or_mask_without_replacing_original(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        raw = np.zeros((40, 40), np.uint8); raw[10:28, 10:28] = 200
        asset = upload(client, raw)
        job = analyze(client, {'method': 'nuclei', 'source': source(asset), 'threshold': 100, 'min_area': 10})
        assert job['status'] == 'done', job
        base = context(client, measurement_id=job['id'], operations=[], cursor=0,
                       base_mask_sha256=job['result']['instance_mask']['sha256'])
        for changed in [{'project_id': 'wrong-project'}, {'revision': base['revision'] - 1},
                        {'revision': True}, {'base_mask_sha256': '0' * 64}]:
            assert client.post('/api/instances/review', json={**base, **changed}).status_code == 409
        assert client.get('/api/project').json()['measurements'][0]['result'] == job['result']


def test_advanced_ihc_requires_true_rgb_and_preserves_profile_in_archive(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        gray = upload(client, np.zeros((16, 17), np.uint8))
        rejected = analyze(client, {'method': 'ihc_advanced', 'source': source(gray), 'stain_method': 'fixed',
                                    'grading_method': 'manual', 'grade_thresholds': [.1, .3, .6]})
        assert rejected['status'] == 'error' and 'RGB' in rejected['error']
        h = np.array([.65, .70, .29]); h /= np.linalg.norm(h)
        d = np.array([.27, .57, .78]); d /= np.linalg.norm(d)
        concentration = np.tile(np.array([.03, .18, .42, .85]), (25, 8))
        image = np.rint(255 * np.exp(-(.3 * h + concentration[..., None] * d))).astype(np.uint8)
        asset = upload(client, image, 'rgb.tiff', rgb=True)
        params = {'method': 'ihc_advanced', 'source': source(asset), 'stain_method': 'fixed',
                  'stain_vectors': [h.tolist(), d.tolist()], 'grading_method': 'manual',
                  'grade_thresholds': [.1, .3, .6], 'tissue_min_od': .05}
        job = analyze(client, params)
        assert job['status'] == 'done', job
        result = job['result']
        assert sum(row['area_px'] for row in result['rows']) == image.shape[0] * image.shape[1]
        assert all(row['area_px'] > 0 for row in result['rows'])
        profile = result['calibration_profile']
        second = analyze(client, {'method': 'ihc_advanced', 'source': source(asset), 'locked_profile': profile})
        assert second['status'] == 'done', second
        assert second['result']['rows'] == result['rows']


def test_exposure_api_uses_only_current_project_originals(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        raw = np.full((60, 70), 3000, np.uint16)
        raw[12:18, 15:35] = 2400; raw[37:43, 15:35] = 2500
        asset = upload(client, raw)
        identities = {'asset_id': asset['id'], 'source': source(asset), 'sample_id': 'sample-1',
                      'group': 'Control', 'biological_replicate': 'bio-1', 'technical_replicate': '1',
                      'experiment_id': 'experiment-A', 'exposure_id': '10s', 'exposure_seconds': 10,
                      'exposure_series_id': 'blot-A', 'included': True}
        rois = [{**identities, 'id': 'target', 'role': 'target', 'protein': 'TARGET',
                 'control_roi_id': 'control', 'x': 12, 'y': 8, 'width': 26, 'height': 15},
                {**identities, 'id': 'control', 'role': 'control', 'protein': 'ACTB',
                 'x': 12, 'y': 33, 'width': 26, 'height': 15}]
        body = context(client, rois=rois, polarity='dark', adc_bits=12)
        before = client.get('/api/project').json()
        valid = client.post('/api/wb/exposure-review', json=body)
        assert valid.status_code == 200, valid.text
        assert client.get('/api/project').json() == before
        wrong = copy.deepcopy(body); wrong['rois'][0]['source']['asset_id'] = 'f' * 64
        assert client.post('/api/wb/exposure-review', json=wrong).status_code == 400
        assert client.post('/api/wb/exposure-review', json={**body, 'revision': -1}).status_code == 409


def test_exposure_api_rejects_excess_rois_before_decoding_sources(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        # No source is present: input bounds must be checked before resolving or
        # loading any image, matching the scientific engine's 5,000-ROI budget.
        response = client.post('/api/wb/exposure-review', json=context(
            client, rois=[{'id': str(index), 'source': {'asset_id': 'f'*64}} for index in range(5001)]))
        assert response.status_code == 400
        assert '1–5000' in response.json()['detail']
