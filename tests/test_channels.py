"""Scientific channel identity and display/analysis separation regressions."""
import copy
import hashlib
import io
from zipfile import ZipFile

import numpy as np
from PIL import Image
import pytest
import tifffile

from workbench.channels import (FilePlaneReader, auto_range, normalize_view,
                                plane_statistics, render_view, tile_layout,
                                validate_view)
from workbench.imaging import load_image_array
from workbench.models import ChannelView
from workbench.planes import read_raster
from workbench.storage import ProjectStore


def tiff_bytes(array, axes=None, compression=None, photometric='minisblack'):
    output = io.BytesIO()
    tifffile.imwrite(output, array, photometric=photometric, compression=compression,
                     metadata={'axes': axes} if axes else None)
    return output.getvalue()


def source(asset, series=0, frame=0, channel=0):
    return {'asset_id': asset['id'], 'series': series, 'frame': frame, 'channel': channel}


def style(asset, identifier='c0', color='#FFFFFF', **kwargs):
    return {'id': identifier, 'name': identifier, 'color': color,
            'source': source(asset), **kwargs}


@pytest.mark.parametrize('compression', [None, 'deflate', 'lzw'])
def test_tczyx_frames_are_explicit_and_same_in_store_and_export_reader(tmp_path, compression):
    shape = (2, 3, 2, 5, 7)
    original = np.arange(np.prod(shape), dtype=np.uint16).reshape(shape) * 37
    payload = tiff_bytes(original, 'TCZYX', compression)
    store = ProjectStore(tmp_path)
    asset = store.add_files([('acquisition.tif', payload)])[0]
    assert (asset['frames'], asset['channels'], asset['bit_depth']) == (4, 3, 16)
    for t in range(2):
        for z in range(2):
            frame = t * 2 + z
            array = store.read_array(asset['id'], frame=frame)
            assert array.shape == (5, 7, 3) and not array.flags.writeable
            for c in range(3):
                expected = original[t, c, z]
                plane = store.read_plane(source(asset, frame=frame, channel=c))
                np.testing.assert_array_equal(plane, expected)
                np.testing.assert_array_equal(array[..., c], expected)
                np.testing.assert_array_equal(load_image_array(store.resolve(asset['id']), frame=frame, channel=c), expected)
                assert plane.dtype == np.uint16 and not plane.flags.writeable
    descriptor = store.describe_channels(asset['id'], frame=3)
    assert descriptor['frame_axes'] == 'TZ' and descriptor['frame_shape'] == [2, 2]
    assert [(axis['axis'], axis['index']) for axis in descriptor['frame_coordinates']] == [('T', 1), ('Z', 1)]
    assert descriptor['channels'][2]['source'] == source(asset, frame=3, channel=2)
    assert descriptor['channels'][2]['minimum'] == int(original[1, 2, 1].min())
    assert store.resolve(asset['id']).read_bytes() == payload


@pytest.mark.parametrize('compression', [None, 'deflate'])
def test_simultaneous_c_and_rgb_s_axes_keep_every_component(tmp_path, compression):
    original = np.arange(2 * 2 * 5 * 7 * 3, dtype=np.uint16).reshape(2, 2, 5, 7, 3)
    store = ProjectStore(tmp_path)
    asset = store.add_files([('compound.tif', tiff_bytes(original, 'TCYXS', compression, 'rgb'))])[0]
    assert asset['channels'] == 6 and asset['frames'] == 2
    for frame in range(2):
        array = store.read_array(asset['id'], frame=frame)
        assert array.shape == (5, 7, 6)
        for c in range(2):
            for s in range(3):
                plane = store.read_plane(source(asset, frame=frame, channel=c * 3 + s))
                np.testing.assert_array_equal(plane, original[frame, c, :, :, s])
                np.testing.assert_array_equal(array[..., c * 3 + s], plane)
    description = store.describe_channels(asset['id'], frame=1)
    assert description['channels'][4]['logical_channel'] == 1
    assert description['channels'][4]['sample'] == 1
    assert description['channels'][4]['role'] == 'rgb_component'
    assert any('不能据此推定' in warning for warning in description['warnings'])


def test_compressed_scalar_selection_does_not_decode_entire_stack(tmp_path, monkeypatch):
    original = np.arange(2 * 3 * 4 * 6 * 8, dtype=np.uint16).reshape(2, 3, 4, 6, 8)
    path = tmp_path / 'compressed.tif'
    path.write_bytes(tiff_bytes(original, 'TCZYX', 'deflate'))
    def reject_whole_series(*args, **kwargs):
        raise AssertionError('A scalar original plane should read its physical page, not a whole compressed time series.')
    monkeypatch.setattr(tifffile.TiffPageSeries, 'asarray', reject_whole_series)
    np.testing.assert_array_equal(read_raster(path, frame=7, channel=2), original[1, 2, 3])


def test_multiseries_identity_and_dimensions_are_independent(tmp_path):
    first = np.arange(48, dtype=np.uint16).reshape(6, 8)
    second = np.arange(2 * 4 * 5, dtype=np.float32).reshape(2, 4, 5) / 10
    output = io.BytesIO()
    with tifffile.TiffWriter(output) as writer:
        writer.write(first, metadata={'axes': 'YX'}, photometric='minisblack')
        writer.write(second, metadata={'axes': 'CYX'}, photometric='minisblack')
    store = ProjectStore(tmp_path)
    asset = store.add_files([('series.tif', output.getvalue())])[0]
    description = store.describe_channels(asset['id'], series=1)
    assert (description['width_px'], description['height_px'], len(description['series_list'])) == (5, 4, 2)
    assert description['channels'][1]['dtype'] == 'float32'
    np.testing.assert_array_equal(store.read_plane(source(asset, series=1, channel=1)), second[1])
    reader = FilePlaneReader([asset], store.resolve)
    np.testing.assert_array_equal(reader.read_plane(source(asset, series=1, channel=1)), second[1])
    view = {'channels': [style(asset), style(asset, 'second', source=source(asset, series=1))]}
    with pytest.raises(ValueError, match='identical original dimensions'):
        validate_view(view, store)
    for identity in [source(asset, series=2), source(asset, frame=1), source(asset, channel=1)]:
        with pytest.raises(ValueError):
            store.read_plane(identity)


def test_uint16_rgb_png_keeps_source_values_and_default_rgb_appearance(tmp_path):
    import imagecodecs
    original = np.array([[[100, 257, 63000], [1000, 30000, 40000]],
                         [[65000, 62000, 80], [16, 32, 64]]], dtype=np.uint16)
    payload = imagecodecs.png_encode(original)
    store = ProjectStore(tmp_path)
    asset = store.add_files([('rgb16.png', payload)])[0]
    assert asset['bit_depth'] == 16
    np.testing.assert_array_equal(store.read_array(asset['id']), original)
    np.testing.assert_array_equal(store.read_plane(source(asset, channel=1)), original[..., 1])
    description = store.describe_channels(asset['id'])
    view = description['default_view']
    assert all(channel['white'] == 65535 for channel in view['channels'])
    rendered, _ = render_view(store, view)
    np.testing.assert_array_equal(np.array(rendered), np.rint(original.astype(float) / 65535 * 255).astype(np.uint8))
    np.testing.assert_array_equal(np.array(Image.open(io.BytesIO(store.preview(asset['id'])))), np.array(rendered))
    assert hashlib.sha256(store.resolve(asset['id']).read_bytes()).hexdigest() == asset['id']


def test_ome_names_colors_and_unselected_alpha_are_explicit(tmp_path):
    output = io.BytesIO()
    tifffile.imwrite(output, np.arange(3 * 4 * 5, dtype=np.uint16).reshape(3, 4, 5), ome=True,
                     photometric='minisblack', metadata={'axes': 'CYX', 'Channel': {
                         'Name': ['DAPI', 'GFP', 'Marker'], 'Color': [65535, 16711935, -16776961]}})
    store = ProjectStore(tmp_path)
    asset = store.add_files([('named.ome.tif', output.getvalue())])[0]
    description = store.describe_channels(asset['id'])
    assert [item['name'] for item in description['channels']] == ['DAPI', 'GFP', 'Marker']
    assert [item['color'] for item in description['channels']] == ['#0000FF', '#00FF00', '#FF0000']
    rgba = io.BytesIO()
    Image.new('RGBA', (4, 3), (20, 30, 40, 50)).save(rgba, format='PNG')
    rgb = store.add_files([('ordinary.png', rgba.getvalue())])[0]
    description = store.describe_channels(rgb['id'])
    assert [item['name'] for item in description['channels']] == ['Red', 'Green', 'Blue', 'Alpha']
    assert description['channels'][3]['role'] == 'alpha'
    assert description['default_view']['channels'][3]['visible'] is False
    assert all(item['name'] != 'DAPI' for item in description['channels'])


def test_merge_display_luts_and_split_geometry_never_change_originals(tmp_path, monkeypatch):
    a = np.array([[0, 25, 100], [100, 25, 0]], dtype=np.uint16)
    b = np.array([[100, 0, 100], [0, 50, 100]], dtype=np.uint8)
    store = ProjectStore(tmp_path)
    first, second = store.add_files([('a.tif', tiff_bytes(a)), ('b.tif', tiff_bytes(b))])
    view = normalize_view({'channels': [style(first, 'red', '#FF0000', black=0, white=100, gamma=2),
                                         style(second, 'green', '#00FF00', black=0, white=100)]})
    expected = np.stack([np.sqrt(a / 100) * 255, b.astype(float) / 100 * 255, np.zeros(a.shape)], axis=-1)
    # Exercise native mapped accumulator on small fixtures without allocating a
    # huge test image; result must be identical to the normal heap buffer.
    regular, manifest = render_view(store, view)
    import workbench.channels as channels
    monkeypatch.setattr(channels, 'MEMORY_TILE_PIXELS', 1)
    mapped, _ = render_view(store, view)
    np.testing.assert_array_equal(np.array(regular), np.rint(expected).astype(np.uint8))
    np.testing.assert_array_equal(np.array(mapped), np.array(regular))
    assert manifest['source_asset_ids'] == sorted([first['id'], second['id']])
    view['mode'] = 'split'
    view['split'] = {'columns': 2, 'gap_px': 2, 'include_merge': True}
    image, manifest = render_view(store, view)
    assert image.size == (8, 6)
    assert [(t['x_px'], t['y_px']) for t in manifest['tiles']] == [(0, 0), (5, 0), (0, 4)]
    assert manifest['tiles'][0]['source'] == source(first)
    assert manifest['tiles'][2]['channel_ids'] == ['red', 'green']
    np.testing.assert_array_equal(np.array(image)[4:6, :3], np.array(regular))
    assert np.all(np.array(image)[:, 3:5] == 255)
    preview, small_manifest = render_view(store, view, max_size=4)
    assert preview.size == (4, 3) and small_manifest['width_px'] == 8
    assert small_manifest['source_width_px'] == 3
    np.testing.assert_array_equal(store.read_plane(source(first)), a)
    np.testing.assert_array_equal(store.read_plane(source(second)), b)
    assert store.read_plane(source(first)).dtype == np.uint16
    assert store.read_plane(source(second)).dtype == np.uint8


def test_crop_uses_whole_original_levels_and_single_ignores_merge_visibility(tmp_path):
    array = np.array([[0, 100, 200, 1000], [0, 300, 400, 1000]], dtype=np.uint16)
    store = ProjectStore(tmp_path)
    asset = store.add_files([('whole.tif', tiff_bytes(array))])[0]
    view = {'mode': 'single', 'channels': [style(asset, visible=False)]}
    crop = {'x': 1, 'y': 0, 'width': 2, 'height': 2}
    image, manifest = render_view(store, view, crop=crop)
    expected = np.rint(array[:, 1:3] / 1000 * 255).astype(np.uint8)
    np.testing.assert_array_equal(np.array(image)[..., 0], expected)
    assert manifest['levels'][0]['white'] == 1000
    assert manifest['crop'] == crop
    view['mode'] = 'split'
    with pytest.raises(ValueError, match='visible channel'):
        tile_layout(view, 4, 2)
    with pytest.raises(ValueError, match='beyond'):
        render_view(store, {'channels': [style(asset)]}, crop={'x': 3, 'y': 0, 'width': 2, 'height': 1})


def test_same_asset_different_frames_caches_and_localization_use_complete_identity(tmp_path):
    base = np.arange(1200, dtype=np.uint16).reshape(30, 40)
    original = np.stack([np.stack([base, np.flip(base, 0)]),
                         np.stack([np.flip(base, 1), np.flip(base, (0, 1))])])
    store = ProjectStore(tmp_path)
    asset = store.add_files([('time.tif', tiff_bytes(original, 'TCYX'))])[0]
    view = {'channels': [style(asset, 't0', '#FF0000'),
                         style(asset, 't1', '#00FF00', source=source(asset, frame=1))]}
    image, _ = render_view(store, view)
    pixels = np.array(image)
    assert pixels[0, 0, 0] == 0 and pixels[0, 0, 1] > 0
    first = store.localization_array(asset['id'], frame=None, channel=0)
    second = store.localization_array(asset['id'], frame=1, channel=1)
    np.testing.assert_allclose(first['array'], base / base.max(), rtol=1e-6)
    np.testing.assert_allclose(second['array'], original[1, 1] / base.max(), rtol=1e-6)
    assert first is store.localization_array(asset['id'], frame=0, channel=0)
    assert first['source']['frame'] == 0 and second['source']['channel'] == 1
    assert store.preview(asset['id'], frame=0, channel=0) != store.preview(asset['id'], frame=1, channel=1)


def test_multisource_recipe_archive_retains_original_bytes_and_extension_state(tmp_path):
    store = ProjectStore(tmp_path / 'first')
    float_values = np.arange(30, dtype=np.float32).reshape(5, 6) * .05 - 1
    originals = [('u16.tif', tiff_bytes(np.arange(30, dtype=np.uint16).reshape(5, 6))),
                 ('float.tif', tiff_bytes(float_values))]
    first, second = store.add_files(originals)
    view = normalize_view({'channels': [style(first, 'nucleus', '#0000FF'), style(second, 'protein', '#00FF00')]})
    project = store.get_project()
    panel = store.default_panel(first)
    panel['channel_view'] = view
    panel['custom_state'] = {'retained': True}
    project['panels'] = [panel]
    project['settings']['asset_edits'] = {first['id']: {'channel_view': view, 'custom_extension': 'kept'}}
    saved = store.update_project(project)
    archive = store.export_archive()
    with ZipFile(io.BytesIO(archive)) as zipped:
        for asset, (_, payload) in zip([first, second], originals):
            assert zipped.read('originals/' + asset['id'] + '.tif') == payload
    reopened = ProjectStore(tmp_path / 'reopened')
    restored = reopened.open_archive(archive)
    assert restored['panels'][0]['channel_view'] == view
    assert restored['panels'][0]['custom_state']['retained']
    assert restored['settings']['asset_edits'][first['id']]['custom_extension'] == 'kept'
    np.testing.assert_array_equal(reopened.read_plane(source(second)), float_values)
    bad = copy.deepcopy(saved)
    bad['assets'] = [first]
    with pytest.raises(ValueError, match='[Mm]issing.*original'):
        store.update_project(bad)
    assert store.get_project()['revision'] == saved['revision']


@pytest.mark.parametrize('patch', [
    {'color': 'javascript:run'}, {'gamma': 0}, {'black': float('nan')},
    {'black': 2, 'white': 1}, {'visible': 'false'}, {'id': 'bad"id'},
    {'source': {'asset_id': 'a' * 64, 'series': 0, 'frame': -1, 'channel': 0}},
    {'source': {'asset_id': 'a' * 64, 'series': 0, 'frame': 0, 'channel': True}},
])
def test_invalid_channel_fields_are_rejected(patch):
    config = style({'id': 'a' * 64})
    config.update(patch)
    with pytest.raises(ValueError):
        normalize_view({'channels': [config]})


def test_project_rejects_invalid_channel_references_and_split_lane_exclusion(tmp_path):
    store = ProjectStore(tmp_path)
    asset = store.add_files([('one.tif', tiff_bytes(np.arange(20, dtype=np.uint16).reshape(4, 5)))])[0]
    initial = store.get_project()
    for bad_channel in [1, -1]:
        draft = copy.deepcopy(initial)
        panel = store.default_panel(asset)
        panel['channel_view'] = {'channels': [style(asset, source=source(asset, channel=bad_channel))]}
        draft['panels'] = [panel]
        with pytest.raises(ValueError):
            store.update_project(draft)
    draft = copy.deepcopy(initial)
    draft['settings']['asset_edits'] = {asset['id']: {
        'channel_view': {'mode': 'split', 'channels': [style(asset)]}, 'gel': {'hidden_lanes': [0]}}}
    with pytest.raises(ValueError, match='Split channel'):
        store.update_project(draft)
    assert store.get_project() == initial
    with pytest.raises(ValueError, match='unique'):
        ChannelView.model_validate({'channels': [style(asset), style(asset)]})


def test_percentile_sampling_is_explicit_and_updates_only_requested_channel(tmp_path):
    original = np.arange(700 * 800, dtype=np.float32).reshape(700, 800)
    store = ProjectStore(tmp_path)
    asset = store.add_files([('large.tif', tiff_bytes(original))])[0]
    view = {'channels': [style(asset, 'a'), style(asset, 'b', black=5, white=500)]}
    result = auto_range(store, view, (1, 99), channel_ids=['a'])
    first, second = result['channel_view']['channels']
    assert first['black'] > 0 and first['white'] < original.max()
    assert second['black'] == 5 and second['white'] == 500
    stats = result['levels'][0]
    assert stats['sampled'] and stats['sample_count'] <= 262144
    assert stats['finite_count'] == original.size and stats['sampling'] == 'deterministic_flat_stride'
    np.testing.assert_array_equal(store.read_plane(source(asset)), original)
    assert 'black' not in view['channels'][0]
    with pytest.raises(ValueError):
        auto_range(store, view, (99, 1))
    with pytest.raises(ValueError):
        auto_range(store, view, channel_ids=['missing'])
    floats = np.array([[np.nan, 1, np.inf], [-np.inf, 3, 2]], np.float32)
    assert plane_statistics(floats)['finite_count'] == 3


def test_more_than_32_original_channels_are_reported_without_silent_loss(tmp_path):
    original = np.arange(35 * 2 * 3, dtype=np.uint16).reshape(35, 2, 3)
    store = ProjectStore(tmp_path)
    asset = store.add_files([('many.tif', tiff_bytes(original, 'CYX'))])[0]
    first = store.describe_channels(asset['id'])
    assert first['total_channels'] == 35 and first['returned_channels'] == 32 and first['truncated']
    assert first['warnings']
    chosen = store.describe_channels(asset['id'], channel_indices=[34])
    assert chosen['channels'][0]['source']['channel'] == 34
    np.testing.assert_array_equal(store.read_plane(chosen['channels'][0]['source']), original[34])
    with pytest.raises(ValueError):
        store.describe_channels(asset['id'], channel_indices=list(range(33)))


def test_demo_real_three_channel_tiff_keeps_original_nine_nuclei_and_counts(tmp_path):
    from workbench.microscopy import analyze_microscopy
    store = ProjectStore(tmp_path)
    project = store.demo()
    assert len(project['assets']) == 3 and len(project['panels']) == 2
    assert project['settings']['synthetic_demo'] is True
    asset = project['assets'][1]
    descriptor = store.describe_channels(asset['id'])
    assert descriptor['total_channels'] == 3 and asset['dtype'] == 'uint16'
    assert [item['color'] for item in descriptor['channels']] == ['#0000FF', '#00FF00', '#FF0000']
    assert all(item['name'].startswith('SYNTHETIC') for item in descriptor['channels'])
    yy, xx = np.mgrid[:360, :480]
    expected = np.full((360, 480), 400.0)
    for cx, cy, radius in [(50,60,10),(100,140,12),(180,75,13),(260,90,12),(380,100,14),(330,220,11),(200,240,14),(100,280,10),(420,290,12)]:
        expected += 30000 * np.exp(-((xx-cx)**2+(yy-cy)**2)/(2*radius**2))
    plane = store.read_plane(source(asset))
    np.testing.assert_array_equal(plane, expected.astype(np.uint16))
    assert analyze_microscopy(plane, {'method': 'nuclei', 'min_area': 20})['count'] == 9
    descriptor['default_view']['mode'] = 'split'
    image, manifest = render_view(store, descriptor['default_view'], max_size=800)
    assert len(manifest['tiles']) == 3 and max(image.size) <= 800


def test_channel_recipe_cannot_be_silently_ignored_on_a_vector_asset(tmp_path):
    store = ProjectStore(tmp_path)
    raster, vector = store.add_files([
        ('raw.tif', tiff_bytes(np.arange(12, dtype=np.uint16).reshape(3, 4))),
        ('vector.svg', b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="30"><rect width="40" height="30" fill="red"/></svg>')])
    project = store.get_project()
    panel = store.default_panel(vector)
    panel['channel_view'] = {'channels': [style(raster)]}
    project['panels'] = [panel]
    with pytest.raises(ValueError, match='attached to an original raster'):
        store.update_project(project)
