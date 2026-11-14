"""Reproducible H/DAB analysis on original RGB, with explicit batch calibration.

Macenko's angular-extreme stain estimate is independently implemented from the
published method (doi:10.1109/ISBI.2009.5193250). Concentrations use natural-log
optical density, nonnegative least squares, and no per-image intensity scaling.
The score is a tissue-pixel weighted intensity index, never a cellular H-score.
"""
from __future__ import annotations

import hashlib
import json
from math import isfinite
from time import perf_counter
from typing import Any

import numpy as np
from scipy.special import logsumexp

from .microscopy_measurements import analysis_region

DEFAULT_VECTORS = np.array([[.650, .704, .286], [.268, .570, .776]], dtype=np.float64)
DEFAULT_VECTORS /= np.linalg.norm(DEFAULT_VECTORS, axis=1)[:, None]
MAX_SAMPLE = 100_000
BLOCK = 262_144


def _check_cancel(event):
    if event is not None and event.is_set():
        raise ValueError("分析已取消。")


def _number(value, name, minimum=0.0, maximum=None):
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是有限数值。")
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是有限数值。") from exc
    if not isfinite(value) or value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{name} 超出有效范围。")
    return value


def normalize_vectors(value):
    vectors = np.asarray(value, dtype=np.float64)
    if vectors.shape != (2, 3) or not np.isfinite(vectors).all() or np.any(vectors < 0):
        raise ValueError("H/DAB 向量须为两个非负、有限的 RGB 光密度向量 (2×3)。")
    lengths = np.linalg.norm(vectors, axis=1)
    if np.any(lengths <= 0):
        raise ValueError("染色向量不能为零。")
    # A saved unit vector must stay byte-for-byte stable across batch runs.
    # Renormalizing a length of 1 ± machine epsilon changes its last bit and
    # therefore its calibration identity, although the optical basis is equal.
    divisors = np.where(np.abs(lengths - 1) <= 4 * np.finfo(float).eps, 1., lengths)
    vectors = vectors / divisors[:, None]
    if float(vectors[0] @ vectors[1]) >= .995:
        raise ValueError("H 与 DAB 染色方向过于接近，无法可靠分离。")
    return vectors


def _rgb_source(image, params):
    if isinstance(image, dict):
        raise ValueError("高级 IHC 需要完整真实 RGB 原图，不能用伪彩或独立强度通道代替。")
    source = np.asarray(image)
    if source.ndim != 3 or source.dtype.kind not in "uif":
        raise ValueError("高级 IHC 需要三维数值 RGB/RGBA 原图。")
    axis = params.get("channel_axis")
    if axis is None:
        candidates = [a for a in (0, 2) if source.shape[a] in (3, 4)]
        if len(candidates) != 1:
            raise ValueError("RGB 颜色轴不明确；请显式指定 channel_axis。")
        axis = candidates[0]
    if isinstance(axis, bool) or not isinstance(axis, (int, np.integer)) or not -3 <= axis < 3:
        raise ValueError("channel_axis 须明确指定 RGB 颜色轴。")
    if source.shape[axis] not in (3, 4):
        raise ValueError("指定颜色轴不是 RGB/RGBA，不得把空间轴当颜色轴。")
    source = np.moveaxis(source, int(axis), -1)
    profile = params.get("locked_profile")
    specified = profile.get("rgb_white_level") if isinstance(profile, dict) else params.get("rgb_white_level")
    if specified is not None:
        white = _number(specified, "RGB 白点", np.finfo(float).tiny)
    elif source.dtype.kind in "ui":
        white = float(np.iinfo(source.dtype).max)
    else:
        white = 1.0
    return source, white


def _optical_density(rgb, white):
    values = np.asarray(rgb, dtype=np.float64)
    if not np.isfinite(values).all() or np.any(values < 0) or np.any(values > white):
        raise ValueError("原始 RGB 含无效值或超出所选白点；浮点/有效位深数据请指定正确白点。")
    if values.shape[-1] == 4:
        alpha = values[..., 3:4] / white
        values = values[..., :3] * alpha + white * (1 - alpha)
    else:
        values = values[..., :3]
    return -np.log(np.clip(values / white, 1e-6, 1.0))


def estimate_macenko(rgb, tissue_mask, white, *, percentile=1.0, cancel_event=None):
    """Estimate two OD directions with a bounded deterministic sample.

    Fit a two-dimensional covariance plane; remove angular wrap relative to the
    mean ray before estimating angular percentiles. Assignment to H/DAB is by
    best cosine agreement with reference directions, with ambiguity checks.
    """
    percentile = _number(percentile, "Macenko 角度百分位", .01, 10)
    tissue_count = int(np.count_nonzero(tissue_mask))
    if tissue_count < 128:
        raise ValueError("Macenko 至少需要 128 个组织像素；请扩大组织 ROI 或使用已校准的固定向量。")
    stride = max(1, int(np.ceil(tissue_count / MAX_SAMPLE)))
    selected, seen = [], 0
    flat_mask, flat_rgb = tissue_mask.ravel(), rgb.reshape(-1, rgb.shape[-1])
    for begin in range(0, flat_mask.size, BLOCK):
        _check_cancel(cancel_event)
        positions = np.flatnonzero(flat_mask[begin:begin + BLOCK])
        indexes = positions[(-seen) % stride::stride]
        if indexes.size:
            selected.append(flat_rgb[begin + indexes])
        seen += positions.size
    sampled_rgb = np.concatenate(selected, axis=0)
    od = _optical_density(sampled_rgb, white)
    _check_cancel(cancel_event)
    covariance = np.cov(od, rowvar=False)
    eigenvalues, basis = np.linalg.eigh(covariance)
    if eigenvalues[-1] <= 1e-10 or eigenvalues[-2] / eigenvalues[-1] < 1e-4:
        raise ValueError("组织染色变化接近单一方向，不能可靠估计两个 Macenko 向量；请选择固定校准。")
    plane = basis[:, -2:]
    projected = od @ plane
    angles = np.arctan2(projected[:, 1], projected[:, 0])
    mean_ray = np.mean(projected, axis=0)
    center = np.arctan2(mean_ray[1], mean_ray[0])
    unwrapped = center + np.angle(np.exp(1j * (angles - center)))
    extremes = np.percentile(unwrapped, [percentile, 100 - percentile])
    vectors = np.stack([plane @ np.array([np.cos(a), np.sin(a)]) for a in extremes])
    if np.any(vectors < -.05):
        raise ValueError("Macenko 估计落在非物理染色方向；请排除伪影或使用实测固定向量。")
    vectors = normalize_vectors(np.maximum(vectors, 0))
    scores = vectors @ DEFAULT_VECTORS.T
    forward, reverse = float(scores[0, 0] + scores[1, 1]), float(scores[1, 0] + scores[0, 1])
    if abs(forward - reverse) < .01:
        raise ValueError("H 与 DAB 方向归属不明确；请使用阴阳性对照校准的固定染色向量。")
    if reverse > forward:
        vectors = vectors[::-1].copy()
    details = {"algorithm": "macenko_angular_extremes", "sample_count": int(sampled_rgb.shape[0]),
               "sample_stride": stride, "sampled": stride > 1, "angle_percentile": percentile,
               "plane_variance_fraction": float(eigenvalues[-2:].sum() / eigenvalues.sum()),
               "ordering": "maximum_cosine_assignment_to_H_DAB_references",
               "assignment_margin": abs(forward - reverse)}
    return vectors, details


def deconvolve_original(rgb, white, vectors, *, cancel_event=None, return_density=True):
    """Blockwise exact two-variable NNLS; unit stain directions are preserved."""
    vectors = normalize_vectors(vectors)
    shape = rgb.shape[:2]
    flat = rgb.reshape(-1, rgb.shape[-1])
    h = np.empty(flat.shape[0], np.float32)
    d = np.empty_like(h)
    density = np.empty_like(h) if return_density else None
    cosine = float(vectors[0] @ vectors[1])
    inverse = 1.0 / (1.0 - cosine * cosine)
    residual = 0.0
    for begin in range(0, flat.shape[0], BLOCK):
        _check_cancel(cancel_event)
        end = min(begin + BLOCK, flat.shape[0])
        od = _optical_density(flat[begin:end], white)
        ph, pd = od @ vectors[0], od @ vectors[1]
        ch, cd = (ph - cosine * pd) * inverse, (pd - cosine * ph) * inverse
        pair = (ch >= 0) & (cd >= 0)
        only_h = ~pair & (ph >= pd)
        ch = np.where(pair, ch, np.where(only_h, np.maximum(ph, 0), 0))
        cd = np.where(pair, cd, np.where(~only_h, np.maximum(pd, 0), 0))
        h[begin:end], d[begin:end] = ch, cd
        if density is not None:
            density[begin:end] = np.linalg.norm(od, axis=1)
        residual += float(np.sum((od - ch[:, None] * vectors[0] - cd[:, None] * vectors[1]) ** 2))
    return h.reshape(shape), d.reshape(shape), density.reshape(shape) if density is not None else None, residual / max(1, flat.shape[0])


def _thresholds(values):
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        raise ValueError("强度分级需要三个 DAB 光密度阈值。")
    result = [_number(v, "DAB 分级阈值") for v in values]
    if not result[0] < result[1] < result[2]:
        raise ValueError("DAB 三个阈值必须严格递增：阳性 < 中等 < 强。")
    return result


def fit_dab_gmm(values, *, cancel_event=None):
    """Deterministic 4-component 1-D Gaussian mixture, with refusal criteria.

    A four-grade score needs four supported ordered populations. A constant or
    unresolved population is not silently mapped to arbitrary fixed bins.
    """
    source = np.asarray(values)
    stride = max(1, int(np.ceil(source.size / MAX_SAMPLE)))
    sample = np.asarray(source[::stride], dtype=np.float64)
    if sample.size < 128 or np.unique(sample).size < 8 or np.ptp(sample) < 1e-5:
        raise ValueError("GMM 分级数据不足或强度近似恒定；请用人工阈值/阴阳性对照。")
    span = float(np.ptp(sample))
    variance_floor = max((span * .002) ** 2, 1e-10)
    def fit_components(component_count):
        if component_count == 1:
            means = np.array([float(np.mean(sample))])
            variances = np.array([max(float(np.var(sample)), variance_floor)])
            likelihood = float(np.sum(-.5 * (np.log(2 * np.pi * variances[0]) + (sample - means[0]) ** 2 / variances[0])))
            return likelihood, means, variances, np.ones(1), 1
        best_fit = None
        for quantiles in (np.linspace(.1, .9, component_count), np.linspace(.02, .95, component_count)):
            means = np.quantile(sample, quantiles)
            variances = np.full(component_count, max(float(np.var(sample)) / (component_count ** 2), variance_floor))
            weights = np.full(component_count, 1 / component_count)
            previous = -np.inf
            converged = False
            for iteration in range(180):
                _check_cancel(cancel_event)
                logp = np.log(np.maximum(weights, 1e-12))[None, :] - .5 * (
                    np.log(2 * np.pi * variances)[None, :] + (sample[:, None] - means) ** 2 / variances)
                normalizer = logsumexp(logp, axis=1)
                likelihood = float(normalizer.sum())
                responsibilities = np.exp(logp - normalizer[:, None])
                effective = responsibilities.sum(axis=0)
                if np.any(effective < 2):
                    break
                means = (responsibilities * sample[:, None]).sum(axis=0) / effective
                variances = np.maximum((responsibilities * (sample[:, None] - means) ** 2).sum(axis=0) / effective,
                                       variance_floor)
                weights = effective / sample.size
                if isfinite(previous) and abs(likelihood - previous) < 1e-6 * (1 + abs(previous)):
                    converged = True
                    break
                previous = likelihood
            if converged and (best_fit is None or likelihood > best_fit[0]):
                best_fit = (likelihood, means.copy(), variances.copy(), weights.copy(), iteration + 1)
        return best_fit
    models = {count: fit_components(count) for count in range(1, 5)}
    bics = {count: -2 * fitted[0] + (3 * count - 1) * np.log(sample.size)
            for count, fitted in models.items() if fitted is not None}
    if models[4] is None:
        raise ValueError("GMM 未稳定收敛；请检查 ROI 或改用人工分级阈值。")
    selected_components = min(bics, key=bics.get)
    simpler_bic = min(value for count, value in bics.items() if count < 4)
    if selected_components != 4 or simpler_bic - bics[4] < 10:
        raise ValueError(f"GMM 模型比较不支持四组强度（最佳 {selected_components} 组）；请用校准阈值分级。")
    likelihood, means, variances, weights, iterations = models[4]
    order = np.argsort(means)
    means, variances, weights = means[order], variances[order], weights[order]
    separation = np.diff(means) / np.sqrt((variances[:-1] + variances[1:]) / 2)
    if np.min(weights) < .01 or np.min(separation) < 1.0:
        raise ValueError("数据不支持四个可分辨的强度组；不会强行生成四级评分，请改用校准阈值。")
    boundaries = []
    for i in range(3):
        # Locate the adjacent-component equal posterior crossing between means.
        grid = np.linspace(means[i], means[i + 1], 1025)
        first = np.log(weights[i]) - .5 * np.log(variances[i]) - (grid - means[i]) ** 2 / (2 * variances[i])
        second = np.log(weights[i + 1]) - .5 * np.log(variances[i + 1]) - (grid - means[i + 1]) ** 2 / (2 * variances[i + 1])
        diff = first - second
        crossings = np.flatnonzero(diff[:-1] * diff[1:] <= 0)
        if crossings.size != 1:
            raise ValueError("GMM 相邻强度组没有唯一稳定分界；请改用人工校准阈值。")
        j = crossings[0]
        boundaries.append(float(grid[j] + (grid[j + 1] - grid[j]) * diff[j] / (diff[j] - diff[j + 1])))
    return _thresholds(boundaries), {"algorithm": "deterministic_four_component_GMM", "components": 4,
        "means": means.tolist(), "variances": variances.tolist(), "weights": weights.tolist(),
        "sample_count": int(sample.size), "sample_stride": stride, "sampled": stride > 1,
        "iterations": iterations, "log_likelihood": likelihood, "adjacent_separation": separation.tolist(),
        "bic_candidates": {str(k): float(v) for k, v in bics.items()}, "bic_margin_over_simpler": float(simpler_bic - bics[4]),
        "interpretation": "ordered_intensity_populations_not_clinically_calibrated_grades"}


def _region_values(dab, tissue, roi, source_shape, origin):
    selection = analysis_region(roi, source_shape)
    x0, y0 = origin
    box = selection.bounds
    if box.x < x0 or box.y < y0 or box.x + box.width > x0 + dab.shape[1] or box.y + box.height > y0 + dab.shape[0]:
        raise ValueError("阴阳示例和伪影区域必须位于本次分析 ROI 内。")
    slices = (slice(box.y - y0, box.y - y0 + box.height), slice(box.x - x0, box.x - x0 + box.width))
    mask = tissue[slices] & selection.mask
    return dab[slices][mask]


def _validate_profile(profile):
    if not isinstance(profile, dict) or profile.get("version") != 1 or profile.get("measurement_basis") != "natural_log_OD":
        raise ValueError("锁定校准必须为本程序的 version=1 natural_log_OD 配方。")
    vectors = normalize_vectors(profile.get("stain_vectors"))
    thresholds = _thresholds(profile.get("grade_thresholds"))
    _number(profile.get("rgb_white_level"), "锁定白点", np.finfo(float).tiny)
    _number(profile.get("tissue_min_od"), "锁定组织阈值")
    return vectors, thresholds


def _sample_signal(signal, mask, total):
    stride = max(1, int(np.ceil(total / MAX_SAMPLE)))
    flat_signal, flat_mask = signal.ravel(), mask.ravel()
    sampled, seen = [], 0
    for begin in range(0, flat_signal.size, BLOCK):
        positions = np.flatnonzero(flat_mask[begin:begin + BLOCK])
        chosen = positions[(-seen) % stride::stride]
        if chosen.size:
            sampled.append(flat_signal[begin + chosen])
        seen += positions.size
    return np.concatenate(sampled), stride


def analyze_histology(image, params: dict[str, Any], *, cancel_event=None):
    started = perf_counter()
    if params.get("locked_profile") is not None:
        _validate_profile(params["locked_profile"])
    source, white = _rgb_source(image, params)
    selection = analysis_region(params.get("roi"), source.shape[:2])
    bounds = selection.bounds
    rgb = source[bounds.slices]
    allowed = selection.mask.copy()
    exclusions = params.get("exclude_rois", [])
    if not isinstance(exclusions, list) or len(exclusions) > 100:
        raise ValueError("伪影排除区域最多 100 个。")
    for roi in exclusions:
        exclusion = analysis_region(roi, source.shape[:2])
        box = exclusion.bounds
        if box.x < bounds.x or box.y < bounds.y or box.x + box.width > bounds.x + bounds.width or box.y + box.height > bounds.y + bounds.height:
            raise ValueError("伪影排除区域必须位于分析 ROI 内。")
        target = (slice(box.y - bounds.y, box.y - bounds.y + box.height),
                  slice(box.x - bounds.x, box.x - bounds.x + box.width))
        allowed[target] &= ~exclusion.mask
    profile = params.get("locked_profile")
    tissue_min_od = _number(profile["tissue_min_od"] if profile else params.get("tissue_min_od", .15), "组织最小光密度")
    # Tissue detection needs no full float RGB copy; blockwise whitepoint checks.
    density = np.empty(rgb.shape[:2], np.float32)
    flat = rgb.reshape(-1, rgb.shape[-1])
    for begin in range(0, flat.shape[0], BLOCK):
        _check_cancel(cancel_event)
        density.ravel()[begin:begin + BLOCK] = np.linalg.norm(_optical_density(flat[begin:begin + BLOCK], white), axis=1)
    tissue = allowed & (density > tissue_min_od)
    del density
    tissue_count = int(np.count_nonzero(tissue))
    if not tissue_count:
        raise ValueError("所选分析区域没有符合组织光密度阈值的像素；未生成虚假的零分结果。")
    warnings = ["评分按组织像素面积加权，不是逐细胞 H-score；GMM 的有序强度组需用对照确认。"]
    if profile:
        vectors, thresholds = _validate_profile(profile)
        estimation = {"algorithm": "batch_locked", "profile_id": profile.get("id"), "profile_name": profile.get("name", "")}
        grading = {"algorithm": "batch_locked", "profile_id": profile.get("id")}
    else:
        stain_method = str(params.get("stain_method", "macenko"))
        if stain_method == "macenko":
            vectors, estimation = estimate_macenko(rgb, tissue, white,
                percentile=params.get("macenko_percentile", 1.0), cancel_event=cancel_event)
        elif stain_method == "fixed":
            vectors = normalize_vectors(params.get("stain_vectors", DEFAULT_VECTORS))
            estimation = {"algorithm": "user_fixed_vectors" if params.get("stain_vectors") is not None else "standard_H_DAB_vectors"}
        else:
            raise ValueError("染色向量方法须为 macenko 或 fixed。")
    hematoxylin, dab, _, residual = deconvolve_original(rgb, white, vectors, cancel_event=cancel_event, return_density=False)
    if not profile:
        method = str(params.get("grading_method", "manual"))
        if method == "gmm":
            sampled, sampling_stride = _sample_signal(dab, tissue, tissue_count)
            thresholds, grading = fit_dab_gmm(sampled, cancel_event=cancel_event)
            grading.update(population_count=tissue_count, sample_stride=sampling_stride, sampled=sampling_stride > 1)
        elif method in {"manual", "examples"}:
            thresholds = _thresholds(params.get("grade_thresholds", [.2, .5, .8]))
            grading = {"algorithm": "manual_fixed_OD_thresholds"}
            if method == "examples":
                examples = params.get("example_rois", {})
                if not isinstance(examples, dict) or not examples.get("negative") or not examples.get("positive"):
                    raise ValueError("示例校准需要分别框选阴性与阳性区域。")
                negative = _region_values(dab, tissue, examples["negative"], source.shape[:2], (bounds.x, bounds.y))
                positive = _region_values(dab, tissue, examples["positive"], source.shape[:2], (bounds.x, bounds.y))
                if min(negative.size, positive.size) < 16:
                    raise ValueError("每个阴阳示例至少需要 16 个有效组织像素。")
                neg_high, pos_low = float(np.quantile(negative, .95)), float(np.quantile(positive, .05))
                if neg_high >= pos_low:
                    raise ValueError("阴阳示例强度范围重叠，无法稳定校准；请重新框选代表区域。")
                thresholds = _thresholds([(neg_high + pos_low) / 2, thresholds[1], thresholds[2]])
                grading = {"algorithm": "negative_q95_positive_q05_midpoint", "positive_count": int(positive.size),
                    "negative_count": int(negative.size), "negative_q95": neg_high, "positive_q05": pos_low,
                    "upper_grades": "explicit_manual_thresholds", "example_rois": examples}
                warnings.append("阴阳示例仅重新校准阳性界线；中、强两条界线保留明确设置值。")
        else:
            raise ValueError("强度分级方法须为 manual、examples 或 gmm。")
    counts = np.zeros(4, np.int64)
    flat_dab, flat_tissue = dab.ravel(), tissue.ravel()
    for begin in range(0, flat_dab.size, BLOCK):
        _check_cancel(cancel_event)
        part = flat_dab[begin:begin + BLOCK][flat_tissue[begin:begin + BLOCK]]
        counts += np.bincount(np.searchsorted(thresholds, part, side="right"), minlength=4)
    fractions = counts / tissue_count
    score = float(np.dot(np.arange(4), fractions) * 100)
    rows = [{"grade": name, "area_px": int(counts[i]), "fraction": float(fractions[i]),
             "percent": float(fractions[i] * 100), "lower_dab_od": None if i == 0 else thresholds[i - 1],
             "upper_dab_od": thresholds[i] if i < 3 else None}
            for i, name in enumerate(["0", "1+", "2+", "3+"])]
    calibration_profile = {"version": 1, "measurement_basis": "natural_log_OD",
        "name": str(params.get("profile_name") or (profile or {}).get("name") or "IHC 批次校准")[:200],
        "stain_vectors": vectors.tolist(), "grade_thresholds": thresholds,
        "rgb_white_level": white, "tissue_min_od": tissue_min_od,
        "origin_source": (profile or {}).get("origin_source", params.get("source") or {
            "asset_id": params.get("asset_id"), "series": params.get("series", 0), "frame": params.get("frame", 0)}),
        "origin_methods": (profile or {}).get("origin_methods", {
            "stain": estimation["algorithm"], "grading": grading["algorithm"]})}
    calibration_profile["id"] = hashlib.sha256(json.dumps(calibration_profile, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]
    summary = {"tissue_area_px": tissue_count, "excluded_area_px": int(np.count_nonzero(selection.mask & ~allowed)),
        "positive_area_px": int(counts[1:].sum()), "positive_fraction": float(fractions[1:].sum()),
        "mean_dab_od_in_tissue": float(np.sum(dab, where=tissue, dtype=np.float64) / tissue_count),
        "integrated_dab_od": float(np.sum(dab, where=tissue, dtype=np.float64)),
        "mean_hematoxylin_od_in_tissue": float(np.sum(hematoxylin, where=tissue, dtype=np.float64) / tissue_count),
        "pixel_weighted_score": score, "score_range": "0–300", "score_unit": "tissue_pixel_area_weighted",
        "negative_percent": float(fractions[0] * 100), "weak_percent": float(fractions[1] * 100),
        "moderate_percent": float(fractions[2] * 100), "strong_percent": float(fractions[3] * 100),
        "deconvolution_mean_squared_residual": residual}
    scale = params.get("um_per_px")
    if scale is not None:
        scale = _number(scale, "像素标定", np.finfo(float).tiny)
        summary["tissue_area_um2"] = tissue_count * scale * scale
        summary["positive_area_um2"] = int(counts[1:].sum()) * scale * scale
        for row in rows:
            row["area_um2"] = row["area_px"] * scale * scale
    else:
        warnings.append("未提供像素标定，仅报告 px²，不推测 µm²。")
    if residual > .05:
        warnings.append("二染色拟合残差较高，请检查伪影、染色种类或使用实测染色向量。")
    preview_stride = max(1, int(np.ceil(max(dab.shape) / 256)))
    preview = np.full(dab[::preview_stride, ::preview_stride].shape, -1, dtype=np.int8)
    preview_tissue = tissue[::preview_stride, ::preview_stride]
    preview[preview_tissue] = np.searchsorted(thresholds, dab[::preview_stride, ::preview_stride][preview_tissue], side="right")
    result = {"kind": "microscopy", "analysis_method": "ihc_advanced", "count": None, "summary": summary,
        "rows": rows, "objects": [], "warnings": warnings, "calibration_profile": calibration_profile,
        "grade_preview": {"width": int(preview.shape[1]), "height": int(preview.shape[0]), "stride": preview_stride,
            "origin": [bounds.x, bounds.y], "values": preview.ravel().tolist(), "legend": ["background", "0", "1+", "2+", "3+"]},
        "method": {"name": "ihc_advanced", "measurement_basis": "original_RGB_natural_log_OD",
            "display_lut_used": False, "source": params.get("source"), "roi": selection.geometry,
            "stain_estimation": estimation, "stain_vectors": vectors.tolist(), "grading": grading,
            "grade_thresholds": thresholds, "tissue_min_od": tissue_min_od, "rgb_white_level": white,
            "locked_profile": bool(profile), "exclude_rois": exclusions, "grade_boundary_rule": "lower_inclusive"},
        "elapsed_ms": round((perf_counter() - started) * 1000, 3)}
    return result
