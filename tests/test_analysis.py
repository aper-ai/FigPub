"""Numerical and identity regressions for the new analysis core.

Fixtures are small synthetic arrays with known mathematical outcomes. They are
not an accuracy benchmark on real experiments or a source of production data.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from workbench.analysis import PixelRegion, analyze_wb, select_plane
from workbench.microscopy import _deconvolve_hdab, analyze_microscopy
from workbench.qpcr import analyze_qpcr, read_qpcr_table


def band(value: int = 1100, background: int = 1000, dtype=np.uint16) -> np.ndarray:
    image = np.full((20, 24), background, dtype=dtype)
    image[7:13, 6:18] = value
    return image


def roi(asset="a", identifier="r1", sample="s1", biological="b1", group="Control", role="target", **extra):
    return {"id": identifier, "asset_id": asset, "x": 0, "y": 0, "width": 24, "height": 20,
            "sample_id": sample, "group": group, "biological_replicate": biological,
            "technical_replicate": "1", "exposure_id": "1", "role": role,
            "protein": "Target" if role == "target" else "Actin", **extra}


def measure(images, rois, **params):
    return analyze_wb(images, {"rois": rois, "background": "border", "polarity": "bright", **params})


def test_uint16_one_count_difference_survives_and_display_is_irrelevant():
    original = band(1100)
    changed = band(1101)
    result = measure({"a": original}, [roi()])
    result_changed = measure({"a": changed}, [roi()], display={"low": 1050, "high": 1060, "gamma": 4, "invert": True})
    assert result["rows"][0]["background_subtracted"] == 72 * 100
    assert result_changed["rows"][0]["background_subtracted"] - result["rows"][0]["background_subtracted"] == 72
    assert np.array_equal(np.rint(original / 257).astype(np.uint8), np.rint(changed / 257).astype(np.uint8))
    display_only = measure({"a": original}, [roi()], brightness=100, contrast=80, gamma=9)
    assert result["rows"] == display_only["rows"]
    assert result["method"]["display_lut_used"] is False
    assert result["rows"][0]["source_bit_depth"] == 16
    assert np.array_equal(original, band(1100))


def test_roi_uses_source_pixels_and_rejects_clipped_or_empty_geometry():
    image = np.arange(100 * 100, dtype=np.uint16).reshape(100, 100)
    region = {**roi(), "x": 15, "y": 20, "width": 8, "height": 7}
    measured = measure({"a": image}, [region], background="none")
    assert measured["rows"][0]["raw_integrated"] == float(image[20:27, 15:23].sum())
    assert measured["rows"][0]["roi"] == {"x": 15, "y": 20, "width": 8, "height": 7}
    for bad in ({"x": -1}, {"width": 0}, {"x": 99}, {"height": float("nan")}):
        with pytest.raises(ValueError):
            measure({"a": image}, [{**region, **bad}])


def test_channel_selection_is_a_view_and_preserves_dtype():
    rgb = np.arange(60, dtype=np.uint16).reshape(4, 5, 3)
    selected = select_plane(rgb, 1)
    assert selected.dtype == np.uint16
    assert np.shares_memory(selected, rgb)
    assert np.array_equal(selected, rgb[..., 1])


def test_signed_background_is_preserved_without_positive_clipping_bias():
    result = measure({"a": band(900)}, [roi()])
    row = result["rows"][0]
    assert row["background_subtracted"] == -7200
    assert row["positive_only_integral"] == 0
    assert row["analysis_included"] is False
    assert "signal_at_or_below_background" in row["qc_flags"]


def test_saturation_uses_original_detector_range_and_excludes_by_default():
    image = band()
    image[10, 10] = 65535
    result = measure({"a": image}, [roi()])
    assert result["rows"][0]["saturation_fraction"] == 1 / image.size
    assert result["rows"][0]["analysis_included"] is False
    assert result["groups"] == []


def test_lower_bit_adc_and_float_range_must_be_explicit():
    image = band()
    image[10, 10] = 4095
    row = measure({"a": image}, [roi()], adc_bits=12)["rows"][0]
    assert "saturated_source_pixels" in row["qc_flags"]
    float_image = band().astype(np.float64)
    floating = measure({"a": float_image}, [roi()])["rows"][0]
    assert floating["saturation_fraction"] is None
    with pytest.raises(ValueError, match="浮点暗条带"):
        measure({"a": float_image}, [roi()], background="none", polarity="dark")


def test_normalization_is_full_precision_and_controls_match_real_sample():
    images = {"target": band(1107), "control": band(1031)}
    result = measure(images, [roi("target", "t"), roi("control", "c", role="control")])
    value = result["rows"][0]["normalized"]
    assert value == pytest.approx(107 / 31, rel=1e-14)
    assert value != round(value, 4)
    assert result["groups"][0]["n_biological"] == 1
    assert result["groups"][0]["sd"] is None
    assert result["groups"][0]["sem"] is None
    mismatched = measure(images, [roi("target", "t", control_roi_id="c"), roi("control", "c", sample="different", role="control")])
    assert mismatched["rows"][0]["normalized"] is None
    assert "control_sample_identity_mismatch" in mismatched["rows"][0]["qc_flags"]


def test_zero_control_and_self_control_are_never_divided():
    result = measure({"a": band(), "c": band(1000)}, [roi(), roi("c", "c1", role="control")])
    assert result["rows"][0]["normalized"] is None
    same_source = measure({"a": band()}, [roi(control_roi_id="c"), roi("a", "c", role="control")])
    assert same_source["rows"][0]["normalized"] is None
    assert "control_is_target_source_roi" in same_source["rows"][0]["qc_flags"]


def test_exposures_never_become_independent_replicates():
    rois = [roi("a", "a1", exposure_id="short"), roi("b", "b1", exposure_id="long")]
    result = measure({"a": band(), "b": band(1200)}, rois)
    assert result["groups"] == []
    assert all("exposure_or_technical_identity_ambiguous" in row["qc_flags"] for row in result["rows"])
    selected = measure({"a": band(), "b": band(1200)}, rois, selected_exposures={"s1": "short"})
    assert selected["groups"][0]["n_biological"] == 1
    assert selected["groups"][0]["n_technical"] == 1


def test_technical_repeats_aggregate_before_biological_group_statistics():
    images = {"a": band(1100), "b": band(1200), "c": band(1300)}
    rois = [roi("a", "a1", technical_replicate="1"), roi("b", "b1", technical_replicate="2"),
            roi("c", "c1", sample="s2", biological="b2")]
    result = measure(images, rois)
    group = result["groups"][0]
    assert group["n_biological"] == 2
    assert group["n_technical"] == 3
    assert group["values"] == [72 * 150, 72 * 300]
    assert group["mean"] == 72 * 225


def test_relabeling_identical_source_pixels_cannot_increase_n():
    result = measure({"a": band()}, [roi(), roi(identifier="duplicate", sample="s2", biological="b2")])
    assert result["groups"] == []
    assert all("duplicate_source_roi" in row["qc_flags"] for row in result["rows"])


def test_missing_sample_identity_still_returns_measurement_not_group_n():
    result = measure({"a": band()}, [{"id": "r", "asset_id": "a", "x": 0, "y": 0, "width": 24, "height": 20}])
    assert result["rows"][0]["background_subtracted"] == 7200
    assert result["groups"] == []


def test_real_rolling_ball_background_is_operational_and_non_destructive():
    image = band()
    original = image.copy()
    result = measure({"a": image}, [roi()], background="rolling_ball", radius=5)
    assert result["rows"][0]["background_subtracted"] > 0
    assert result["method"]["radius_px"] == 5
    assert np.array_equal(image, original)
    json.dumps(result, allow_nan=False)


def qpcr_wells(sample, group, biological, delta, technical_count=1):
    rows = []
    for index in range(technical_count):
        for target, ct in (("ACTB", 20), ("Gene", 20 + delta)):
            rows.append({"sample": sample, "group": group, "biological_replicate": biological,
                         "technical_replicate": str(index + 1), "target": target, "ct": ct})
    return rows


def qpcr(rows, **params):
    return analyze_qpcr(rows, {"reference_gene": "ACTB", "control_group": "Control", **params})


def test_qpcr_baseline_uses_independent_biological_means_not_technical_well_counts():
    rows = (qpcr_wells("c1", "Control", "b1", 4, 1) + qpcr_wells("c2", "Control", "b2", 6, 3)
            + qpcr_wells("t1", "Treated", "b3", 3, 2))
    result = qpcr(rows)
    treated = next(row for row in result["rows"] if row["group"] == "Treated")
    assert treated["control_mean_delta_ct"] == 5
    assert treated["fold_change"] == 4
    controls = next(group for group in result["groups"] if group["group"] == "Control")
    assert controls["n_biological"] == 2
    assert controls["n_technical"] == 4
    assert controls["values"] == [2, 0.5]
    assert controls["mean"] == 1.25
    assert controls["geometric_mean_fold"] == 1
    assert result["comparisons"] == []


def test_qpcr_never_matches_reference_to_a_different_sample():
    rows = qpcr_wells("c1", "Control", "b1", 4) + qpcr_wells("t1", "Treated", "b2", 3)
    rows = [row for row in rows if not (row["sample"] == "t1" and row["target"] == "ACTB")]
    result = qpcr(rows)
    assert len(result["rows"]) == 1
    assert any("缺少有效内参" in warning for warning in result["warnings"])


def test_qpcr_undetermined_and_duplicate_technical_ids_are_visible():
    rows = qpcr_wells("c1", "Control", "b1", 4) + qpcr_wells("t1", "Treated", "b2", 3, 2)
    rows.append({**rows[-1]})
    rows.append({"sample": "t1", "group": "Treated", "biological_replicate": "b2", "target": "Gene", "ct": "Undetermined"})
    result = qpcr(rows)
    assert any("duplicate_well_or_technical_id" in row["qc_flags"] for row in result["source_rows"])
    assert any("undetermined_or_invalid_ct" in row["qc_flags"] for row in result["source_rows"])
    assert next(row for row in result["rows"] if row["group"] == "Treated")["n_technical"] == 1
    json.dumps(result, allow_nan=False)


def test_qpcr_welch_does_not_invent_significance_with_n_one():
    rows = qpcr_wells("c1", "Control", "b1", 4) + qpcr_wells("t1", "Treated", "b2", 3)
    result = qpcr(rows, test="welch_t")
    assert result["comparisons"][0]["p_value"] is None
    assert all(group["sd"] is None for group in result["groups"])


def test_qpcr_instrument_header_and_explicit_group_are_preserved(tmp_path):
    path = tmp_path / "export.csv"
    path.write_text("Instrument export\nOperator,Example\nSample Name,Target Name,Cq,Group,biological_replicate\ns1,ACTB,20,Control,b1\ns1,Gene,24,Control,b1\n", encoding="utf-8")
    table = read_qpcr_table(path)
    assert table["header_row"] == 3
    assert table["rows"][0]["source_row"] == 4
    result = qpcr(table["rows"])
    assert result["rows"][0]["fold_change"] == 1


def test_qpcr_missing_groups_are_not_guessed_from_names():
    with pytest.raises(ValueError, match="未指定组别"):
        qpcr([{"sample": "Control_rep1", "target": "Gene", "ct": 20}])


def test_microscopy_original_intensity_preserves_uint16_and_display_ignored():
    image = np.array([[500, 501], [1000, 1001]], dtype=np.uint16)
    params = {"method": "intensity", "um_per_px": 0.25, "display": {"low": 0, "high": 100, "gamma": 2}}
    result = analyze_microscopy(image, params)
    assert result["summary"]["mean_intensity"] == 750.5
    assert result["summary"]["integrated_intensity"] == 3002
    assert result["summary"]["area_um2"] == 0.25
    assert result["summary"]["source_bit_depth"] == 16


def test_nuclei_empty_constant_image_has_zero_objects():
    for value in (0, 1000):
        result = analyze_microscopy(np.full((40, 40), value, np.uint16), {"method": "nuclei"})
        assert result["count"] == 0
        assert result["objects"] == []


def test_nuclei_roi_coordinates_calibration_and_raw_measurement():
    image = np.zeros((100, 100), np.uint16)
    yy, xx = np.ogrid[:100, :100]
    disk = (xx - 42)**2 + (yy - 50)**2 <= 7**2
    image[disk] = 12001
    result = analyze_microscopy(image, {"method": "nuclei", "sigma": 0, "threshold": 100,
                                       "roi": {"x": 20, "y": 30, "width": 50, "height": 45},
                                       "um_per_px_x": 0.5, "um_per_px_y": 0.25, "min_area": 20})
    assert result["count"] == 1
    obj = result["objects"][0]
    assert obj["x"] == 42
    assert obj["y"] == 50
    assert obj["area_px"] == int(disk.sum())
    assert obj["area_um2"] == obj["area_px"] * 0.125
    assert obj["mean_intensity"] == 12001
    assert obj["contour"]
    json.dumps(result, allow_nan=False)


def test_ihc_nonnegative_stain_separation_matches_constructed_optical_density():
    vectors = np.array([[0.650, 0.704, 0.286], [0.268, 0.570, 0.776]])
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    rgb = np.ones((30, 30, 3), dtype=float)
    expected_h, expected_d = 0.3, 1.2
    rgb[5:25, 5:25] = np.exp(-(expected_h * vectors[0] + expected_d * vectors[1]))
    h, d, mask, _ = _deconvolve_hdab(rgb, {})
    assert h[10, 10] == pytest.approx(expected_h, rel=1e-12)
    assert d[10, 10] == pytest.approx(expected_d, rel=1e-12)
    assert mask.sum() == 400
    result = analyze_microscopy(rgb, {"method": "ihc", "threshold": 0.5, "min_area": 10, "um_per_px": 0.5})
    assert result["count"] == 1
    assert result["summary"]["positive_area_px"] == 400
    assert result["summary"]["positive_fraction"] == 1
    assert result["summary"]["positive_area_um2"] == 100
    assert result["summary"]["object_type"] == "connected_DAB_positive_region"
    json.dumps(result, allow_nan=False)


def test_ihc_white_background_is_not_tissue_and_not_false_positive():
    result = analyze_microscopy(np.full((30, 30, 3), 255, np.uint8), {"method": "ihc"})
    assert result["count"] == 0
    assert result["summary"]["tissue_area_px"] == 0
    assert result["summary"]["positive_fraction"] is None


def test_no_fake_optional_model_or_bad_calibration(monkeypatch, tmp_path):
    # Test the missing-component contract independently of whether the test
    # host also has the real optional model installed for integration checks.
    monkeypatch.setenv("FIGURE_WORKBENCH_CELLPOSE_HOME", str(tmp_path / "missing-cellpose"))
    monkeypatch.setenv("FIGURE_WORKBENCH_CELLPOSE_PYTHON", str(tmp_path / "missing-python.exe"))
    monkeypatch.setenv("FIGURE_WORKBENCH_CELLPOSE_MODELS", str(tmp_path / "missing-models"))
    with pytest.raises(ValueError, match="离线模型"):
        analyze_microscopy(np.zeros((20, 20)), {"method": "cellpose"})
    with pytest.raises(ValueError, match="像素标定"):
        analyze_microscopy(np.zeros((20, 20)), {"method": "intensity", "um_per_px": 0})
