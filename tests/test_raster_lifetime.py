"""TIFF results must remain valid without retaining an OS mapping or source lock."""
from io import BytesIO
import threading

from fastapi.testclient import TestClient
import numpy as np
import pytest
import tifffile

from workbench.planes import read_raster
from workbench.server import create_app


@pytest.mark.parametrize('selection', ['gray', 'scalar', 'multichannel', 'rgb'])
def test_mapped_tiff_reads_close_mapping_while_selected_pixels_remain_exact(tmp_path, monkeypatch, selection):
    if selection == 'gray':
        original = np.arange(63, dtype=np.uint16).reshape(7, 9)
        axes, options, expected = 'YX', {}, original
    elif selection == 'rgb':
        original = np.arange(7 * 9 * 3, dtype=np.uint16).reshape(7, 9, 3)
        axes, options, expected = 'YXS', {}, original
    else:
        original = np.arange(2 * 3 * 4 * 7 * 9, dtype=np.uint16).reshape(2, 3, 4, 7, 9)
        axes = 'TCZYX'
        options = {'frame': 7, **({'channel': 2} if selection == 'scalar' else {})}
        expected = original[1, 2, 3] if selection == 'scalar' else np.stack(original[1, :, 3], axis=-1)
    path = tmp_path / 'original.tif'
    tifffile.imwrite(path, original, metadata={'axes': axes}, photometric='rgb' if selection == 'rgb' else 'minisblack')
    mappings, real_memmap = [], tifffile.memmap

    def tracked_memmap(*args, **kwargs):
        mapping = real_memmap(*args, **kwargs)
        mappings.append(mapping)
        return mapping

    monkeypatch.setattr(tifffile, 'memmap', tracked_memmap)
    decoded = read_raster(path, **options)
    assert mappings and all(mapping._mmap.closed for mapping in mappings)
    assert decoded.flags.c_contiguous and not decoded.flags.writeable
    assert decoded.nbytes == expected.nbytes
    path.unlink()  # Windows fails here if any mapping still retains the file.
    np.testing.assert_array_equal(decoded, expected)


def test_invalid_tiff_channel_also_closes_mapping(tmp_path, monkeypatch):
    path = tmp_path / 'source.tif'
    tifffile.imwrite(path, np.arange(63, dtype=np.uint16).reshape(7, 9), photometric='minisblack')
    mappings, real_memmap = [], tifffile.memmap

    def tracked_memmap(*args, **kwargs):
        mapped = real_memmap(*args, **kwargs)
        mappings.append(mapped)
        return mapped

    monkeypatch.setattr(tifffile, 'memmap', tracked_memmap)
    with pytest.raises(ValueError, match='channel is outside'):
        read_raster(path, channel=1)
    assert mappings and all(mapping._mmap.closed for mapping in mappings)
    path.unlink()


def test_app_shutdown_waits_for_worker_cleanup_then_releases_decoded_caches(tmp_path, monkeypatch):
    started, finished = threading.Event(), threading.Event()

    def runner(kind, params):
        started.set()
        job = next(iter(app.state.jobs.jobs.values()))
        assert job['_cancel'].wait(3), 'Application shutdown must request cancellation'
        # A running operation can finish a read at its next cancellation boundary.
        app.state.store.read_plane({'asset_id': params['asset_id']})
        finished.set()
        return {'kind': kind, 'rows': []}

    app = create_app(tmp_path, runner=runner)
    store = app.state.store
    clear = store.clear_caches

    def clear_after_workers():
        assert finished.is_set(), 'The worker must finish before cache disposal'
        clear()

    monkeypatch.setattr(store, 'clear_caches', clear_after_workers)
    original = np.arange(7 * 9, dtype=np.uint16).reshape(7, 9)
    data = BytesIO()
    tifffile.imwrite(data, original, photometric='minisblack')
    with TestClient(app) as client:
        uploaded = client.post('/api/assets', files={'files': ('original.tif', data.getvalue(), 'image/tiff')})
        assert uploaded.status_code == 200
        asset = uploaded.json()['assets'][0]
        retained_array = store.read_array(asset['id'])
        assert client.get(asset['preview_url']).status_code == 200
        assert store.array_cache.bytes and store.preview_cache.bytes
        job = client.post('/api/jobs', json={'kind': 'microscopy', 'params': {'asset_id': asset['id']}})
        assert job.status_code == 200 and started.wait(1)
    assert finished.is_set()
    assert app.state.jobs.closed
    assert not app.state.jobs.get(job.json()['id'])['cleanup_pending']
    assert not store.get_project()['measurements']
    for cache in (store.array_cache, store.preview_cache, store.table_cache, store.localization_cache):
        assert cache.bytes == 0 and not cache.items
    store.resolve(asset['id']).unlink()
    np.testing.assert_array_equal(retained_array, original)
