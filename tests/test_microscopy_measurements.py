"""Numerical contracts for new microscopy measurements, using known fixtures."""
from __future__ import annotations

import copy
import json

import numpy as np
import pytest

from workbench.microscopy import analyze_microscopy
from workbench.microscopy_measurements import analysis_region, resolve_channels


def channel(identifier, asset="source", frame=0, channel_index=0, **extra):
    return {"id": identifier, "name": identifier, "source": {"asset_id": asset, "series": 0,
            "frame": frame, "channel": channel_index}, **extra}


def call(images, method, channels=None, **params):
    return analyze_microscopy(images, {"method": method, **({"channels": channels} if channels else {}), **params})


def test_mixed_dtype_and_distinct_frames_keep_complete_identity():
    first = np.full((8, 9), 41001, np.uint16)
    second = np.full((8, 9), .125, np.float32)
    inputs = {"first": first, "second": second}
    chosen = [channel("first", frame=1, channel_index=3), channel("second", frame=2, channel_index=3)]
    result = call(inputs, "channel_intensity", chosen)
    assert [row["raw_mean"] for row in result["rows"]] == [41001, .125]
    assert [row["source_dtype"] for row in result["rows"]] == ["uint16", "float32"]
    assert [row["source"]["frame"] for row in result["rows"]] == [1, 2]
    assert [row["source"]["channel"] for row in result["rows"]] == [3, 3]
    assert result["rows"][0]["raw_integrated"] == 41001 * 72
    assert first.dtype == np.uint16 and second.dtype == np.float32
    resolved = resolve_channels(inputs, {"channels": chosen})
    assert np.shares_memory(resolved[0].values, first)


def test_scalar_plane_does_not_rewrite_original_channel_identity():
    source = {"asset_id": "same_asset", "series": 1, "frame": 4, "channel": 6}
    result = call(np.ones((4, 5), np.uint16), "channel_intensity", source=source, channel=0)
    assert result["rows"][0]["source"] == source


def test_display_colors_visibility_and_gamma_do_not_change_measurements():
    values = np.arange(30, dtype=np.uint16).reshape(5, 6) + 1000
    before = values.copy()
    descriptors = [channel("signal")]
    a = call({"signal": values}, "channel_intensity", descriptors)
    changed = copy.deepcopy(descriptors)
    changed[0].update(visible=False, color="#ff0000", gamma=5, low=1200, high=1400)
    b = call({"signal": values}, "channel_intensity", changed, display={"low": 0, "high": 1})
    assert a["rows"] == b["rows"]
    assert a["method"]["display_lut_used"] is False
    assert np.array_equal(values, before)


def test_ellipse_and_polygon_masks_use_source_pixel_centers():
    ellipse = analysis_region({"type": "ellipse", "x": 10, "y": 20, "width": 4, "height": 4}, (40, 40))
    assert ellipse.area_px == 12
    polygon = analysis_region({"type": "polygon", "points": [[10, 20], [14, 20], [14, 24], [10, 24]]}, (40, 40))
    assert polygon.area_px == 16
    assert polygon.bounds.to_dict() == {"x": 10, "y": 20, "width": 4, "height": 4}
    image = np.arange(1600, dtype=np.uint16).reshape(40, 40)
    result = call(image, "channel_intensity", roi=polygon.geometry)
    assert result["rows"][0]["raw_integrated"] == image[20:24, 10:14].sum()
    with pytest.raises(ValueError, match="边界"):
        analysis_region({"type": "polygon", "points": [[0, 0], [100, 0], [0, 10]]}, (40, 40))


def test_fractional_ellipse_geometry_round_trips_without_expanding_the_mask():
    region = analysis_region({"type": "ellipse", "x": 2.8, "y": 3.2, "width": 6.1, "height": 4.4}, (20, 20))
    replay = analysis_region(region.geometry, (20, 20))
    assert np.array_equal(region.mask, replay.mask)
    assert region.geometry["x"] == 2.8 and region.geometry["width"] == 6.1


def test_classical_border_exclusion_follows_polygon_edge_not_bounding_box():
    image = np.zeros((40, 40), np.uint16)
    image[13:18, 13:18] = 1000
    params = {"method": "nuclei", "threshold": 100, "sigma": 0, "min_area": 1, "split_touching": False,
              "roi": {"type": "polygon", "points": [[5, 5], [25, 5], [5, 25]]}}
    normal = analyze_microscopy(image, params)
    excluded = analyze_microscopy(image, {**params, "exclude_border": True})
    assert normal["count"] == 1 and normal["objects"][0]["touches_roi_border"]
    assert excluded["count"] == 0


def test_background_roi_and_fixed_values_preserve_signed_results():
    a = np.full((12, 12), 1000, np.uint16)
    a[4:8, 4:8] = 950
    b = np.full((12, 12), 2000, np.uint16)
    b[4:8, 4:8] = 2201
    inputs, chosen = {"a": a, "b": b}, [channel("a"), channel("b")]
    roi = {"x": 4, "y": 4, "width": 4, "height": 4}
    result = call(inputs, "channel_intensity", chosen, roi=roi, background_method="roi",
                  background_roi={"x": 0, "y": 0, "width": 3, "height": 3})
    assert result["rows"][0]["background_mean"] == 1000
    assert result["rows"][0]["corrected_integrated"] == -800
    assert result["rows"][1]["corrected_integrated"] == 201 * 16
    fixed = call(inputs, "channel_intensity", chosen, roi=roi, background_method="fixed", background_values={"a": 1000, "b": 2000})
    assert [row["corrected_integrated"] for row in fixed["rows"]] == [-800, 3216]
    with pytest.raises(ValueError, match="固定背景"):
        call(inputs, "channel_intensity", chosen, background_method="fixed", background_values={"a": 0})


def test_channel_specific_background_override_and_nonfinite_background():
    values = np.full((5, 5), 10.0)
    descriptors = [channel("a", background={"method": "fixed", "value": 12})]
    result = call({"a": values}, "channel_intensity", descriptors)
    assert result["rows"][0]["corrected_mean"] == -2
    values[:2, :2] = np.nan
    with pytest.raises(ValueError, match="背景 ROI 没有有限"):
        call({"a": values}, "channel_intensity", [channel("a")], background_method="roi",
             background_roi={"x": 0, "y": 0, "width": 2, "height": 2})


@pytest.mark.parametrize("method", ["otsu", "triangle", "li"])
def test_real_automatic_thresholds_report_source_scale(method):
    image = np.full((30, 30), 1000, np.uint16)
    image[8:22, 8:22] = 50000
    result = call(image, "threshold", threshold_method=method, include_contours=False)
    row = result["rows"][0]
    assert 999 <= row["threshold"] < 50000
    assert row["positive_area_px"] == 14 * 14
    assert result["count"] == 1
    assert result["objects"][0]["area_px"] == 196
    assert "contour" not in result["objects"][0]


def test_manual_threshold_keeps_one_uint16_level_difference():
    image = np.full((8, 8), 41000, np.uint16)
    image[2:6, 2:6] = 41001
    result = call(image, "threshold", threshold_method="manual", threshold=41000)
    assert result["rows"][0]["positive_area_px"] == 16
    assert result["rows"][0]["positive_fraction"] == .25
    with pytest.raises(ValueError, match="未填写"):
        call(image, "threshold", threshold_method="manual")


def test_constant_and_all_invalid_images_return_defined_empty_results():
    constant = call(np.full((4, 5), 2000, np.uint16), "threshold")
    assert constant["rows"][0]["positive_area_px"] == 0
    assert constant["count"] == 0
    assert constant["method"]["thresholds"]["c0"]["status"] == "constant_image_no_separation"
    invalid = call(np.full((4, 5), np.nan), "threshold")
    assert invalid["rows"][0]["raw_mean"] is None
    assert invalid["rows"][0]["threshold"] is None
    assert invalid["rows"][0]["positive_fraction"] is None
    json.dumps(invalid, allow_nan=False)


def test_threshold_contours_and_centroids_use_original_coordinates():
    image = np.zeros((60, 70), np.uint16)
    image[32:37, 24:29] = 5000
    result = call(image, "threshold", roi={"x": 20, "y": 30, "width": 20, "height": 20},
                  threshold_method="manual", threshold=1000, um_per_px=.5)
    item = result["objects"][0]
    assert (item["x"], item["y"]) == (26, 34)
    assert item["area_px"] == 25 and item["area_um2"] == 6.25
    assert min(point[0] for point in item["contour"]) >= 23
    assert min(point[1] for point in item["contour"]) >= 31


def test_classical_methods_honor_polygon_roi_and_map_identity():
    image = np.full((30, 30), 1000, np.uint16)
    image[4:10, 4:10] = 2000
    image[20:26, 20:26] = 5000
    result = call({"target": image}, "nuclei", [channel("target", frame=4, channel_index=2)],
                  roi={"type": "polygon", "points": [[1, 1], [13, 1], [13, 13], [1, 13]]},
                  threshold=1500, sigma=0, min_area=4, split_touching=False)
    assert result["count"] == 1
    assert result["objects"][0]["mean_intensity"] == 2000
    assert result["method"]["source_channel"]["source"]["channel"] == 2


def test_line_profile_bilinear_linear_function_and_physical_length():
    yy, xx = np.mgrid[:20, :30]
    image = (2 * xx + 3 * yy).astype(np.uint16)
    result = call(image, "line_profile", line={"x1": 2.5, "y1": 4.5, "x2": 12.5, "y2": 4.5},
                  samples=6, line_width_px=3, um_per_px_x=2, um_per_px_y=3)
    profile = result["profiles"][0]
    assert profile["values"] == pytest.approx([18.5, 22.5, 26.5, 30.5, 34.5, 38.5])
    assert profile["distance_px"] == [0, 2, 4, 6, 8, 10]
    assert profile["distance_um"] == [0, 4, 8, 12, 16, 20]
    assert profile["valid_width_samples"] == [3] * 6
    assert result["method"]["interpolation"] == "bilinear_source_intensity"


def test_line_width_boundary_is_explicit_and_invalid_line_rejected():
    image = np.ones((10, 10), np.uint16)
    result = call(image, "line_profile", line={"x1": 0, "y1": 0, "x2": 9, "y2": 0}, samples=5, line_width_px=3)
    assert result["profiles"][0]["values"] == [1] * 5
    assert result["profiles"][0]["valid_width_samples"] == [2] * 5
    assert any("超出" in message for message in result["warnings"])
    with pytest.raises(ValueError, match="端点"):
        call(image, "line_profile", line={"x1": -1, "y1": 0, "x2": 9, "y2": 0})


def test_pairwise_overlap_and_manders_definitions_match_small_known_arrays():
    a = np.array([[0, 2], [3, 4]], np.uint16)
    b = np.array([[0, 4], [0, 8]], np.uint16)
    result = call({"a": a, "b": b}, "channel_overlap", [channel("a"), channel("b")],
                  threshold_method="manual", threshold=1)
    pair = result["pairs"][0]
    assert pair["intersection_px"] == 2
    assert pair["positive_a_px"] == 3 and pair["positive_b_px"] == 2
    assert pair["jaccard"] == pytest.approx(2 / 3)
    assert pair["dice"] == pytest.approx(4 / 5)
    assert pair["overlap_fraction_a"] == pytest.approx(2 / 3)
    assert pair["overlap_fraction_b"] == 1
    assert pair["manders_m1"] == pytest.approx(6 / 9)
    assert pair["manders_m2"] == 1
    assert pair["pearson_r"] == pytest.approx(np.corrcoef(a.ravel(), b.ravel())[0, 1])
    assert result["method"]["metrics"]["pixel_domain"] == "pairwise_joint_finite_pixels_within_roi"


def test_channel_threshold_override_and_zero_denominators_are_not_fake_zero_correlation():
    a, b = np.zeros((4, 4), np.uint16), np.ones((4, 4), np.uint16)
    chosen = [channel("a", threshold={"method": "manual", "value": 0}),
              channel("b", threshold={"method": "manual", "value": 2})]
    result = call({"a": a, "b": b}, "channel_overlap", chosen)
    pair = result["pairs"][0]
    assert pair["pearson_r"] is None
    assert pair["jaccard"] is None and pair["dice"] is None
    assert pair["manders_m1"] is None
    assert pair["manders_m2"] == 0
    assert pair["manders_overlap_coefficient"] is None
    assert "both_threshold_masks_empty" in pair["notes"]
    json.dumps(result, allow_nan=False)


def test_negative_intensities_disable_manders_but_keep_pearson():
    a = np.arange(-4, 5).reshape(3, 3).astype(float)
    b = a * 2 + 1
    result = call({"a": a, "b": b}, "channel_overlap", [channel("a"), channel("b")],
                  threshold_method="manual", threshold=0)
    pair = result["pairs"][0]
    assert pair["pearson_r"] == pytest.approx(1)
    assert pair["manders_m1"] is None and pair["manders_m2"] is None
    assert pair["manders_overlap_coefficient"] is None


def test_pairwise_nan_uses_common_valid_domain():
    a = np.array([[1, 2], [3, np.nan]])
    b = np.array([[2, 4], [np.nan, 8]])
    result = call({"a": a, "b": b}, "channel_overlap", [channel("a"), channel("b")], threshold_method="manual", threshold=0)
    assert result["pairs"][0]["valid_pixels"] == 2
    assert result["pairs"][0]["pearson_r"] == pytest.approx(1)
    assert result["pairs"][0]["intersection_px"] == 2


def test_invalid_channel_geometry_and_index_never_silently_align():
    with pytest.raises(ValueError, match="尺寸不同"):
        call({"a": np.zeros((5, 5)), "b": np.zeros((6, 5))}, "channel_intensity", [channel("a"), channel("b")])
    with pytest.raises(ValueError, match="唯一"):
        call({"a": np.zeros((5, 5))}, "channel_intensity", [channel("a"), channel("a")])
    with pytest.raises(ValueError, match="至少需要两个"):
        call(np.zeros((5, 5)), "channel_overlap")
    with pytest.raises(ValueError, match="整数"):
        call(np.zeros((5, 5, 2)), "channel_intensity", [channel("a", channel_index=.5)])
def test_ihc_respects_declared_color_axis_and_rejects_ambiguous_shapes():
    image = np.full((3, 8, 6), 200, np.uint8)
    with pytest.raises(ValueError, match="颜色轴必须"):
        analyze_microscopy(image, {"method": "ihc", "channel_axis": -1})
    cyx = analyze_microscopy(image, {"method": "ihc", "channel_axis": 0, "min_area": 1})
    assert cyx["method"]["roi"]["width"] == 6
    assert cyx["method"]["roi"]["height"] == 8
    ambiguous = np.full((3, 8, 4), 200, np.uint8)
    with pytest.raises(ValueError, match="颜色轴不明确"):
        analyze_microscopy(ambiguous, {"method": "ihc"})
    declared = analyze_microscopy(ambiguous, {"method": "ihc", "channel_axis": -1, "min_area": 1})
    assert declared["method"]["roi"]["height"] == 3


def test_legacy_scalar_result_preserves_original_plane_identity():
    identity = {"asset_id": "source", "series": 2, "frame": 7, "channel": 6}
    result = analyze_microscopy(np.full((8, 9), 30001, np.uint16),
                                {"method": "intensity", "channel": 0, "source": identity})
    assert result["method"]["source"] == identity
    assert result["method"]["channel"] == 6

