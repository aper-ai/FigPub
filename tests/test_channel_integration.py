"""Real TIFF -> API -> source measurement -> all figure outputs -> archive."""
from concurrent.futures import ThreadPoolExecutor
import copy
from io import BytesIO
import threading
import time
from zipfile import ZipFile

from fastapi.testclient import TestClient
import fitz
import numpy as np
from PIL import Image
import pytest
import tifffile

from workbench.exporting import build_scene, export_project, MM_TO_PT
from workbench.preview_cache import PreviewBusyError, PreviewCache
from workbench.preflight import check_project
from workbench.server import create_app
from workbench.storage import ProjectStore
from workbench.vectors import svg_to_pdf


def multidimensional_tiff():
    t, c, z, y, x = np.indices((2, 4, 3, 36, 48))
    raw = (1000 * t + 100 * z + 10 * c + y + x).astype(np.uint16)
    stream = BytesIO()
    tifffile.imwrite(stream, raw, photometric='minisblack', metadata={'axes': 'TCZYX'}, compression='deflate')
    return raw, stream.getvalue()


def run_job(client, params):
    response = client.post('/api/jobs', json={'kind': 'microscopy', 'params': params})
    assert response.status_code == 200, response.text
    jid = response.json()['id']
    for _ in range(500):
        result = client.get('/api/jobs/' + jid).json()
        if result['status'] not in {'queued', 'running'}:
            assert result['status'] == 'done', result
            return result
        time.sleep(.005)
    pytest.fail('Analysis did not finish')


def test_multidimensional_view_and_measurement_keep_same_raw_plane(tmp_path):
    raw, binary = multidimensional_tiff()
    app = create_app(tmp_path)
    with TestClient(app) as client:
        response = client.post('/api/assets', files={'files': ('tzchannels.tif', binary, 'image/tiff')})
        assert response.status_code == 200, response.text
        aid = response.json()['assets'][0]['id']
        description = client.get(f'/api/assets/{aid}/channels?frame=4').json()
        assert {item['axis']: item['index'] for item in description['frame_coordinates']} == {'T': 1, 'Z': 1}
        view = description['default_view']
        view.update(mode='single', active_channel='c2')
        for channel in view['channels']:
            channel.update(black=0, white=2000, color='#FFFFFF')
        response = client.post('/api/channel-view/preview', json={'channel_view': view, 'max_size': 128})
        assert response.status_code == 200, response.text
        preview = np.asarray(Image.open(BytesIO(response.content)))
        assert preview[10, 20, 0] == round(float(raw[1, 2, 1, 10, 20]) / 2000 * 255)
        raw_pixels = client.get(f'/api/assets/{aid}/pixels.bin?series=0&frame=4&channel=2&max_size=128')
        assert raw_pixels.status_code == 200
        binary_plane = np.frombuffer(raw_pixels.content, dtype='<f4').reshape(36, 48)
        # Localization normalization is intentional; relative raw ordering must
        # come from the chosen plane and not the first T/Z plane.
        expected = app.state.store.read_plane({'asset_id': aid, 'series': 0, 'frame': 4, 'channel': 2})
        np.testing.assert_array_equal(expected, raw[1, 2, 1])
        assert np.isfinite(binary_plane).all()
        roi = {'x': 4, 'y': 5, 'width': 10, 'height': 8}
        channel = {'id': 'selected', 'name': 'Marker', 'source': {'asset_id': aid, 'series': 0, 'frame': 4, 'channel': 2}}
        params = {'method': 'channel_intensity', 'channels': [channel], 'roi': roi}
        before = run_job(client, params)['result']
        mean = float(np.mean(raw[1, 2, 1, 5:13, 4:14]))
        assert before['rows'][0]['raw_mean'] == mean
        assert before['rows'][0]['source'] == channel['source']
        # LUT changes must have no route into numerical input or source bytes.
        view['channels'][2].update(black=1120, white=1140, color='#FF0033', gamma=2, visible=False)
        assert client.post('/api/channel-view/preview', json={'channel_view': view}).status_code == 200
        after = run_job(client, params)['result']
        assert after['rows'][0]['raw_mean'] == before['rows'][0]['raw_mean']
        assert client.get(f'/api/assets/{aid}/original').content == binary


def split_project(tmp_path, *, flip=False, rotation=0):
    store = ProjectStore(tmp_path)
    arrays = [np.full((40, 60), 100, np.uint16), np.full((40, 60), 4000, np.uint16)]
    files = []
    for index, raw in enumerate(arrays):
        stream = BytesIO()
        tifffile.imwrite(stream, raw, photometric='minisblack')
        files.append((f'source{index}.tif', stream.getvalue()))
    assets = store.add_files(files)
    view = {'version': 1, 'mode': 'split', 'active_channel': 'red',
            'split': {'columns': 3, 'gap_px': 8, 'include_merge': True}, 'channels': [
                {'id': 'red', 'name': '信号 Red', 'visible': True, 'black': 0, 'white': 100, 'gamma': 1, 'color': '#FF0000',
                 'source': {'asset_id': assets[0]['id'], 'series': 0, 'frame': 0, 'channel': 0}},
                {'id': 'green', 'name': 'Green', 'visible': True, 'black': 0, 'white': 4000, 'gamma': 1, 'color': '#00FF00',
                 'source': {'asset_id': assets[1]['id'], 'series': 0, 'frame': 0, 'channel': 0}}]}
    project = store.get_project()
    panel = ProjectStore.default_panel(assets[0], '', 15, 25, 125, 32.89473684210526)
    panel.update(channel_view=view, crop={'x': 10, 'y': 10, 'width': 40, 'height': 20},
                 flip_h=flip, rotation=rotation, scale_bar={'length_um': 5, 'um_per_px': 1, 'color': '#FFFFFF'})
    project['panels'] = [panel]
    return store, project, assets


@pytest.mark.parametrize('flip,rotation', [(False, 0), (True, 17)])
def test_split_real_pixels_scale_and_export_geometry(tmp_path, flip, rotation):
    store, project, assets = split_project(tmp_path, flip=flip, rotation=rotation)
    scene = build_scene(project, store.resolve)
    images = [primitive for primitive in scene.primitives if primitive['type'] == 'image']
    assert len(images) == 3
    assert [image['image'].getpixel((3, 3)) for image in images] == [(255, 0, 0), (0, 255, 0), (255, 255, 0)]
    assert all(image['image'].size == (40, 20) for image in images)
    lines = [p for p in scene.primitives if p['type'] == 'line']
    assert len(lines) == 3
    expected_length = 5 / 40 * (125 * 40 / 136)
    assert all(p['x2'] - p['x'] == pytest.approx(expected_length) for p in lines)
    assert images[0]['x'] > images[-1]['x'] if flip else images[0]['x'] < images[-1]['x']
    for extension in ['pdf', 'svg', 'png', 'tiff', 'pptx']:
        data, _, name = export_project(project, store.resolve, extension, dpi=144)
        assert data and name.endswith('.' + extension)
        if extension == 'pdf':
            pdf = data
        elif extension == 'svg':
            restored = svg_to_pdf(data)
        elif extension == 'pptx':
            with ZipFile(BytesIO(data)) as archive:
                xml = archive.read('ppt/slides/slide1.xml').decode()
                assert '202723' in xml and '信号 Red' in xml
    def pixels(data):
        with fitz.open(stream=data, filetype='pdf') as document:
            pix = document[0].get_pixmap(dpi=144, alpha=False)
            return np.frombuffer(pix.samples, np.uint8).astype(np.int16)
    assert np.mean(np.abs(pixels(pdf) - pixels(restored))) < 1.2
    preflight = check_project(project, store.resolve)
    metric = preflight['metrics']['panels'][0]
    assert metric['kind'] == 'multichannel' and len(metric['channels']) == 4
    assert metric['effective_ppi'] == pytest.approx(min(40 / (125 * 40 / 136) * 25.4, 20 / project['panels'][0]['height_mm'] * 25.4))


def test_archive_restores_cross_asset_channel_references_and_exact_bytes(tmp_path):
    store, project, assets = split_project(tmp_path / 'first')
    saved = store.update_project(project)
    payload = store.export_archive(saved)
    target = ProjectStore(tmp_path / 'second')
    restored = target.open_archive(payload)
    assert restored['panels'][0]['channel_view'] == saved['panels'][0]['channel_view']
    for asset in assets:
        assert target.resolve(asset['id']).read_bytes() == store.resolve(asset['id']).read_bytes()


def test_preview_cache_coalesces_concurrent_work_and_bounds_bytes():
    cache = PreviewCache(max_bytes=10, max_entries=2)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def build():
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return b'12345'
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(cache.get_or_create, 'same', build) for _ in range(6)]
        assert entered.wait(2)
        release.set()
        assert [f.result() for f in futures] == [b'12345'] * 6
    assert calls == [1]
    for number in range(20):
        cache.get_or_create(number, lambda: b'123456')
        assert cache._bytes <= 10 and len(cache._items) <= 2


def test_ihc_rejects_multiple_logical_rgb_groups_and_preview_bad_parameters(tmp_path):
    stream = BytesIO()
    tifffile.imwrite(stream, np.ones((2, 4, 9, 3), np.uint8) * 100,
                    photometric='rgb', metadata={'axes': 'CYXS'})
    with TestClient(create_app(tmp_path)) as client:
        response = client.post('/api/assets', files={'files': ('two_rgb_groups.tif', stream.getvalue(), 'image/tiff')})
        assert response.status_code == 200, response.text
        aid = response.json()['assets'][0]['id']
        view = client.get(f'/api/assets/{aid}/channels').json()['default_view']
        assert client.post('/api/channel-view/preview', json={'channel_view': view, 'max_size': None}).status_code == 400
        assert client.post('/api/channel-view/auto-range', json={'channel_view': view, 'channel_ids': [{}]}).status_code == 400
        response = client.post('/api/jobs', json={'kind': 'microscopy', 'params': {'method': 'ihc', 'asset_id': aid}})
        jid = response.json()['id']
        for _ in range(200):
            job = client.get('/api/jobs/' + jid).json()
            if job['status'] not in {'queued', 'running'}:
                break
            time.sleep(.005)
        assert job['status'] == 'error' and 'true RGB' in job['error']


def test_legacy_panel_frame_alias_is_used_by_export_and_preflight(tmp_path):
    store = ProjectStore(tmp_path)
    raw, binary = multidimensional_tiff()
    asset = store.add_files([('frames.tif', binary)])[0]
    project = store.get_project()
    panel = ProjectStore.default_panel(asset, '', 10, 10, 50, 40)
    panel.update(frame=4, channel=2, display={'low': 0, 'high': 2000, 'gamma': 1})
    project['panels'] = [panel]
    scene = build_scene(project, store.resolve)
    image = next(p['image'] for p in scene.primitives if p['type'] == 'image')
    assert abs(image.getpixel((20, 10)) - float(raw[1, 2, 1, 10, 20]) / 2000 * 255) < 1
    check = check_project(project, store.resolve)
    assert not any(issue['severity'] == 'error' for issue in check['issues'])


def test_preview_queue_rejects_excess_distinct_recipes_without_blocking_cached_requests():
    cache = PreviewCache(max_renderers=1, max_pending=1)
    cache.get_or_create('cached', lambda: b'old')
    entered, release = threading.Event(), threading.Event()
    def slow():
        entered.set()
        assert release.wait(2)
        return b'new'
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(cache.get_or_create, 'active', slow)
        assert entered.wait(2)
        with pytest.raises(PreviewBusyError):
            cache.get_or_create('another', lambda: b'excess')
        assert cache.get_or_create('cached', lambda: b'wrong') == b'old'
        release.set()
        assert pending.result() == b'new'
