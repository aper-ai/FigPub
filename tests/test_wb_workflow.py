from copy import deepcopy

import numpy as np
import pytest

from workbench.analysis import analyze_wb
from workbench.wb_workflow import review_wb_exposures


def exposure(images, rois, *, pair="short", sample="s1", bio="b1", experiment="e1", group="control",
             contrast=1000, control_contrast=1200, saturated=False, dtype=np.uint16, seconds=1):
    aid = f"{experiment}-{sample}-{pair}"
    image = np.full((52, 40), 4000, dtype=dtype)
    # Explicit border noise produces finite, reproducible background estimates.
    image[::2, ::2] += 2
    image[1::2, 1::2] -= 2
    image[10:16, 10:20] = 4000 - contrast
    image[34:40, 10:20] = 4000 - control_contrast
    if saturated:
        image[11, 11] = 0
    images[(aid, 0, 0, 0)] = image
    for role, protein, y in (("target", "Target", 6), ("control", "ACTB", 30)):
        rois.append({"id": f"{aid}-{role}", "asset_id": aid,
                     "source": {"asset_id": aid, "series": 0, "frame": 0, "channel": 0},
                     "x": 5, "y": y, "width": 20, "height": 14,
                     "role": role, "protein": protein, "experiment_id": experiment,
                     "sample_id": sample, "group": group, "biological_replicate": bio,
                     "technical_replicate": "1", "exposure_id": pair, "exposure_seconds": seconds})


def result(images, rois, **params):
    return review_wb_exposures(images, {"rois": rois, "polarity": "dark", **params})


def test_complete_valid_exposure_beats_stronger_saturated_and_weak_candidates():
    images, rois = {}, []
    exposure(images, rois, pair="valid", contrast=800)
    exposure(images, rois, pair="clipped", contrast=3000, saturated=True)
    exposure(images, rois, pair="weak", contrast=2, control_contrast=2)
    snapshot = {key: array.copy() for key, array in images.items()}
    before = deepcopy(rois)
    report = result(images, rois)
    assert report["groups"][0]["recommended_pair_id"] == "valid"
    assert any("saturated_pixels" in row["reasons"] for row in report["rows"])
    assert any("low_signal_to_noise" in row["reasons"] for row in report["rows"])
    assert {d["roi_id"] for d in report["roi_decisions"] if d["included"]} == {r["id"] for r in rois if r["exposure_id"] == "valid"}
    assert report["method"]["automatic_selection_applied"] is False
    assert rois == before
    for key in images:
        np.testing.assert_array_equal(images[key], snapshot[key])


def test_recommendation_uses_one_complete_exposure_for_all_groups_not_cherry_picking():
    images, rois = {}, []
    for sample, group, bio in (("s1", "Control", "bio1"), ("s2", "Treated", "bio2")):
        exposure(images, rois, pair="short", sample=sample, group=group, bio=bio, contrast=800)
        exposure(images, rois, pair="long", sample=sample, group=group, bio=bio, contrast=3000, saturated=sample == "s2", seconds=4)
    report = result(images, rois)
    assert len(report["groups"]) == 1
    assert report["groups"][0]["logical_measurements"] == 4
    assert report["groups"][0]["recommended_pair_id"] == "short"
    assert len([d for d in report["roi_decisions"] if d["included"]]) == 4


def test_same_sample_name_across_experiments_does_not_pair_or_share_choices():
    images, rois = {}, []
    exposure(images, rois, experiment="A", pair="first")
    exposure(images, rois, experiment="B", pair="second")
    report = result(images, rois)
    assert [(g["experiment_id"], g["recommended_pair_id"]) for g in report["groups"]] == [("A", "first"), ("B", "second")]
    by_id = {row["id"]: row for row in rois}
    for decision in report["roi_decisions"]:
        if decision["control_roi_id"]:
            assert by_id[decision["roi_id"]]["experiment_id"] == by_id[decision["control_roi_id"]]["experiment_id"]


def test_incomplete_pair_and_failed_all_candidates_produce_no_recommendation():
    images, rois = {}, []
    exposure(images, rois, pair="incomplete")
    rois.pop()
    exposure(images, rois, pair="bad", saturated=True)
    report = result(images, rois)
    assert report["groups"][0]["recommended_pair_id"] is None
    assert report["roi_decisions"] == []
    assert "incomplete_exposure_set" in next(c for c in report["groups"][0]["candidates"] if c["pair_id"] == "incomplete")["reasons"]


def test_float_unknown_detector_range_requires_explicit_limits():
    images, rois = {}, []
    exposure(images, rois, dtype=np.float32)
    report = result(images, rois)
    assert report["groups"][0]["recommended_pair_id"] is None
    assert all("detector_range_unknown" in row["reasons"] for row in report["rows"])
    assert result(images, rois, black_level=0, saturation_value=4095)["groups"][0]["recommended_pair_id"] == "short"


def test_explicit_adc_detects_clipping_hidden_by_storage_dtype():
    images, rois = {}, []
    exposure(images, rois)
    for plane in images.values():
        plane[:] = 4095 - plane
    raw_report = review_wb_exposures(images, {"rois": rois, "polarity": "bright"})
    assert raw_report["groups"][0]["recommended_pair_id"] == "short"
    list(images.values())[0][11, 11] = 4095
    storage_report = review_wb_exposures(images, {"rois": rois, "polarity": "bright"})
    assert storage_report["groups"][0]["recommended_pair_id"] == "short"
    adc_report = review_wb_exposures(images, {"rois": rois, "polarity": "bright", "adc_bits": 12})
    assert adc_report["groups"][0]["recommended_pair_id"] is None
    assert adc_report["rows"][0]["detector_high"] == 4095


def test_changed_roi_area_prevents_incomparable_exposure_ranking():
    images, rois = {}, []
    exposure(images, rois, pair="a")
    exposure(images, rois, pair="b")
    rois[-2]["width"] = 21
    report = result(images, rois)
    assert report["groups"][0]["recommended_pair_id"] is None
    assert all("roi_area_mismatch_between_exposures" in c["reasons"] for c in report["groups"][0]["candidates"])


def test_user_exclusion_is_never_revived_and_duplicate_source_is_not_independence():
    images, rois = {}, []
    exposure(images, rois, pair="a")
    exposure(images, rois, pair="b")
    for roi in rois:
        if roi["exposure_id"] == "b":
            roi["included"] = False
    report = result(images, rois)
    assert report["groups"][0]["recommended_pair_id"] == "a"
    assert not any("-b-" in decision["roi_id"] for decision in report["roi_decisions"])
    duplicate = deepcopy(rois[0])
    duplicate.update(id="duplicate", sample_id="suspicious-sample", biological_replicate="other")
    rois.append(duplicate)
    report = result(images, rois)
    assert report["groups"][0]["recommended_pair_id"] is None
    assert "duplicate_source_pixels" in report["rows"][0]["reasons"]
    # Resolving the duplicate explicitly must recover the retained source ROI.
    # The excluded copy stays in the report and can never become a decision.
    rois[-1]["included"] = False
    corrected = result(images, rois)
    assert corrected["groups"][0]["recommended_pair_id"] == "a"
    assert "duplicate_source_pixels" not in corrected["rows"][0]["reasons"]
    assert "excluded_by_user" in corrected["rows"][-1]["reasons"]
    assert not any(item["roi_id"] == "duplicate" for item in corrected["roi_decisions"])


def test_source_identity_distinguishes_frames_and_rejects_invalid_indices():
    images, rois = {}, []
    exposure(images, rois)
    for roi in rois:
        roi["source"]["frame"] = 2
    key = next(iter(images))
    images[(key[0], 0, 2, 0)] = images.pop(key)
    assert result(images, rois)["groups"][0]["recommended_pair_id"] == "short"
    rois[0]["source"]["frame"] = 2.5
    with pytest.raises(ValueError, match="非负整数"):
        result(images, rois)


def test_missing_identity_outside_roi_and_nonfinite_pixels_fail_honestly():
    images, rois = {}, []
    exposure(images, rois, dtype=np.float32)
    rois[0]["biological_replicate"] = ""
    assert result(images, rois, adc_bits=12)["groups"][0]["recommended_pair_id"] is None
    rois[0]["x"] = 500
    with pytest.raises(ValueError, match="超出原图"):
        result(images, rois)
    rois[0]["x"] = 5
    list(images.values())[0][11, 11] = np.nan
    assert "nonfinite_pixels" in result(images, rois, adc_bits=12)["rows"][0]["reasons"]


def test_quality_application_keeps_one_biological_value_and_correct_control():
    images, rois = {}, []
    for pair, time in (("short", 1), ("long", 5)):
        exposure(images, rois, pair=pair, seconds=time)
    report = result(images, rois)
    assert report["groups"][0]["recommended_pair_id"] == "short"
    decisions = {d["roi_id"]: d for d in report["roi_decisions"]}
    selected = [{**roi, "included": decisions[roi["id"]]["included"], "control_roi_id": decisions[roi["id"]]["control_roi_id"]} for roi in rois]
    output = analyze_wb(images, {"rois": selected, "polarity": "dark", "background": "border"})
    assert len(output["biological_rows"]) == 1
    assert output["groups"][0]["n_biological"] == 1
    assert output["groups"][0]["sd"] is None
    target = next(row for row in output["rows"] if row["role"] == "target" and row["analysis_included"])
    assert target["control_roi_id"].endswith("short-control")
    assert target["normalized"] is not None


def test_explicit_cross_file_pairing_and_source_specific_adc():
    images, rois = {}, []
    exposure(images, rois)
    rois[0]["exposure_id"] = "target-acquisition"
    rois[1]["exposure_id"] = "control-acquisition"
    for roi in rois:
        roi["exposure_pair_id"] = "pair-one"
    key = "|".join(map(str, next(iter(images))))
    report = result(images, rois, source_ranges={key: {"adc_bits": 12}})
    assert report["groups"][0]["recommended_pair_id"] == "pair-one"
    assert all(row["detector_range_basis"] == "explicit_detector" for row in report["rows"])


def test_work_budget_and_noninteger_adc_reject_before_measurement():
    images, rois = {}, []
    exposure(images, rois)
    with pytest.raises(ValueError, match="ADC 位数"):
        result(images, rois, adc_bits=12.5)
    large = np.zeros((3000, 3000), dtype=np.uint8)
    source = {"asset_id": "large", "series": 0, "frame": 0, "channel": 0}
    with pytest.raises(ValueError, match="800 万像素"):
        result({("large", 0, 0, 0): large}, [{**rois[0], "asset_id": "large", "source": source, "x": 0, "y": 0, "width": 3000, "height": 3000}])
