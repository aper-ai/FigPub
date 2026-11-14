"""Source-pixel fiber geometry, real label imports, nuclei and review replay."""
import hashlib
import threading

import numpy as np
import pytest
from scipy.spatial import ConvexHull

from workbench.fibers import (analyze_fibers, minimum_feret, recompute_fiber_result,
                             skeleton_length, validate_labels)
from workbench.instances import decode_instance_mask


def fixture():
    labels = np.zeros((80, 100), np.uint16)
    labels[10:35, 10:30] = 11
    labels[15:45, 55:85] = 22
    raw = np.where(labels, 120, 4).astype(np.uint16)
    nuclear = np.zeros(labels.shape, np.uint16)
    nuclear[20:23, 18:21] = 1  # central first fiber
    nuclear[12:15, 10:12] = 2  # peripheral first fiber
    nuclear[25:28, 68:71] = 3  # central second fiber
    nuclear[60:63, 10:13] = 4  # outside
    marker = np.where(labels == 11, 200, 10).astype(np.uint16)
    images = {"f": raw, "l": labels, "n": nuclear, "m": marker}
    params = {"method": "fiber_cross_section", "segmentation": "labels", "fiber_channel_id": "f",
        "label_channel_id": "l", "min_area": 5, "nucleus_min_area": 1,
        "channels": [{"id": key, "name": key.upper(), "source": {"asset_id": key, "series": 0, "frame": 0, "channel": 0}}
                     for key in images]}
    return images, params


def test_imported_sparse_ids_preserve_geometry_dtype_and_full_label_mask():
    images, params = fixture()
    for image in images.values():
        image.setflags(write=False)
    hashes = {key: hashlib.sha256(value.tobytes()).hexdigest() for key, value in images.items()}
    result = analyze_fibers(images, params)
    assert result["count"] == 2
    assert [row["id"] for row in result["objects"]] == [11, 22]
    assert [row["area_px"] for row in result["objects"]] == [500, 900]
    assert [row["min_feret_px"] for row in result["objects"]] == [20., 30.]
    np.testing.assert_array_equal(decode_instance_mask(result["instance_mask"]), images["l"])
    assert all(hashlib.sha256(value.tobytes()).hexdigest() == hashes[key] for key, value in images.items())
    assert result["summary"]["nuclei_total"] is None
    assert result["objects"][0]["nuclei_count"] is None


def test_minimum_feret_uses_pixel_squares_and_anisotropic_scale():
    assert minimum_feret(np.ones((1, 8), bool)) == pytest.approx(1)
    assert minimum_feret(np.ones((10, 20), bool)) == pytest.approx(10)
    assert minimum_feret(np.ones((10, 20), bool), spacing=(.5, 2.)) == pytest.approx(10)
    assert minimum_feret(np.ones((1, 1), bool), spacing=(.4, .7)) == pytest.approx(.4)


def test_rotating_calipers_agrees_with_brute_force_hull_supports():
    yy, xx = np.mgrid[:45, :65]
    mask = ((xx - 31) + .4 * (yy - 22)) ** 2 / 500 + ((yy - 22) - .3 * (xx - 31)) ** 2 / 90 < 1
    points = np.argwhere(mask)[:, ::-1].astype(float)
    corners = (points[:, None, :] + np.array([[0, 0], [1, 0], [1, 1], [0, 1]])).reshape(-1, 2)
    hull = corners[ConvexHull(corners).vertices]
    edges = np.roll(hull, -1, axis=0) - hull
    normal = np.column_stack((-edges[:, 1], edges[:, 0]))
    normal /= np.linalg.norm(normal, axis=1)[:, None]
    brute = np.ptp(hull @ normal.T, axis=0).min()
    assert minimum_feret(mask) == pytest.approx(brute, abs=1e-10)


def test_physical_area_and_feret_are_recomputed_with_anisotropy():
    images, params = fixture()
    result = analyze_fibers(images, {**params, "um_per_px_x": .5, "um_per_px_y": 2.})
    first = result["objects"][0]
    assert first["area_um2"] == 500
    assert first["min_feret_um"] == 10
    assert first["equivalent_diameter_um"] == pytest.approx(np.sqrt(4 * 500 / np.pi))


def test_central_peripheral_outside_nuclei_have_explicit_membership():
    images, params = fixture()
    result = analyze_fibers(images, {**params, "nucleus_label_channel_id": "n", "peripheral_fraction": .2})
    classes = {row["id"]: row["classification"] for row in result["nuclei"]}
    assert classes == {1: "central", 2: "peripheral", 3: "central", 4: "outside"}
    assert [item["nuclei_count"] for item in result["objects"]] == [2, 1]
    assert result["summary"]["nuclei_total"] == 4
    assert result["summary"]["nuclei_outside"] == 1
    assert result["summary"]["centrally_nucleated_fiber_fraction"] == 1


def test_ambiguous_nucleus_is_not_arbitrarily_assigned_to_one_fiber():
    images, params = fixture()
    images["l"][10:35, 30:40] = 33
    images["n"][20:23, 28:33] = 5
    result = analyze_fibers(images, {**params, "nucleus_label_channel_id": "n"})
    mixed = next(item for item in result["nuclei"] if item["id"] == 5)
    assert mixed["classification"] == "uncertain"
    assert mixed["fiber_id"] is None
    assert set(mixed["candidate_fiber_ids"]) == {11, 33}
    assert result["summary"]["nuclei_uncertain"] == 1


def test_myotube_fusion_denominator_includes_outside_nuclei():
    images, params = fixture()
    result = analyze_fibers(images, {**params, "method": "myotube", "nucleus_label_channel_id": "n", "fusion_min_nuclei": 2})
    assert result["summary"]["fusion_numerator_nuclei"] == 2
    assert result["summary"]["fusion_denominator_nuclei"] == 4
    assert result["summary"]["fusion_index"] == .5
    assert result["objects"][0]["skeleton_length_px"] > 0


def test_reviewed_split_recomputes_nuclear_assignment_and_fusion():
    images, params = fixture()
    params = {**params, "method": "myotube", "nucleus_label_channel_id": "n"}
    original = analyze_fibers(images, params)
    labels = decode_instance_mask(original["instance_mask"]).copy()
    labels[10:18, 10:30] = 99
    revised = recompute_fiber_result(images, params, labels)
    assert revised["count"] == 3
    assert revised["summary"]["fusion_index"] == 0
    assert next(item for item in revised["nuclei"] if item["id"] == 2)["fiber_id"] == 99
    assert sum(item["area_px"] for item in revised["objects"]) == 1400


def test_marker_types_use_original_mean_and_keep_joint_positive_identity():
    images, params = fixture()
    result = analyze_fibers(images, {**params, "fiber_markers": [{"channel_id": "m", "name": "MYH-A", "threshold": 100},
                                                                {"channel_id": "f", "name": "Pan", "threshold": 50}]})
    assert [item["phenotype"] for item in result["objects"]] == ["MYH-A+Pan", "Pan"]
    assert result["objects"][0]["marker_means"] == {"MYH-A": 200., "Pan": 120.}


def test_roi_origin_and_edge_exclusion_use_original_geometry():
    images, params = fixture()
    roi = {"type": "rectangle", "x": 10, "y": 10, "width": 80, "height": 40}
    result = analyze_fibers(images, {**params, "roi": roi, "exclude_border": True})
    assert result["count"] == 1
    assert result["objects"][0]["id"] == 22
    assert result["objects"][0]["bbox"]["x"] == 55
    mask = decode_instance_mask(result["instance_mask"])
    assert mask.shape == (40, 80)
    with pytest.raises(ValueError, match="原点"):
        recompute_fiber_result(images, {**params, "roi": roi}, mask)


def test_ellipse_roi_never_counts_objects_outside_true_mask():
    images, params = fixture()
    roi = {"type": "ellipse", "x": 45., "y": 5., "width": 50., "height": 50.}
    result = analyze_fibers(images, {**params, "roi": roi})
    assert result["count"] == 1
    assert result["objects"][0]["id"] == 22
    assert result["objects"][0]["area_px"] <= 900


def test_threshold_workflow_is_connected_to_original_source():
    images, params = fixture()
    result = analyze_fibers(images, {**params, "segmentation": "threshold", "threshold_method": "manual", "threshold": 50,
                                     "sigma": 0, "split_touching": False})
    assert result["count"] == 2
    assert [item["area_px"] for item in result["objects"]] == [500, 900]


def test_membrane_workflow_recovers_enclosed_interiors_without_outer_background():
    membrane = np.zeros((60, 80), np.uint16)
    membrane[10:41, 10] = 100
    membrane[10:41, 40] = 100
    membrane[10, 10:41] = 100
    membrane[40, 10:41] = 100
    result = analyze_fibers(membrane, {"method": "fiber_cross_section", "fiber_input": "membrane", "min_area": 20,
        "sigma": 0, "threshold_method": "manual", "threshold": 50, "membrane_close_radius": 0})
    assert result["count"] == 1
    assert result["objects"][0]["area_px"] == 29 * 29


@pytest.mark.parametrize("bad", [np.ones((3, 3), float), np.full((3, 3), -1, np.int16), np.full((3, 3), 2**32, np.uint64)])
def test_invalid_instance_label_types_and_values_are_rejected(bad):
    with pytest.raises(ValueError):
        validate_labels(bad, (3, 3))


def test_wrong_channel_geometry_duplicate_roles_and_cancel_are_rejected():
    images, params = fixture()
    with pytest.raises(ValueError, match="独立"):
        analyze_fibers(images, {**params, "nucleus_channel_id": "f"})
    with pytest.raises(ValueError, match="尺寸"):
        analyze_fibers({**images, "n": images["n"][:20]}, params)
    event = threading.Event();event.set()
    with pytest.raises(ValueError, match="取消"):
        analyze_fibers(images, params, cancel_event=event)


def test_skeleton_length_has_no_double_counted_corner_diagonals():
    line = np.zeros((5, 12), bool)
    line[2, 1:11] = True
    length, pixels = skeleton_length(line)
    assert pixels == 10 and length == 9
    assert skeleton_length(line, spacing=(.4, 1.))[0] == pytest.approx(3.6)
