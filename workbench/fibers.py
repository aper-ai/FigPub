"""Fiber cross-sections and myotubes: geometry, nuclei, phenotype and review.

Measurements always use original scalar planes. Imported instance labels remain
explicit data; threshold/Cellpose output is a candidate segmentation. Physical
measurements require real pixel calibration. No specialized model is implied.
"""
from __future__ import annotations

from collections import Counter
from math import hypot, isfinite, pi, sqrt
from time import perf_counter
from typing import Any

import numpy as np
from scipy import ndimage
from scipy.spatial import ConvexHull, QhullError

from .microscopy_measurements import analysis_region, resolve_channels


def _cancel(event):
    if event is not None and event.is_set():
        raise ValueError("分析已取消。")


def _float(value, name, low=0., high=None):
    if isinstance(value, bool):
        raise ValueError(f"{name} 须为有限数值。")
    try:
        value = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{name} 须为有限数值。") from exc
    if not isfinite(value) or value < low or high is not None and value > high:
        raise ValueError(f"{name} 超出有效范围。")
    return value


def _integer(value, name, low=1, high=16_000_000):
    result = _float(value, name, low, high)
    if result != int(result):
        raise ValueError(f"{name} 须为整数。")
    return int(result)


def _scale(params):
    scalar = params.get("um_per_px")
    sx, sy = params.get("um_per_px_x", scalar), params.get("um_per_px_y", scalar)
    if sx is None and sy is None:
        return None
    if sx is None or sy is None:
        raise ValueError("请同时提供 x/y 标定或统一 um_per_px。")
    return _float(sx, "x 像素标定", np.finfo(float).tiny), _float(sy, "y 像素标定", np.finfo(float).tiny)


def _channel_map(image, params):
    channels = resolve_channels(image, params, default_all=True)
    return channels, {item.channel_id: item for item in channels}


def _choose(mapping, value, name, default=None):
    identifier = str(value or default or "")
    if identifier not in mapping:
        raise ValueError(f"请选择有效的 {name} 原始通道。")
    return mapping[identifier]


def _dense_labels(labels):
    identities, inverse = np.unique(labels, return_inverse=True)
    if identities[0] != 0:
        identities = np.insert(identities, 0, 0)
        inverse += 1
    if identities.size - 1 > 100_000:
        raise ValueError("实例超过 100,000 个；请缩小分析 ROI。")
    return inverse.reshape(labels.shape).astype(np.int32), identities


def validate_labels(labels, shape):
    array = np.asarray(labels)
    if array.shape != shape or array.ndim != 2 or array.dtype.kind not in "ui":
        raise ValueError("实例标签须为同尺寸的二维非负整数原始通道，0 为背景。")
    if array.size and (int(array.min()) < 0 or int(array.max()) > np.iinfo(np.uint32).max):
        raise ValueError("标签 ID 必须在 uint32 范围内。")
    _dense_labels(array)
    return array.astype(np.uint32, copy=True)


def _threshold(raw, valid, method, manual):
    if method == "manual":
        return _float(manual, "原始分割阈值", -np.inf)
    if method != "otsu":
        raise ValueError("纤维/细胞核阈值方法须为 manual 或 otsu。")
    values = np.asarray(raw[valid], dtype=np.float64)
    if not values.size:
        raise ValueError("所选区域没有有限原始像素。")
    if np.ptp(values) <= max(1e-12, abs(float(values[0])) * 1e-12):
        raise ValueError("强度近似恒定，不能自动分割；请核对通道或指定标签。")
    from skimage.filters import threshold_otsu
    return float(threshold_otsu(values))


def _filtered(labels, roi_mask, params, *, nuclear=False):
    prefix = "nucleus_" if nuclear else ""
    minimum = _integer(params.get(prefix + "min_area", 12 if nuclear else 50), "最小面积")
    maximum = params.get(prefix + "max_area")
    maximum = _integer(maximum, "最大面积", minimum) if maximum is not None else None
    dense, ids = _dense_labels(labels)
    counts = np.bincount(dense.ravel())
    allowed = counts >= minimum
    if maximum is not None:
        allowed &= counts <= maximum
    allowed[0] = False
    if not nuclear and params.get("exclude_border", False):
        edge = roi_mask & ~ndimage.binary_erosion(roi_mask, border_value=0)
        allowed[np.unique(dense[edge])] = False
    keep = allowed[dense]
    result = np.where(keep, labels, 0).astype(np.uint32)
    return result, int(np.count_nonzero((counts[1:] > 0) & ~allowed[1:]))


def _segment(raw, roi_mask, params, *, nuclear=False, cancel_event=None):
    prefix = "nucleus_" if nuclear else ""
    finite = np.isfinite(raw) & roi_mask
    sigma = _float(params.get(prefix + "sigma", 1.0), "平滑 sigma", 0, 20)
    values = np.asarray(raw, dtype=np.float64)
    if not finite.any():
        raise ValueError("分割通道没有有效原始值。")
    # Normalized convolution prevents NaN holes/outside-ROI zeros biasing edges.
    if sigma:
        denominator = ndimage.gaussian_filter(finite.astype(float), sigma)
        smooth = ndimage.gaussian_filter(np.where(finite, values, 0), sigma) / np.maximum(denominator, 1e-12)
    else:
        smooth = values
    threshold = _threshold(smooth, finite, params.get(prefix + "threshold_method", "otsu"), params.get(prefix + "threshold"))
    polarity = params.get(prefix + "polarity", "bright")
    if polarity not in {"bright", "dark"}:
        raise ValueError("结构极性须为 bright 或 dark。")
    mask = ((smooth > threshold) if polarity == "bright" else (smooth < threshold)) & finite
    _cancel(cancel_event)
    input_kind = "filled" if nuclear else str(params.get("fiber_input", "filled"))
    if input_kind == "membrane":
        radius = _integer(params.get("membrane_close_radius", 1), "边界闭合半径", 0, 20)
        if radius:
            from skimage.morphology import disk
            mask = ndimage.binary_closing(mask, structure=disk(radius)) & finite
        interiors = ~mask & finite
        labels, _ = ndimage.label(interiors)
        edge = finite & ~ndimage.binary_erosion(finite, border_value=0)
        exterior = np.unique(labels[edge])
        labels[np.isin(labels, exterior)] = 0
    elif input_kind == "filled":
        mask = ndimage.binary_fill_holes(mask) & finite
        if params.get(prefix + "split_touching", True) and mask.any():
            from skimage.feature import peak_local_max
            from skimage.segmentation import watershed
            distance = ndimage.distance_transform_edt(np.pad(mask, 1))[1:-1, 1:-1]
            separation = _integer(params.get(prefix + "min_distance", 5 if nuclear else 12), "分离种子最小间距", 1, 1000)
            peaks = peak_local_max(distance, labels=mask, min_distance=separation, exclude_border=False)
            seeds = np.zeros(mask.shape, np.int32)
            if peaks.size:
                seeds[tuple(peaks.T)] = np.arange(1, len(peaks) + 1)
                labels = watershed(-distance, seeds, mask=mask)
            else:
                labels, _ = ndimage.label(mask)
        else:
            labels, _ = ndimage.label(mask)
    else:
        raise ValueError("纤维输入须为 filled 或 membrane。")
    labels, removed = _filtered(labels, roi_mask, params, nuclear=nuclear)
    return labels, {"algorithm": "threshold_watershed" if input_kind == "filled" else "closed_membrane_interiors",
                    "threshold": threshold, "threshold_method": params.get(prefix + "threshold_method", "otsu"),
                    "polarity": polarity, "sigma": sigma, "filtered_objects": removed}


def minimum_feret(mask, *, spacing=(1.0, 1.0)):
    """Minimum caliper width of the convex hull of closed pixel squares.

    Pixel squares, not pixel-center spread, make a 1×N bar one pixel wide.
    Rotating calipers examine antipodal supports in linear hull time.
    spacing is (x,y); anisotropic physical widths are recomputed geometrically.
    """
    mask = np.asarray(mask, dtype=bool)
    boundary = mask & ~ndimage.binary_erosion(mask, border_value=0)
    y, x = np.nonzero(boundary)
    if not x.size:
        return None
    sx, sy = spacing
    centers = np.column_stack((x * sx, y * sy))
    if len(centers) >= 3:
        try:
            centers = centers[ConvexHull(centers).vertices]
        except QhullError:
            # Collinear boundary: its endpoints suffice for the square dilation.
            axis = int(np.argmax(np.ptp(centers, axis=0)))
            centers = centers[[np.argmin(centers[:, axis]), np.argmax(centers[:, axis])]]
    offsets = np.array([[0, 0], [sx, 0], [sx, sy], [0, sy]])
    corners = (centers[:, None, :] + offsets).reshape(-1, 2)
    hull = corners[ConvexHull(corners).vertices]
    n, opposite, answer = len(hull), 1, np.inf
    for i in range(n):
        start, end = hull[i], hull[(i + 1) % n]
        edge = end - start
        def distance(j):
            delta = hull[j] - start
            return edge[0] * delta[1] - edge[1] * delta[0]
        for _ in range(n):
            nxt = (opposite + 1) % n
            if distance(nxt) > distance(opposite) + 1e-12:
                opposite = nxt
            else:
                break
        answer = min(answer, distance(opposite) / np.linalg.norm(edge))
    return float(answer)


def skeleton_length(mask, *, spacing=(1.0, 1.0)):
    from skimage.morphology import skeletonize
    skeleton = skeletonize(mask)
    sx, sy = spacing
    horizontal = int(np.count_nonzero(skeleton[:, :-1] & skeleton[:, 1:]))
    vertical = int(np.count_nonzero(skeleton[:-1] & skeleton[1:]))
    # Diagonal corner links are omitted where an orthogonal connection exists,
    # so a staircase does not create a triangular graph at every corner.
    diagonal_a = skeleton[:-1, :-1] & skeleton[1:, 1:] & ~skeleton[:-1, 1:] & ~skeleton[1:, :-1]
    diagonal_b = skeleton[:-1, 1:] & skeleton[1:, :-1] & ~skeleton[:-1, :-1] & ~skeleton[1:, 1:]
    length = horizontal * sx + vertical * sy + int(np.count_nonzero(diagonal_a) + np.count_nonzero(diagonal_b)) * hypot(sx, sy)
    return float(length), int(np.count_nonzero(skeleton))


def _contour(mask, origin, max_points=512):
    from skimage.measure import find_contours
    paths = find_contours(np.pad(mask.astype(np.uint8), 1), .5)
    if not paths:
        return []
    longest = max(paths, key=len)
    stride = max(1, int(np.ceil(len(longest) / max_points)))
    ox, oy = origin
    return [[float(x - 1 + ox), float(y - 1 + oy)] for y, x in longest[::stride]]


def _nuclear_assignments(labels, nuclear_labels, objects, params, origin, spacing):
    from skimage.measure import regionprops
    dense, original_ids = _dense_labels(nuclear_labels)
    if len(original_ids) - 1 > 20_000:
        raise ValueError("当前 ROI 的细胞核超过 20,000 个，请缩小范围以保留完整逐核归属和复核记录。")
    by_id = {item["id"]: item for item in objects}
    distance_map = np.zeros(labels.shape, np.float32)
    radius = {}
    fiber_dense, fiber_ids = _dense_labels(labels)
    for component in regionprops(fiber_dense):
        identifier = int(fiber_ids[component.label])
        distance = ndimage.distance_transform_edt(np.pad(component.image, 1), sampling=(spacing[1], spacing[0]))[1:-1, 1:-1]
        view = distance_map[component.slice]
        view[component.image] = distance[component.image]
        radius[identifier] = float(distance.max())
    min_overlap = _float(params.get("nucleus_min_overlap", .5), "细胞核归属最小覆盖率", .5, 1)
    peripheral = _float(params.get("peripheral_fraction", .2), "周边带比例", .01, .99)
    entries = []
    for component in regionprops(dense):
        pixels = component.image
        fibers = labels[component.slice][pixels]
        ids, counts = np.unique(fibers, return_counts=True)
        overlaps = sorted([(int(i), int(c)) for i, c in zip(ids, counts) if i], key=lambda pair: (-pair[1], pair[0]))
        candidates = [identifier for identifier, n in overlaps if n / component.area >= .15]
        winner = overlaps[0][0] if overlaps else None
        fraction = overlaps[0][1] / component.area if overlaps else 0.
        category, reason, edge_ratio = "outside", "no_fiber_overlap", None
        assigned = None
        if overlaps:
            if fraction < min_overlap or len(candidates) > 1:
                category, reason = "uncertain", "insufficient_or_multiple_fiber_overlap"
            else:
                distances = distance_map[component.slice][pixels & (labels[component.slice] == winner)]
                edge_ratio = float(np.quantile(distances, .1) / max(radius[winner], 1e-12))
                category = "central" if edge_ratio > peripheral else "peripheral"
                reason = "nuclear_body_distance_percentile"
                assigned = winner
                by_id[winner]["nuclei_count"] += 1
                by_id[winner][category + "_nuclei"] += 1
        if category == "uncertain":
            for identifier in candidates or ([winner] if winner else []):
                by_id[identifier]["uncertain_nuclei"] += 1
        cy, cx = component.centroid
        entries.append({"id": int(original_ids[component.label]), "x": float(cx + origin[0]), "y": float(cy + origin[1]),
            "area_px": int(component.area), "fiber_id": assigned, "candidate_fiber_ids": candidates,
            "overlap_fraction": float(fraction), "classification": category, "reason": reason,
            "normalized_edge_distance_q10": edge_ratio,
            "bbox": {"x": int(component.bbox[1] + origin[0]), "y": int(component.bbox[0] + origin[1]),
                     "width": int(component.bbox[3] - component.bbox[1]), "height": int(component.bbox[2] - component.bbox[0])}})
    return entries


def _phenotype(objects, labels, mapping, params, slices):
    settings = params.get("fiber_markers", [])
    if not isinstance(settings, list) or len(settings) > 16:
        raise ValueError("纤维分型最多支持 16 个明确指定的标记通道。")
    if not settings:
        return []
    names = set()
    specifications = []
    for item in settings:
        if not isinstance(item, dict):
            raise ValueError("每个分型标记必须包含通道、名称和原始强度阈值。")
        channel = _choose(mapping, item.get("channel_id"), "分型标记")
        name = str(item.get("name") or channel.name)[:100]
        if name in names:
            raise ValueError("纤维分型名称不能重复。")
        names.add(name)
        specifications.append((channel, name, _float(item.get("threshold"), "分型标记阈值", -np.inf)))
    dense, ids = _dense_labels(labels)
    counts = np.bincount(dense.ravel())
    means = {}
    for channel, name, threshold in specifications:
        values = channel.values[slices]
        valid = np.isfinite(values)
        finite_counts = np.bincount(dense[valid], minlength=len(ids))
        totals = np.bincount(dense[valid], weights=np.asarray(values[valid], dtype=np.float64), minlength=len(ids))
        means[name] = np.divide(totals, finite_counts, out=np.full(len(ids), np.nan), where=finite_counts > 0)
    indices = {int(identifier): index for index, identifier in enumerate(ids)}
    classes = Counter()
    for item in objects:
        index = indices[item["id"]]
        item["marker_means"] = {name: float(means[name][index]) if np.isfinite(means[name][index]) else None for _, name, _ in specifications}
        if any(value is None for value in item["marker_means"].values()):
            phenotype = "unresolved"
        else:
            positive = [name for _, name, threshold in specifications if item["marker_means"][name] >= threshold]
            phenotype = "+".join(positive) if positive else "negative"
        item["phenotype"] = phenotype
        classes[phenotype] += 1
    return [{"phenotype": key, "fiber_count": value, "fraction": value / len(objects) if objects else None}
            for key, value in sorted(classes.items())]


def recompute_fiber_result(image, params, labels, *, origin=(0, 0), cancel_event=None,
                           segmentation_details=None):
    started = perf_counter()
    method = str(params.get("method", "fiber_cross_section"))
    if method not in {"fiber_cross_section", "myotube"}:
        raise ValueError("纤维方法须为 fiber_cross_section 或 myotube。")
    channels, mapping = _channel_map(image, params)
    primary = _choose(mapping, params.get("fiber_channel_id"), "纤维结构", channels[0].channel_id)
    selection = analysis_region(params.get("roi"), primary.values.shape)
    region = selection.bounds
    if tuple(origin) != (region.x, region.y):
        raise ValueError("实例标签原点与原始分析 ROI 不一致。")
    labels = validate_labels(labels, selection.mask.shape)
    if np.any(labels[~selection.mask]):
        raise ValueError("实例标签包含分析 ROI 外的像素。")
    source = primary.values[region.slices]
    calibration = _scale(params)
    spacing = calibration or (1., 1.)
    dense, ids = _dense_labels(labels)
    from skimage.measure import regionprops
    properties = regionprops(dense)
    maximum = _integer(params.get("max_objects", 10000), "对象明细上限", 1, 100000)
    if len(properties) > maximum:
        raise ValueError(f"本次 {len(properties)} 个纤维超过明细上限 {maximum}；请缩小 ROI，以免核归属/统计仅测部分对象。")
    objects, warnings = [], []
    contour_points = max(8, min(512, 125_000 // max(1, len(properties))))
    if contour_points < 512:
        warnings.append(f"为限制工程大小，每条显示轮廓最多 {contour_points} 个点；计量与实例复核仍使用完整标签像素。")
    if not calibration:
        warnings.append("未提供像素标定，几何仅报告 px/px²；核边界距离使用像素尺度。")
    roi_edge = selection.mask & ~ndimage.binary_erosion(selection.mask, border_value=0)
    for component in properties:
        _cancel(cancel_event)
        identifier = int(ids[component.label])
        mask = component.image
        raw = source[component.slice][mask]
        finite = raw[np.isfinite(raw)]
        with np.errstate(over="ignore", invalid="ignore"):
            integrated = float(np.sum(finite, dtype=np.float64)) if finite.size else None
            mean = integrated / finite.size if finite.size else None
        if integrated is not None and not isfinite(integrated):
            raise ValueError("原始纤维强度积分超出 float64 可表示范围；未输出无穷值。")
        y0, x0, y1, x1 = component.bbox
        cy, cx = component.centroid
        minor = float(component.axis_minor_length)
        major = float(component.axis_major_length)
        feret = minimum_feret(mask)
        area = int(component.area)
        item = {"id": identifier, "x": float(cx + region.x), "y": float(cy + region.y),
            "bbox": {"x": x0 + region.x, "y": y0 + region.y, "width": x1 - x0, "height": y1 - y0},
            "area_px": area, "equivalent_diameter_px": sqrt(4 * area / pi), "min_feret_px": feret,
            "major_axis_length_px": major, "minor_axis_length_px": minor,
            "aspect_ratio": major / minor if minor > 0 else None,
            "orientation_rad": float(component.orientation), "solidity": float(component.solidity),
            "mean_intensity": mean, "integrated_intensity": integrated,
            "valid_intensity_pixels": int(finite.size),
            "touches_roi_border": bool(np.any(roi_edge[component.slice] & mask)),
            "nuclei_count": 0, "central_nuclei": 0, "peripheral_nuclei": 0, "uncertain_nuclei": 0}
        if params.get("include_contours", True):
            item["contour"] = _contour(mask, (x0 + region.x, y0 + region.y), max_points=contour_points)
        if calibration:
            item["area_um2"] = area * calibration[0] * calibration[1]
            item["equivalent_diameter_um"] = sqrt(4 * item["area_um2"] / pi)
            item["min_feret_um"] = minimum_feret(mask, spacing=calibration)
            physical = regionprops(mask.astype(np.uint8), spacing=(calibration[1], calibration[0]))[0]
            item["major_axis_length_um"] = float(physical.axis_major_length)
            item["minor_axis_length_um"] = float(physical.axis_minor_length)
        if method == "myotube":
            item["skeleton_length_px"], item["skeleton_pixel_count"] = skeleton_length(mask)
            if calibration:
                item["skeleton_length_um"], _ = skeleton_length(mask, spacing=calibration)
        objects.append(item)
    nuclear_channel_id = params.get("nucleus_channel_id")
    nuclear_labels, nuclear_details = None, None
    if params.get("nucleus_label_channel_id"):
        nuclear_channel = _choose(mapping, params["nucleus_label_channel_id"], "细胞核标签")
        if nuclear_channel.channel_id == primary.channel_id:
            raise ValueError("细胞核标签与纤维结构必须使用独立的原始通道。")
        nuclear_labels = validate_labels(nuclear_channel.values[region.slices], labels.shape)
        nuclear_labels[~selection.mask] = 0
        nuclear_labels, removed = _filtered(nuclear_labels, selection.mask, params, nuclear=True)
        nuclear_details = {"algorithm": "imported_integer_labels", "source": nuclear_channel.metadata(), "filtered_objects": removed}
    elif nuclear_channel_id:
        nuclear_channel = _choose(mapping, nuclear_channel_id, "细胞核")
        if nuclear_channel.channel_id == primary.channel_id:
            raise ValueError("细胞核强度与纤维结构必须使用独立的原始通道。")
        nuclear_labels, nuclear_details = _segment(nuclear_channel.values[region.slices], selection.mask, params,
                                                   nuclear=True, cancel_event=cancel_event)
        nuclear_details["source"] = nuclear_channel.metadata()
    nuclei = _nuclear_assignments(labels, nuclear_labels, objects, params, (region.x, region.y), spacing) if nuclear_labels is not None else []
    if nuclear_labels is None:
        warnings.append("未提供细胞核强度/标签通道，核计数、中央核比例与融合指数为空，不按 0 报告。")
        for item in objects:
            for key in ("nuclei_count", "central_nuclei", "peripheral_nuclei", "uncertain_nuclei"):
                item[key] = None
    phenotypes = _phenotype(objects, labels, mapping, params, region.slices)
    counts = Counter(item["classification"] for item in nuclei)
    reliable = [item for item in objects if item["uncertain_nuclei"] == 0] if nuclear_labels is not None else []
    total_nuclei = len(nuclei) if nuclear_labels is not None else None
    minimum_fusion = _integer(params.get("fusion_min_nuclei", 2), "融合肌管最少细胞核数", 2, 1000)
    fused = [item for item in reliable if item["nuclei_count"] >= minimum_fusion]
    fused_nuclei = sum(item["nuclei_count"] for item in fused)
    summary = {"count": len(objects), "object_type": "fiber_cross_section" if method == "fiber_cross_section" else "myotube_candidate",
        "total_area_px": sum(item["area_px"] for item in objects),
        "mean_area_px": float(np.mean([item["area_px"] for item in objects])) if objects else None,
        "mean_min_feret_px": float(np.mean([item["min_feret_px"] for item in objects])) if objects else None,
        "nuclei_total": total_nuclei, "nuclei_assigned": counts["central"] + counts["peripheral"] if total_nuclei is not None else None,
        "nuclei_outside": counts["outside"] if total_nuclei is not None else None,
        "nuclei_uncertain": counts["uncertain"] if total_nuclei is not None else None,
        "central_nuclei": counts["central"] if total_nuclei is not None else None,
        "peripheral_nuclei": counts["peripheral"] if total_nuclei is not None else None,
        "reliable_nuclear_fibers": len(reliable) if total_nuclei is not None else None,
        "centrally_nucleated_fiber_fraction": sum(item["central_nuclei"] > 0 for item in reliable) / len(reliable) if reliable else None}
    if method == "myotube":
        summary.update({"fusion_index": fused_nuclei / total_nuclei if total_nuclei else None,
            "fusion_numerator_nuclei": fused_nuclei if total_nuclei is not None else None,
            "fusion_denominator_nuclei": total_nuclei, "fused_myotubes": len(fused) if total_nuclei is not None else None})
    if calibration:
        summary["total_area_um2"] = sum(item["area_um2"] for item in objects)
        summary["mean_min_feret_um"] = float(np.mean([item["min_feret_um"] for item in objects])) if objects else None
    if counts["uncertain"]:
        warnings.append("跨多个纤维或覆盖不足的细胞核标为不确定；涉及纤维不纳入可靠中央核比例/融合分子。")
    result = {"kind": "microscopy", "analysis_method": method, "count": len(objects), "objects": objects,
        "nuclei": nuclei, "phenotypes": phenotypes, "summary": summary, "warnings": warnings,
        "central_nuclei_distribution": [{"central_nuclei": key, "fiber_count": value}
            for key, value in sorted(Counter(item["central_nuclei"] for item in reliable).items())],
        "method": {"name": method, "measurement_basis": "original_pixels", "display_lut_used": False,
            "source_channels": [item.metadata() for item in channels], "source": primary.source,
            "roi": selection.geometry, "calibration": {"um_per_px_x": calibration[0], "um_per_px_y": calibration[1]} if calibration else None,
            "segmentation": segmentation_details or {"algorithm": "reviewed_instance_labels"},
            "nuclear_segmentation": nuclear_details, "nucleus_min_overlap": params.get("nucleus_min_overlap", .5),
            "peripheral_fraction": params.get("peripheral_fraction", .2), "central_rule": "nuclear_body_distance_q10 / fiber_max_distance > peripheral_fraction",
            "minimum_feret_definition": "minimum_caliper_width_of_convex_hull_of_closed_pixel_squares",
            "skeleton_definition": "sum_of_8_connected_skeleton_edges_without_redundant_corner_diagonals",
            "fusion_definition": "nuclei_in_unambiguous_myotubes_with_at_least_min_nuclei / all_observed_nuclei_in_ROI",
            "fusion_min_nuclei": minimum_fusion, "fiber_markers": params.get("fiber_markers", [])},
        "elapsed_ms": round((perf_counter() - started) * 1000, 3)}
    from .instances import attach_instance_mask
    attach_instance_mask(result, labels, origin=(region.x, region.y), source_shape=primary.values.shape)
    return result


def analyze_fibers(image, params, *, cancel_event=None):
    started = perf_counter()
    channels, mapping = _channel_map(image, params)
    primary = _choose(mapping, params.get("fiber_channel_id"), "纤维结构", channels[0].channel_id)
    selection = analysis_region(params.get("roi"), primary.values.shape)
    bounds = selection.bounds
    segmentation = str(params.get("segmentation", "threshold"))
    if segmentation == "labels":
        label_channel = _choose(mapping, params.get("label_channel_id"), "纤维实例标签")
        if label_channel.channel_id == primary.channel_id:
            raise ValueError("结构强度通道和实例标签通道必须明确分开。")
        labels = validate_labels(label_channel.values[bounds.slices], selection.mask.shape)
        labels[~selection.mask] = 0
        labels, filtered = _filtered(labels, selection.mask, params)
        details = {"algorithm": "imported_integer_labels", "label_source": label_channel.metadata(), "filtered_objects": filtered}
    elif segmentation == "cellpose":
        from .cellpose_runtime import analyze_cellpose
        from .instances import decode_instance_mask
        model_params = {**params, "method": "cellpose", "model": "cyto3", "channels": [
            {"id": primary.channel_id, "name": primary.name, "source": primary.source}], "include_instance_mask": True}
        model_result = analyze_cellpose({primary.channel_id: primary.values}, model_params, cancel_event=cancel_event)
        if not model_result.get("instance_mask"):
            raise ValueError("Cellpose 未返回可复核实例掩膜；请缩小 ROI 或导入标签通道。")
        labels = decode_instance_mask(model_result["instance_mask"])
        labels, filtered = _filtered(labels, selection.mask, params)
        details = {"algorithm": "cellpose_cyto3_general_purpose", "model_details": model_result.get("method"), "filtered_objects": filtered}
    elif segmentation == "threshold":
        labels, details = _segment(primary.values[bounds.slices], selection.mask, params, cancel_event=cancel_event)
    else:
        raise ValueError("纤维分割方法须为 threshold、labels 或 cellpose。")
    result = recompute_fiber_result(image, params, labels, origin=(bounds.x, bounds.y),
                                    cancel_event=cancel_event, segmentation_details=details)
    if segmentation == "cellpose":
        result["warnings"].append("使用通用 cyto3 模型产生纤维候选，不声称等同于专用肌纤维/肌管训练模型。")
    result["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
    return result
