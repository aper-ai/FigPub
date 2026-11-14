"""Source-grid invariants and malformed-input checks for human instance review."""
import base64
import copy
import io
import json
import threading
import time
import zlib

import numpy as np
import pytest
import tifffile

from workbench import instances
from workbench.instances import (attach_instance_mask, decode_instance_mask, encode_instance_mask,
                                 label_tiff, review_instances)
from workbench.microscopy import analyze_microscopy


def base(labels, *, origin=(0, 0), source_shape=None):
    return {"kind": "microscopy", "count": len(np.unique(labels[labels > 0])),
            "method": {"name": "nuclei", "measurement_basis": "original_pixels", "display_lut_used": False},
            "summary": {"foreground_area_px": int(np.count_nonzero(labels)), "object_type": "nucleus_candidate"},
            "objects": [], "warnings": [],
            "instance_mask": encode_instance_mask(labels, origin=origin, source_shape=source_shape)}


def operation(kind, identifier="edit_1", **fields):
    return {"id": identifier, "type": kind, **fields}


@pytest.fixture
def sample():
    raw = np.arange(24 * 32, dtype=np.uint16).reshape(24, 32)
    labels = np.zeros(raw.shape, dtype=np.uint32)
    labels[4:18, 5:24] = 1
    return raw, labels, base(labels)


def test_lossless_mask_preserves_holes_sparse_ids_and_origin():
    labels = np.zeros((20, 30), dtype=np.uint32)
    labels[2:15, 3:13] = 90001
    labels[5:7, 6:8] = 0
    payload = encode_instance_mask(labels, origin=(7, 13), source_shape=(60, 80))
    decoded = decode_instance_mask(payload)
    np.testing.assert_array_equal(decoded, labels)
    assert decoded.dtype == np.dtype("<u4")
    assert not decoded.flags.writeable
    assert payload["origin"] == [7, 13]
    assert encode_instance_mask(labels, origin=(8, 13), source_shape=(60, 80))["sha256"] != payload["sha256"]


@pytest.mark.parametrize("change", [
    {"origin": [1, 0], "source_shape": [24, 33]}, {"sha256": "0" * 64},
    {"nbytes": 1}, {"shape": [16_000_001, 1]}, {"encoding": "pickle"},
    {"data": "not-base64"}, {"source_shape": [23, 32]},
])
def test_mask_rejects_corruption_or_geometry_substitution(sample, change):
    payload = {**sample[2]["instance_mask"], **change}
    with pytest.raises(ValueError):
        decode_instance_mask(payload)


def test_mask_rejects_trailing_compression_and_decompression_bomb(sample):
    payload = sample[2]["instance_mask"]
    for compressed in (base64.b64decode(payload["data"]) + zlib.compress(b"extra"), zlib.compress(b"x" * (payload["nbytes"] + 10000))):
        changed = {**payload, "data": base64.b64encode(compressed).decode("ascii")}
        with pytest.raises(ValueError):
            decode_instance_mask(changed)


@pytest.mark.parametrize("labels", [np.array([[1.2]]), np.array([[-1]], dtype=np.int32), np.array([[1000001]], dtype=np.uint32), np.zeros((0, 2), dtype=np.uint32)])
def test_codec_rejects_noninteger_negative_or_unbounded_labels(labels):
    with pytest.raises(ValueError):
        encode_instance_mask(labels)


def test_large_editor_payload_keeps_scientific_result_with_explicit_warning(monkeypatch):
    monkeypatch.setattr(instances, "MAX_COMPRESSED_BYTES", 1)
    result = {"count": 1, "summary": {"area": 100}, "warnings": []}
    attach_instance_mask(result, np.ones((10, 10), dtype=np.uint32))
    assert result["count"] == 1 and result["summary"] == {"area": 100}
    assert "instance_mask" not in result
    assert "instance_review_unavailable" in result
    assert result["warnings"]


def test_two_seed_split_preserves_foreground_and_exact_raw_integral(sample):
    raw, labels, initial = sample
    snapshot = raw.tobytes()
    revised = review_instances(initial, raw, [operation("split", instance_id=1, seeds=[[7, 7], [20, 14]])], params={"method": "nuclei", "um_per_px": 0.5})
    output = decode_instance_mask(revised["instance_mask"])
    np.testing.assert_array_equal(output > 0, labels > 0)
    assert revised["count"] == 2
    assert sum(row["area_px"] for row in revised["objects"]) == np.count_nonzero(labels)
    assert sum(row["integrated_intensity"] for row in revised["objects"]) == int(raw[labels > 0].sum())
    assert sum(row["area_um2"] for row in revised["objects"]) == np.count_nonzero(labels) * .25
    assert revised["instance_review"]["original_count"] == 1
    assert raw.tobytes() == snapshot
    np.testing.assert_array_equal(decode_instance_mask(initial["instance_mask"]), labels)


def test_split_merge_undo_redo_are_deterministic_and_original_is_immutable(sample):
    raw, labels, initial = sample
    original = copy.deepcopy(initial)
    operations = [operation("split", "s", instance_id=1, seeds=[[7, 7], [20, 14]]), operation("merge", "m", ids=[1, 2])]
    split = review_instances(initial, raw, operations, cursor=1)
    combined = review_instances(initial, raw, operations, cursor=2)
    undone = review_instances(initial, raw, operations, cursor=0)
    redone = review_instances(initial, raw, operations, cursor=1)
    assert split["count"] == 2 and combined["count"] == 1 and undone["count"] == 1
    assert split["instance_mask"] == redone["instance_mask"]
    assert initial == original
    np.testing.assert_array_equal(decode_instance_mask(combined["instance_mask"]), labels)
    assert undone["instance_mask"]["sha256"] == initial["instance_mask"]["sha256"]


def test_manual_add_delete_do_not_recycle_instance_ids(sample):
    raw, _, initial = sample
    operations = [operation("add", "add", region={"type": "rectangle", "x": 27, "y": 3, "width": 3, "height": 4}),
                  operation("delete", "delete", ids=[2]),
                  operation("add", "again", region={"type": "rectangle", "x": 27, "y": 3, "width": 3, "height": 4})]
    result = review_instances(initial, raw, operations)
    assert set(np.unique(decode_instance_mask(result["instance_mask"]))) == {0, 1, 3}
    assert result["instance_review"]["operations"][-1]["output_ids"] == [3]
    empty = review_instances(initial, raw, [operation("delete", ids=[1])])
    assert empty["count"] == 0 and empty["objects"] == []


def test_merge_rejects_separated_objects_unless_explicitly_allowed():
    labels = np.zeros((12, 18), dtype=np.uint32)
    labels[2:5, 2:5] = 1
    labels[6:9, 10:13] = 2
    raw = np.arange(labels.size, dtype=np.uint16).reshape(labels.shape)
    initial = base(labels)
    with pytest.raises(ValueError, match="不相邻"):
        review_instances(initial, raw, [operation("merge", ids=[1, 2])])
    result = review_instances(initial, raw, [operation("merge", ids=[1, 2], allow_disconnected=True)])
    assert result["count"] == 1
    np.testing.assert_array_equal(decode_instance_mask(result["instance_mask"]) > 0, labels > 0)
    assert result["instance_review"]["operations"][0]["disconnected_components"] == 2


def test_roi_coordinates_and_native_mask_roundtrip():
    raw = np.arange(40 * 50, dtype=np.uint16).reshape(40, 50)
    labels = np.zeros((12, 16), dtype=np.uint32)
    labels[2:10, 3:13] = 5
    initial = base(labels, origin=(10, 20), source_shape=raw.shape)
    params = {"roi": {"x": 10, "y": 20, "width": 16, "height": 12}}
    result = review_instances(initial, raw, [operation("split", instance_id=5, seeds=[[14, 23], [21, 28]])], params=params)
    assert result["count"] == 2
    assert all(row["bbox"]["x"] >= 10 and row["bbox"]["y"] >= 20 for row in result["objects"])
    assert sum(row["integrated_intensity"] for row in result["objects"]) == int(raw[20:32, 10:26][labels > 0].sum())
    stream = label_tiff(result)
    with tifffile.TiffFile(io.BytesIO(stream)) as file:
        np.testing.assert_array_equal(file.asarray(), decode_instance_mask(result["instance_mask"]))
        assert file.asarray().dtype == np.uint32
        metadata = json.loads(file.pages[0].description)
        assert metadata["roi_origin_xy"] == [10, 20]
        assert metadata["source_shape_yx"] == [40, 50]
        assert metadata["instance_review"]["cursor"] == 1


@pytest.mark.parametrize("region", [
    {"type": "rectangle", "x": 4, "y": 4, "width": 5, "height": 5},
    {"type": "rectangle", "x": -1, "y": 4, "width": 1, "height": 1},
    {"type": "rectangle", "x": 31, "y": 3, "width": 2, "height": 3},
    {"type": "polygon", "points": [[1, 1], [4, 4], [1, 4], [4, 1]]},
    {"type": "polygon", "points": [[1, 1], [2, 2], [3, 3]]},
])
def test_invalid_or_overlapping_manual_regions_fail_without_modifying_base(sample, region):
    raw, labels, initial = sample
    with pytest.raises(ValueError):
        review_instances(initial, raw, [operation("add", region=region)])
    np.testing.assert_array_equal(decode_instance_mask(initial["instance_mask"]), labels)


def test_manual_polygon_is_a_real_pixel_mask_not_its_bounding_box():
    raw = np.arange(100, dtype=np.uint16).reshape(10, 10)
    initial = base(np.zeros(raw.shape, dtype=np.uint32))
    result = review_instances(initial, raw, [operation("add", region={"type": "polygon", "points": [[2, 2], [7, 2], [2, 7]]})])
    labels = decode_instance_mask(result["instance_mask"])
    assert result["count"] == 1 and 0 < np.count_nonzero(labels) < 25
    assert result["objects"][0]["integrated_intensity"] == int(raw[labels > 0].sum())


def test_manual_add_respects_non_rectangular_roi_and_finite_values():
    raw = np.ones((20, 20), dtype=float)
    params = {"roi": {"type": "ellipse", "x": 2, "y": 2, "width": 16, "height": 16}}
    initial = base(np.zeros((16, 16), dtype=np.uint32), origin=(2, 2), source_shape=raw.shape)
    with pytest.raises(ValueError, match="ROI"):
        review_instances(initial, raw, [operation("add", region={"type": "rectangle", "x": 2, "y": 2, "width": 3, "height": 3})], params=params)
    raw[9, 9] = np.nan
    with pytest.raises(ValueError, match="无效"):
        review_instances(initial, raw, [operation("add", region={"type": "rectangle", "x": 8, "y": 8, "width": 3, "height": 3})], params=params)


@pytest.mark.parametrize("edit", [
    operation("split", instance_id=1, seeds=[[7, 7], [7, 7]]),
    operation("split", instance_id=1, seeds=[[7, 7], [-1, 7]]),
    operation("split", instance_id=1, seeds=[[7, 7], [29, 22]]),
    operation("split", instance_id=99, seeds=[[7, 7], [20, 10]]),
    operation("merge", ids=[1, 1]), operation("delete", ids=[999]),
    operation("delete", ids=[1], unsupported=True),
])
def test_invalid_edit_never_changes_source_or_original_mask(sample, edit):
    raw, labels, initial = sample
    snapshot = raw.tobytes()
    with pytest.raises(ValueError):
        review_instances(initial, raw, [edit])
    assert raw.tobytes() == snapshot
    np.testing.assert_array_equal(decode_instance_mask(initial["instance_mask"]), labels)


def test_operation_replay_rejects_duplicate_ids_invalid_cursor_and_nested_base(sample):
    raw, _, initial = sample
    step = operation("delete", ids=[1])
    for steps, cursor in [([step, step], 0), ([step], True), ([step], 2), ([step] * 201, 0)]:
        with pytest.raises(ValueError):
            review_instances(initial, raw, steps, cursor)
    result = review_instances(initial, raw, [])
    with pytest.raises(ValueError, match="原始分割"):
        review_instances(result, raw, [])


def test_cancel_stops_before_edit_and_measurement(sample):
    event = threading.Event()
    event.set()
    with pytest.raises(ValueError, match="取消"):
        review_instances(sample[2], sample[0], [], cancel_event=event)


def test_classical_segmentation_persists_native_instances_and_can_be_reviewed():
    raw = np.zeros((30, 40), dtype=np.uint16)
    raw[4:15, 5:16] = 1000
    raw[18:25, 24:32] = 2000
    snapshot = raw.copy()
    params = {"method": "nuclei", "threshold": 100, "sigma": 0, "split_touching": False, "min_area": 1}
    result = analyze_microscopy(raw, params)
    assert result["count"] == 2 and "instance_mask" in result
    assert np.count_nonzero(decode_instance_mask(result["instance_mask"])) == 177
    reviewed = review_instances(result, raw, [operation("delete", ids=[1])], params=params)
    assert reviewed["count"] == 1 and reviewed["objects"][0]["area_px"] == 56
    np.testing.assert_array_equal(raw, snapshot)


def test_nonrectangular_intensity_summary_excludes_bbox_corners():
    from workbench.microscopy_measurements import analysis_region
    raw = np.arange(400, dtype=np.uint16).reshape(20, 20)
    geometry = {"type": "ellipse", "x": 3, "y": 4, "width": 12, "height": 10}
    selected = analysis_region(geometry, raw.shape)
    result = analyze_microscopy(raw, {"method": "intensity", "roi": geometry})
    expected = raw[selected.bounds.slices][selected.mask]
    assert result["summary"]["pixel_count"] == expected.size
    assert result["summary"]["integrated_intensity"] == int(expected.sum())


def test_myotube_split_recomputes_nuclear_assignment_fusion_and_geometry():
    labels = np.zeros((40, 60), dtype=np.uint16)
    labels[10:30, 5:55] = 7
    raw = np.where(labels, 200, 10).astype(np.uint16)
    nuclei = np.zeros(labels.shape, dtype=np.uint16)
    nuclei[18:21, 15:18] = 1
    nuclei[18:21, 42:45] = 2
    images = {"f": raw, "l": labels, "n": nuclei}
    snapshots = {key: value.tobytes() for key, value in images.items()}
    params = {"method": "myotube", "segmentation": "labels", "fiber_channel_id": "f", "label_channel_id": "l",
              "nucleus_label_channel_id": "n", "nucleus_min_area": 1, "min_area": 1,
              "channels": [{"id": key, "name": key, "source": {"asset_id": key, "series": 0, "frame": 0, "channel": 0}}
                           for key in images]}
    initial = analyze_microscopy(images, params)
    assert initial["count"] == 1 and initial["summary"]["fusion_index"] == 1
    result = review_instances(initial, images, [operation("split", instance_id=7, seeds=[[15, 20], [45, 20]])], params=params)
    assert result["count"] == 2 and result["summary"]["fusion_index"] == 0
    assert result["summary"]["nuclei_total"] == 2
    assert sorted(item["nuclei_count"] for item in result["objects"]) == [1, 1]
    assert sum(item["area_px"] for item in result["objects"]) == 1000
    assert all("min_feret_px" in item and "skeleton_length_px" in item for item in result["objects"])
    assert all(value.tobytes() == snapshots[key] for key, value in images.items())


def test_imported_fiber_geometry_survives_missing_intensity_samples():
    labels = np.zeros((12, 18), dtype=np.uint16)
    labels[2:10, 3:15] = 7
    raw = np.ones(labels.shape, dtype=float) * 100
    raw[4:6, 5:7] = np.nan
    images = {'f': raw, 'l': labels}
    params = {'method': 'fiber_cross_section', 'segmentation': 'labels', 'fiber_channel_id': 'f',
              'label_channel_id': 'l', 'min_area': 1,
              'channels': [{'id': key, 'source': {'asset_id': key, 'series': 0, 'frame': 0, 'channel': 0}}
                           for key in images]}
    initial = analyze_microscopy(images, params)
    result = review_instances(initial, images, [], params=params)
    assert result['objects'][0]['area_px'] == 96
    assert result['objects'][0]['valid_intensity_pixels'] == 92
    assert result['objects'][0]['mean_intensity'] == 100
    np.testing.assert_array_equal(decode_instance_mask(result['instance_mask']), labels)


def test_real_api_review_is_readonly_revision_bound_and_exports_labels(tmp_path):
    from fastapi.testclient import TestClient
    from workbench.server import create_app

    raw = np.zeros((30, 40), dtype=np.uint16)
    raw[5:25, 6:34] = 1000
    stream = io.BytesIO()
    tifffile.imwrite(stream, raw)
    with TestClient(create_app(tmp_path)) as client:
        uploaded = client.post('/api/assets', files={'files': ('SYNTHETIC_review.tif', stream.getvalue(), 'image/tiff')})
        assert uploaded.status_code == 200
        asset_id = uploaded.json()['assets'][0]['id']
        response = client.post('/api/jobs', json={'kind': 'microscopy', 'params': {
            'method': 'nuclei', 'asset_id': asset_id, 'threshold': 100, 'sigma': 0, 'split_touching': False, 'min_area': 1}})
        assert response.status_code == 200
        job = response.json()
        for _ in range(100):
            job = client.get(f"/api/jobs/{job['id']}").json()
            if job['status'] in {'done', 'error'}:
                break
            time.sleep(.01)
        assert job['status'] == 'done', job
        project = client.get('/api/project').json()
        record = project['measurements'][-1]
        request = {'project_id': project['id'], 'revision': project['revision'], 'measurement_id': record['id'],
                   'base_mask_sha256': record['result']['instance_mask']['sha256'], 'cursor': 1,
                   'operations': [operation('split', instance_id=1, seeds=[[12, 15], [28, 15]])]}
        reviewed = client.post('/api/instances/review', json=request)
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()['count'] == 2
        assert client.get('/api/project').json() == project
        stale = client.post('/api/instances/review', json={**request, 'revision': project['revision'] - 1})
        assert stale.status_code == 409
        changed = client.post('/api/instances/review', json={**request, 'base_mask_sha256': '0' * 64})
        assert changed.status_code == 409
        exported = client.post('/api/instances/export', json=request)
        assert exported.status_code == 200
        np.testing.assert_array_equal(tifffile.imread(io.BytesIO(exported.content)), decode_instance_mask(reviewed.json()['instance_mask']))
