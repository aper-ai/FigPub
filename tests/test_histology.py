"""Numerical and refusal regressions for original-RGB H/DAB measurements."""
import copy
import hashlib
import threading

import numpy as np
import pytest

from workbench.histology import (DEFAULT_VECTORS, analyze_histology, deconvolve_original,
                                estimate_macenko, fit_dab_gmm, normalize_vectors)


def rgb_from_concentrations(h, d, white=1.):
    return white * np.exp(-(h[..., None] * DEFAULT_VECTORS[0] + d[..., None] * DEFAULT_VECTORS[1]))


def graded_fixture():
    rng = np.random.default_rng(17)
    h = rng.uniform(.15, 1., (80, 80))
    d = np.repeat(np.array([.05, .3, .65, 1.1]), 20)[None, :] + rng.normal(0, .003, (80, 80))
    return rgb_from_concentrations(h, d), h, d


def base_params(**changes):
    return {"method": "ihc_advanced", "channel_axis": -1, "stain_method": "fixed",
            "grading_method": "manual", "grade_thresholds": [.15, .5, .85], **changes}


def test_nnls_recovers_known_stain_concentration_without_display_normalization():
    rgb, h, d = graded_fixture()
    source = rgb.copy()
    result_h, result_d, _, residual = deconvolve_original(rgb, 1., DEFAULT_VECTORS)
    np.testing.assert_allclose(result_h, h, atol=6e-8)
    np.testing.assert_allclose(result_d, d, atol=6e-8)
    assert residual < 1e-25
    np.testing.assert_array_equal(rgb, source)


def test_macenko_estimates_and_orders_pure_endmember_directions():
    rng = np.random.default_rng(19)
    h = np.zeros((160, 160))
    d = np.zeros_like(h)
    h[:, :80] = rng.uniform(.2, 1.5, (160, 80))
    d[:, 80:] = rng.uniform(.2, 1.5, (160, 80))
    rgb = rgb_from_concentrations(h, d)
    vectors, details = estimate_macenko(rgb, np.ones(h.shape, bool), 1.)
    np.testing.assert_allclose(vectors, DEFAULT_VECTORS, atol=1e-10)
    assert details["ordering"].startswith("maximum_cosine")
    assert details["sample_count"] == h.size


def test_macenko_sampling_is_bounded_and_repeatable():
    rng = np.random.default_rng(20)
    h = rng.uniform(.05, 1., (400, 400))
    d = rng.uniform(.05, 1., h.shape)
    rgb = rgb_from_concentrations(h, d)
    first, details = estimate_macenko(rgb, np.ones(h.shape, bool), 1.)
    second, _ = estimate_macenko(rgb, np.ones(h.shape, bool), 1.)
    np.testing.assert_array_equal(first, second)
    assert details["sampled"] and details["sample_count"] <= 100_000


@pytest.mark.parametrize("input_case", ["white", "constant_stain", "one_stain", "few_pixels"])
def test_macenko_rejects_missing_or_rank_deficient_tissue(input_case):
    h = np.full((32, 32), .5)
    d = np.zeros_like(h)
    if input_case == "white":
        h[:] = 0
    if input_case == "one_stain":
        h[:] = np.linspace(.1, 1., h.size).reshape(h.shape)
    if input_case == "few_pixels":
        h = h[:4, :4]
        d = d[:4, :4]
    with pytest.raises(ValueError):
        analyze_histology(rgb_from_concentrations(h, d), base_params(stain_method="macenko"))


def test_manual_grades_preserve_raw_input_and_report_pixel_weighted_score():
    rgb, _, d = graded_fixture()
    rgb.setflags(write=False)
    before = hashlib.sha256(rgb.tobytes()).hexdigest()
    result = analyze_histology(rgb, base_params(um_per_px=.5))
    assert [row["area_px"] for row in result["rows"]] == [1600] * 4
    assert result["summary"]["pixel_weighted_score"] == 150
    assert result["summary"]["positive_fraction"] == .75
    assert result["summary"]["tissue_area_um2"] == 1600
    assert result["summary"]["integrated_dab_od"] == pytest.approx(d.sum(), rel=1e-7)
    assert "h_score" not in result["summary"]
    assert not result["method"]["display_lut_used"]
    assert hashlib.sha256(rgb.tobytes()).hexdigest() == before


def test_gmm_fits_real_four_groups_and_is_deterministic():
    rgb, _, d = graded_fixture()
    a = fit_dab_gmm(d.ravel())
    b = fit_dab_gmm(d.ravel())
    assert a == b
    assert a[0][0] == pytest.approx(.175, abs=.02)
    result = analyze_histology(rgb, base_params(grading_method="gmm"))
    assert [row["area_px"] for row in result["rows"]] == [1600] * 4
    assert result["method"]["grading"]["components"] == 4


@pytest.mark.parametrize("components", [1, 2, 3])
def test_gmm_does_not_invent_four_grades_from_fewer_populations(components):
    rng = np.random.default_rng(17)
    values = np.concatenate([rng.normal(.1 + j * .7, .02, 1000) for j in range(components)])
    with pytest.raises(ValueError):
        fit_dab_gmm(values)


def test_examples_calibrate_only_positive_boundary_and_keep_upper_grades():
    rgb, _, _ = graded_fixture()
    examples = {"negative": {"x": 2, "y": 2, "width": 15, "height": 20},
                "positive": {"x": 22, "y": 2, "width": 15, "height": 20}}
    result = analyze_histology(rgb, base_params(grading_method="examples", example_rois=examples))
    thresholds = result["calibration_profile"]["grade_thresholds"]
    assert .16 < thresholds[0] < .19
    assert thresholds[1:] == [.5, .85]
    assert result["method"]["grading"]["upper_grades"] == "explicit_manual_thresholds"


def test_overlapping_examples_and_examples_outside_roi_are_rejected():
    rgb, _, _ = graded_fixture()
    same = {"x": 2, "y": 2, "width": 15, "height": 20}
    with pytest.raises(ValueError, match="重叠"):
        analyze_histology(rgb, base_params(grading_method="examples", example_rois={"negative": same, "positive": same}))
    with pytest.raises(ValueError, match="分析 ROI 内"):
        analyze_histology(rgb, base_params(roi={"x": 20, "y": 0, "width": 40, "height": 80}, grading_method="examples",
            example_rois={"negative": same, "positive": {"x": 22, "y": 2, "width": 15, "height": 20}}))


def test_locked_profile_reuses_vectors_and_absolute_thresholds_on_new_image():
    rgb, h, d = graded_fixture()
    first = analyze_histology(rgb, base_params())
    profile = copy.deepcopy(first["calibration_profile"])
    next_image = rgb_from_concentrations(h, d * 1.5)
    second = analyze_histology(next_image, base_params(locked_profile=profile, stain_method="invalid", grading_method="invalid"))
    assert second["calibration_profile"]["id"] == profile["id"]
    assert second["method"]["locked_profile"]
    assert second["summary"]["mean_dab_od_in_tissue"] == pytest.approx(first["summary"]["mean_dab_od_in_tissue"] * 1.5, rel=1e-7)
    assert second["summary"]["pixel_weighted_score"] > first["summary"]["pixel_weighted_score"]


def test_raw_fixed_vectors_lock_without_rounding_identity_drift_or_roi_reuse():
    rgb, _, _ = graded_fixture()
    source = {"asset_id": "first", "series": 0, "frame": 0}
    first = analyze_histology(rgb, base_params(stain_vectors=[[.650, .704, .286], [.268, .570, .776]],
        source=source, roi={"x": 0, "y": 0, "width": 60, "height": 80}))
    profile = copy.deepcopy(first["calibration_profile"])
    for asset_id in ["second", "third"]:
        result = analyze_histology(rgb, base_params(locked_profile=profile,
            source={"asset_id": asset_id, "series": 0, "frame": 0}))
        assert result["calibration_profile"] == profile
        assert result["calibration_profile"]["origin_source"] == source
        assert "roi" not in result["calibration_profile"]
        assert result["summary"]["tissue_area_px"] == 80 * 80


def test_profile_and_white_level_validation_fail_cleanly():
    rgb, _, _ = graded_fixture()
    with pytest.raises(ValueError, match="锁定"):
        analyze_histology(rgb, base_params(locked_profile={"version": 1}))
    with pytest.raises(ValueError, match="白点"):
        analyze_histology(rgb * 255, base_params())
    good = analyze_histology(rgb * 255, base_params(rgb_white_level=255))
    assert good["summary"]["pixel_weighted_score"] == 150


def test_artifact_exclusion_and_polygon_roi_change_denominator_only_as_selected():
    rgb, _, _ = graded_fixture()
    excluded = {"type": "rectangle", "x": 0, "y": 0, "width": 20, "height": 80}
    result = analyze_histology(rgb, base_params(exclude_rois=[excluded, excluded]))
    assert result["summary"]["tissue_area_px"] == 4800
    assert result["summary"]["excluded_area_px"] == 1600
    assert result["summary"]["pixel_weighted_score"] == 200
    assert result["rows"][0]["area_px"] == 0


@pytest.mark.parametrize("thresholds", [[.2, .2, .5], [.5, .2, .8], [float("nan"), .5, .8], [-1, .5, .8]])
def test_invalid_thresholds_are_rejected(thresholds):
    rgb, _, _ = graded_fixture()
    with pytest.raises(ValueError):
        analyze_histology(rgb, base_params(grade_thresholds=thresholds))


def test_invalid_color_axis_vectors_and_cancel_are_not_silently_repaired():
    rgb, _, _ = graded_fixture()
    with pytest.raises(ValueError, match="颜色轴"):
        analyze_histology(rgb[:3, :, :2], {"channel_axis": -1})
    with pytest.raises(ValueError):
        normalize_vectors([[1, 0, 0], [1, 0, 0]])
    event = threading.Event()
    event.set()
    with pytest.raises(ValueError, match="取消"):
        analyze_histology(rgb, base_params(), cancel_event=event)


def test_transparent_rgba_pixels_are_excluded_as_white_background():
    rgb, _, _ = graded_fixture()
    alpha = np.ones(rgb.shape[:2])
    alpha[:, :20] = 0
    rgba = np.dstack((rgb, alpha))
    result = analyze_histology(rgba, base_params())
    assert result["summary"]["tissue_area_px"] == 4800
    assert result["summary"]["pixel_weighted_score"] == 200
