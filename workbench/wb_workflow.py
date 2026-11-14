"""Source-pixel quality review for explicitly paired Western blot exposures.

The result is a recommendation, never an automatic scientific exclusion.  One
exposure is ranked for the complete user-declared experiment/series so a bright
sample cannot silently receive a different exposure from its comparator.  This
module neither changes source pixels nor invents independent sample identities.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from math import isfinite
from typing import Any, Mapping

import numpy as np

from .analysis import PixelRegion, source_range


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _source(roi: Mapping[str, Any]) -> tuple[str, int, int, int]:
    value = roi.get("source") or {}
    if not isinstance(value, Mapping):
        raise ValueError("ROI source 必须为原始来源对象。")
    asset = _text(value.get("asset_id") or roi.get("asset_id"))
    if not asset or (roi.get("asset_id") and asset != roi["asset_id"]):
        raise ValueError("ROI 原始 asset_id 缺失或不一致。")
    indices = []
    for name in ("series", "frame", "channel"):
        number = value.get(name, 0)
        if isinstance(number, bool) or not isinstance(number, (int, np.integer)) or number < 0:
            raise ValueError(f"ROI 原始 {name} 必须为非负整数。")
        indices.append(int(number))
    return (asset, *indices)


def _fingerprint(rois: list[dict]) -> str:
    encoded = json.dumps(rois, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _measure_quality(plane: np.ndarray, roi: dict, params: dict) -> dict:
    if plane.ndim != 2 or plane.dtype.kind not in "buif":
        raise ValueError("曝光质量比较必须读取二维原始数值平面。")
    region = PixelRegion.parse(roi, plane.shape)
    source = plane[region.slices]
    values = np.asarray(source, dtype=np.float64)
    finite = np.isfinite(values)
    reasons: list[str] = []
    warnings: list[str] = []
    result = {"roi_id": _text(roi.get("id")), "source": dict(roi.get("source") or {}),
              "rectangle": region.to_dict(), "source_dtype": str(plane.dtype),
              "source_pixel_count": int(values.size), "finite_pixel_count": int(finite.sum()),
              "eligible": False, "reasons": reasons, "warnings": warnings}
    if not finite.all():
        reasons.append("nonfinite_pixels")
    if min(values.shape) < 5:
        reasons.append("roi_too_small")
    valid = values[finite]
    if not valid.size:
        result.update(background=None, noise_sigma=None, signal_p95=None, signed_integral=None,
                      snr=None, saturation_fraction=None, dynamic_fraction=None)
        return result
    thickness_y, thickness_x = max(1, int(round(values.shape[0] * .12))), max(1, int(round(values.shape[1] * .12)))
    edge = np.zeros(values.shape, dtype=bool)
    edge[:thickness_y] = edge[-thickness_y:] = True
    edge[:, :thickness_x] = edge[:, -thickness_x:] = True
    edge_values = values[edge & finite]
    if not edge_values.size:
        reasons.append("background_unavailable")
        edge_values = valid
    background = float(np.median(edge_values))
    mad = float(np.median(np.abs(edge_values - background)))
    noise = 1.4826 * mad
    limits = {name: params[name] for name in ("adc_bits", "saturation_value", "black_level") if params.get(name) is not None}
    # Each instrument/source can have a distinct detector range.
    per_source = params.get("source_ranges", {}).get("|".join(map(str, _source(roi))), {})
    if not isinstance(per_source, Mapping):
        raise ValueError("source_ranges 中的探测器范围须为对象。")
    limits.update({name: per_source[name] for name in ("adc_bits", "saturation_value", "black_level") if per_source.get(name) is not None})
    if "adc_bits" in limits:
        bits = limits["adc_bits"]
        if isinstance(bits, bool) or not isinstance(bits, (int, np.integer)) or not 1 <= bits <= 32:
            raise ValueError("ADC 位数须为 1–32 的整数。")
    low, high = source_range(plane, limits)
    polarity = params.get("polarity", "dark")
    signal = (background - values) if polarity == "dark" else (values - background)
    inside = signal[~edge & finite]
    if not inside.size:
        inside = signal[finite]
    peak = float(np.percentile(inside, 95))
    integral = float(np.sum(signal[finite], dtype=np.float64))
    if not all(isfinite(value) for value in (background, noise, peak, integral)):
        raise ValueError("原始数值超出 float64 可可靠计算范围。")
    # A noise-free synthetic integer border must not produce infinite SNR.
    # Half a quantization unit is a conservative numerical floor, not measured noise.
    floor = .5 if plane.dtype.kind in "bui" else max(float(np.max(np.abs(valid))) * 1e-9, 1e-12)
    snr = max(0.0, peak) / max(noise, floor)
    clip = low if polarity == "dark" else high
    if clip is None:
        saturation = None
        reasons.append("detector_range_unknown")
    else:
        clipped = valid <= clip if polarity == "dark" else valid >= clip
        saturation = float(np.mean(clipped))
        if saturation > float(params.get("saturation_tolerance", 0)):
            reasons.append("saturated_pixels")
    if integral <= 0 or peak <= 0:
        reasons.append("nonpositive_signal")
    if snr < float(params.get("min_snr", 3)):
        reasons.append("low_signal_to_noise")
    span = high - low if low is not None and high is not None else None
    dynamic = max(0.0, peak) / span if span else None
    if span and ((valid < low).any() or (valid > high).any()):
        reasons.append("values_outside_detector_range")
    range_basis = "explicit_detector" if limits else "storage_dtype" if low is not None else "unknown"
    if range_basis == "storage_dtype":
        warnings.append("storage_range_may_differ_from_detector_adc")
    if noise == 0:
        warnings.append("zero_measured_border_noise_numerical_floor_used")
    result.update(background=background, noise_sigma=noise, noise_floor=floor,
                  signal_p95=peak, signed_integral=integral, snr=min(snr, 1e12),
                  saturation_fraction=saturation, dynamic_fraction=dynamic,
                  detector_low=low, detector_high=high, detector_range_basis=range_basis,
                  eligible=not reasons, exposure_seconds=roi.get("exposure_seconds"))
    return result


def review_wb_exposures(images: Mapping[tuple, np.ndarray], params: dict[str, Any]) -> dict[str, Any]:
    """Rank complete exposure sets using native ROI values and explicit identity.

    ``images`` keys are (asset_id, series, frame, channel).  ``exposure_pair_id``
    can explicitly connect different target/control acquisition IDs; otherwise
    exposure_id is used.  ``exposure_series_id`` optionally separates independent
    blot panels inside one experiment.  All groups and biological replicates in
    that series must be represented by each candidate exposure.
    """
    rois = params.get("rois")
    if not isinstance(rois, list) or not 1 <= len(rois) <= 5000 or any(not isinstance(r, dict) for r in rois):
        raise ValueError("曝光复核需要 1–5000 个 ROI。")
    if params.get("polarity", "dark") not in {"dark", "bright"}:
        raise ValueError("请明确选择暗条带或亮条带。")
    for name, default, lo, hi in (("min_snr", 3, 0, 1000), ("saturation_tolerance", 0, 0, 1)):
        value = float(params.get(name, default))
        if not isfinite(value) or not lo <= value <= hi:
            raise ValueError(f"{name} 参数超出有效范围。")
    if not isinstance(params.get("source_ranges", {}), Mapping):
        raise ValueError("source_ranges 必须为来源到探测器范围的对象。")
    ids = [_text(roi.get("id")) for roi in rois]
    if not all(ids) or len(set(ids)) != len(ids):
        raise ValueError("每个 ROI 必须有唯一的非空 ID。")
    # Quality review is interactive: bound pixel work before allocating float
    # buffers. Large originals remain supported through ordinary band ROIs.
    total_pixels = 0
    for roi in rois:
        identity = _source(roi)
        if identity not in images:
            raise ValueError("曝光复核缺少完整原始平面：" + "|".join(map(str, identity)))
        plane = np.asarray(images[identity])
        if plane.ndim != 2:
            raise ValueError("曝光质量比较必须读取二维原始数值平面。")
        region = PixelRegion.parse(roi, plane.shape)
        pixels = region.width * region.height
        total_pixels += pixels
        if pixels > 8_000_000 or total_pixels > 32_000_000:
            raise ValueError("曝光复核单个 ROI 最多 800 万像素、一次合计最多 3200 万像素；请缩小条带窗口或分批比较。")
    rows: list[dict] = []
    cohorts: dict[tuple, list[dict]] = defaultdict(list)
    duplicate_pixels: dict[tuple, list[dict]] = defaultdict(list)
    for roi in rois:
        identity = _source(roi)
        if identity not in images:
            raise ValueError("曝光复核缺少完整原始平面：" + "|".join(map(str, identity)))
        measured = _measure_quality(np.asarray(images[identity]), roi, params)
        for name in ("experiment_id", "sample_id", "biological_replicate", "technical_replicate", "group", "role", "protein", "exposure_id", "exposure_pair_id", "exposure_series_id"):
            measured[name] = _text(roi.get(name))
        measured["technical_replicate"] = measured["technical_replicate"] or "1"
        measured["exposure_series_id"] = measured["exposure_series_id"] or "default"
        measured["pair_id"] = measured["exposure_pair_id"] or measured["exposure_id"]
        required = ("experiment_id", "sample_id", "biological_replicate", "group", "role", "protein", "pair_id")
        if any(not measured[key] for key in required):
            measured["reasons"].append("explicit_identity_missing")
        if measured["role"] not in {"target", "control"}:
            measured["reasons"].append("invalid_roi_role")
        if roi.get("included", True) is False:
            measured["reasons"].append("excluded_by_user")
        time = roi.get("exposure_seconds")
        if time not in (None, ""):
            time = float(time)
            if not isfinite(time) or time <= 0:
                raise ValueError("曝光时间须为正的有限秒数。")
            measured["exposure_seconds"] = time
        else:
            measured["exposure_seconds"] = None
        measured["eligible"] = not measured["reasons"]
        r = measured["rectangle"]
        duplicate_pixels[(identity, r["x"], r["y"], r["width"], r["height"])].append(measured)
        rows.append(measured)
        cohorts[(measured["experiment_id"], measured["exposure_series_id"])].append(measured)
    for duplicates in duplicate_pixels.values():
        selected = [row for row in duplicates if "excluded_by_user" not in row["reasons"]]
        if len(selected) > 1:
            for row in selected:
                row["reasons"].append("duplicate_source_pixels")
                row["eligible"] = False

    def slot(row):
        return tuple(row[key] for key in ("sample_id", "biological_replicate", "technical_replicate", "group", "role", "protein"))

    groups = []
    decisions = []
    for (experiment, series), records in sorted(cohorts.items()):
        group_id = hashlib.sha256(json.dumps([experiment, series], ensure_ascii=False).encode()).hexdigest()[:20]
        slots = {slot(row) for row in records if "excluded_by_user" not in row["reasons"]}
        # Geometry is compared for each logical measurement, not across proteins.
        sizes: dict[tuple, set] = defaultdict(set)
        for row in records:
            if "excluded_by_user" not in row["reasons"]:
                sizes[slot(row)].add((row["rectangle"]["width"], row["rectangle"]["height"]))
        candidates = []
        for pair in sorted({row["pair_id"] for row in records}):
            choice = [row for row in records if row["pair_id"] == pair and "excluded_by_user" not in row["reasons"]]
            reasons = []
            found = [slot(row) for row in choice]
            if len(found) != len(set(found)):
                reasons.append("duplicate_measurement_identity")
            if set(found) != slots or not slots:
                reasons.append("incomplete_exposure_set")
            if not any(row["role"] == "target" for row in choice) or not any(row["role"] == "control" for row in choice):
                reasons.append("target_control_pair_missing")
            identities = {tuple(row[key] for key in ("sample_id", "biological_replicate", "technical_replicate", "group")) for row in choice}
            control_links = {}
            for ident in identities:
                members = [row for row in choice if tuple(row[key] for key in ("sample_id", "biological_replicate", "technical_replicate", "group")) == ident]
                controls = [row for row in members if row["role"] == "control"]
                targets = [row for row in members if row["role"] == "target"]
                if len(controls) != 1 or not targets:
                    reasons.append("control_identity_ambiguous")
                else:
                    control_links.update({row["roi_id"]: controls[0]["roi_id"] for row in targets})
            if any(len(sizes[slot(row)]) > 1 for row in choice):
                reasons.append("roi_area_mismatch_between_exposures")
            if any(not row["eligible"] for row in choice):
                reasons.append("roi_quality_failed")
            scores = [row.get("snr") for row in choice if row.get("snr") is not None]
            snr_floor = min(scores) if scores else 0.0
            saturation_max = max((row.get("saturation_fraction") or 0.0) for row in choice) if choice else None
            # Saturation and invalid identities reject first; SNR ranks the
            # weakest usable band across the entire exposure cohort.
            candidate = {"pair_id": pair, "eligible": bool(choice) and not reasons,
                         "reasons": sorted(set(reasons)), "roi_ids": [row["roi_id"] for row in choice],
                         "control_links": control_links, "minimum_snr": snr_floor,
                         "median_snr": float(np.median(scores)) if scores else 0.0,
                         "max_saturation_fraction": saturation_max,
                         "exposure_seconds": max((row["exposure_seconds"] for row in choice if row["exposure_seconds"] is not None), default=None)}
            candidates.append(candidate)
        candidates.sort(key=lambda c: (not c["eligible"], -min(c["minimum_snr"], 100), -min(c["median_snr"], 100), c["exposure_seconds"] if c["exposure_seconds"] is not None else float("inf"), c["pair_id"]))
        recommended = next((candidate for candidate in candidates if candidate["eligible"]), None)
        groups.append({"id": group_id, "experiment_id": experiment, "exposure_series_id": series,
                       "logical_measurements": len(slots), "candidates": candidates,
                       "recommended_pair_id": recommended["pair_id"] if recommended else None,
                       "explanation": "完整配对并通过原始像素质控后，优先改善本系列最弱条带的信噪；信噪达到100后以较短曝光破同分。" if recommended else "没有同时覆盖全部样本、目标和内参且通过质控的曝光；没有自动选择。"})
        if recommended:
            selected = set(recommended["roi_ids"])
            for row in records:
                if "excluded_by_user" not in row["reasons"]:
                    decisions.append({"roi_id": row["roi_id"], "included": row["roi_id"] in selected,
                                      "control_roi_id": recommended["control_links"].get(row["roi_id"], ""),
                                      "group_id": group_id})
    return {"schema_version": 1, "kind": "wb_exposure_review", "roi_signature": _fingerprint(rois),
            "rows": rows, "groups": groups, "roi_decisions": decisions,
            "method": {"measurement_basis": "original_pixels", "polarity": params.get("polarity", "dark"),
                       "ranking": "complete_cohort_minimum_snr_capped100_then_median_then_exposure_time",
                       "selection_scope": "experiment_and_declared_exposure_series", "min_snr": float(params.get("min_snr", 3)),
                       "saturation_tolerance": float(params.get("saturation_tolerance", 0)),
                       "display_lut_used": False, "automatic_selection_applied": False},
            "warnings": ["信噪评分不是线性响应验证或科学准确率；曝光时间不用于自动缩放强度。", "默认检查存储位深范围；仪器ADC小于存储位深时，请填写真实ADC位数或上/下限。"]}
