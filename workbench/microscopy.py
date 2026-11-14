"""Reviewable classical microscopy measurements on original arrays.

New Figure Workbench implementation (AGPL-3.0-only), not a port of a desktop UI.
Segmentation and display are separate from measurement. The returned contours
and centroids refer to the original image, including when an ROI is selected.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite, sqrt
from time import perf_counter
from typing import Any, Mapping

import numpy as np
from scipy import ndimage

from .analysis import PixelRegion, select_plane, source_range


@dataclass(frozen=True, slots=True)
class SpatialCalibration:
    x_um_per_px: float
    y_um_per_px: float

    @classmethod
    def parse(cls, params: Mapping[str, Any]) -> "SpatialCalibration | None":
        scalar = params.get("um_per_px")
        x_value = params.get("um_per_px_x", scalar)
        y_value = params.get("um_per_px_y", scalar)
        if x_value is None and y_value is None:
            return None
        if x_value is None or y_value is None:
            raise ValueError("请同时提供 x/y 像素标定，或统一 um_per_px。")
        x_value, y_value = float(x_value), float(y_value)
        if not all(isfinite(value) and value > 0 for value in (x_value, y_value)):
            raise ValueError("像素标定须为正的有限数值，单位 µm/px。")
        return cls(x_value, y_value)

    @property
    def pixel_area_um2(self) -> float:
        return self.x_um_per_px * self.y_um_per_px


def _summary(values: np.ndarray) -> dict[str, float | int | None]:
    finite = np.asarray(values, dtype=np.float64)
    finite = finite[np.isfinite(finite)]
    if not finite.size:
        return {"pixel_count": 0, "mean_intensity": None, "integrated_intensity": None,
                "min_intensity": None, "max_intensity": None, "sd_intensity": None}
    with np.errstate(over="ignore", invalid="ignore"):
        total = float(np.sum(finite, dtype=np.float64))
        deviation = float(np.std(finite))
    if not isfinite(total) or not isfinite(deviation):
        raise ValueError("原始强度超出 float64 可安全统计的范围，未输出无穷值。")
    return {"pixel_count": int(finite.size), "mean_intensity": total / finite.size,
            "integrated_intensity": total,
            "min_intensity": float(np.min(finite)), "max_intensity": float(np.max(finite)),
            "sd_intensity": deviation}


def _region_objects(labels: np.ndarray, raw: np.ndarray, region: PixelRegion,
                    calibration: SpatialCalibration | None, include_contours: bool,
                    max_objects: int, roi_mask: np.ndarray | None = None) -> tuple[list[dict[str, Any]], int, list[str]]:
    """Vectorized intensities; only optional boundary tracing visits each object."""
    from skimage.measure import find_contours, regionprops

    object_count = int(labels.max())
    if not object_count:
        return [], 0, []
    flat_labels = labels.ravel()
    finite_raw = np.nan_to_num(raw, copy=True, nan=0.0, posinf=0.0, neginf=0.0)
    totals = np.bincount(flat_labels, weights=finite_raw.ravel(), minlength=object_count + 1)
    warnings: list[str] = []
    roi_edge = (roi_mask & ~ndimage.binary_erosion(roi_mask, border_value=0)) if roi_mask is not None else None
    properties = regionprops(labels)
    if len(properties) > max_objects:
        warnings.append(f"完整计数为 {len(properties)}，对象明细仅返回前 {max_objects} 个；请缩小 ROI 获取全部轮廓。")
    objects: list[dict[str, Any]] = []
    for component in properties[:max_objects]:
        y_min, x_min, y_max, x_max = component.bbox
        cy, cx = component.centroid
        area = int(component.area)
        entry: dict[str, Any] = {
            "id": int(component.label), "x": float(cx + region.x), "y": float(cy + region.y),
            "area_px": area, "mean_intensity": float(totals[component.label] / area),
            "integrated_intensity": float(totals[component.label]),
            "bbox": {"x": x_min + region.x, "y": y_min + region.y, "width": x_max - x_min, "height": y_max - y_min},
            "touches_roi_border": bool(x_min == 0 or y_min == 0 or x_max == labels.shape[1] or y_max == labels.shape[0]
                                       or roi_edge is not None and np.any(roi_edge[component.slice] & component.image)),
            "equivalent_diameter_px": sqrt(4 * area / np.pi),
        }
        if calibration:
            entry["area_um2"] = area * calibration.pixel_area_um2
            entry["x_um"] = entry["x"] * calibration.x_um_per_px
            entry["y_um"] = entry["y"] * calibration.y_um_per_px
        if include_contours:
            padded_mask = np.pad(component.image.astype(np.uint8), 1)
            boundaries = find_contours(padded_mask, 0.5)
            if boundaries:
                boundary = max(boundaries, key=len)
                stride = max(1, int(ceil_div(len(boundary), 512)))
                entry["contour"] = [[float(x + x_min - 1 + region.x), float(y + y_min - 1 + region.y)]
                                    for y, x in boundary[::stride]]
        objects.append(entry)
    return objects, len(properties), warnings


def ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def _labels_after_area_filter(mask: np.ndarray, min_area: int, max_area: int | None,
                              split_touching: bool, min_distance: int, exclude_border: bool) -> np.ndarray:
    from skimage.feature import peak_local_max
    from skimage.segmentation import watershed

    connected, _ = ndimage.label(mask)
    sizes = np.bincount(connected.ravel())
    keep = sizes >= min_area
    keep[0] = False
    mask = keep[connected]
    if split_touching and mask.any():
        distance = ndimage.distance_transform_edt(mask)
        coordinates = peak_local_max(distance, min_distance=min_distance, labels=mask,
                                     exclude_border=False, threshold_abs=0)
        seeds = np.zeros(mask.shape, dtype=np.int32)
        if coordinates.size:
            seeds[tuple(coordinates.T)] = np.arange(1, len(coordinates) + 1)
            labels = watershed(-distance, markers=seeds, mask=mask)
        else:
            labels, _ = ndimage.label(mask)
    else:
        labels, _ = ndimage.label(mask)
    sizes = np.bincount(labels.ravel())
    allowed = sizes >= min_area
    if max_area is not None:
        allowed &= sizes <= max_area
    allowed[0] = False
    if exclude_border and labels.size:
        border_ids = np.unique(np.concatenate((labels[0], labels[-1], labels[:, 0], labels[:, -1])))
        allowed[border_ids] = False
    # Relabel without merging touching watershed segments back together.
    new_ids = np.zeros(len(allowed), dtype=np.int32)
    new_ids[allowed] = np.arange(1, np.count_nonzero(allowed) + 1)
    return new_ids[labels]


def _exclude_roi_edge_labels(labels: np.ndarray, roi_mask: np.ndarray) -> np.ndarray:
    """Border exclusion follows the actual polygon/ellipse, not only its bbox."""
    edge = roi_mask & ~ndimage.binary_erosion(roi_mask, border_value=0)
    excluded = np.unique(labels[edge])
    retained = np.ones(int(labels.max(initial=0)) + 1, dtype=bool)
    retained[excluded] = False
    retained[0] = False
    mapping = np.zeros(len(retained), dtype=np.int32)
    mapping[retained] = np.arange(1, np.count_nonzero(retained) + 1)
    return mapping[labels]


def _deconvolve_hdab(rgb: np.ndarray, params: Mapping[str, Any]) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    """Beer–Lambert optical density + non-negative two-stain separation.

    The standard H/DAB vectors are a starting calibration, not a diagnosis or a
    claim of universal stain accuracy. Users may supply measured stain vectors.
    SciPy NNLS solves the two-column non-negative fit analytically per pixel,
    using vectorized active-set candidates rather than a Python per-pixel loop.
    """
    array = np.asarray(rgb)
    if array.ndim != 3 or array.shape[-1] not in (3, 4):
        raise ValueError("IHC 需要 RGB 彩色原图；请选择明场 RGB 图像。")
    values = np.asarray(array[..., :3], dtype=np.float64)
    if not np.isfinite(values).all() or np.min(values) < 0:
        raise ValueError("IHC RGB 图像包含无效/负值。")
    if params.get("rgb_white_level") is not None:
        white = float(params["rgb_white_level"])
    elif array.dtype.kind in "ui":
        white = float(np.iinfo(array.dtype).max)
    elif float(np.max(values)) <= 1:
        white = 1.0
    else:
        raise ValueError("浮点 RGB 原图超出 0–1，请明确 rgb_white_level。")
    if not isfinite(white) or white <= 0:
        raise ValueError("rgb_white_level 须为正的有限数值。")
    if np.max(values) > white:
        raise ValueError("RGB 数值超出所选白点，请检查位深/白点设置。")
    if array.shape[-1] == 4:
        alpha = np.asarray(array[..., 3], dtype=np.float64) / white
        values = values * alpha[..., None] + white * (1 - alpha[..., None])
    minimum_transmittance = max(1e-6, 1.0 / (white + 1.0)) if array.dtype.kind in "ui" else 1e-6
    optical_density = -np.log(np.clip(values / white, minimum_transmittance, 1.0))
    default_vectors = [[0.650, 0.704, 0.286], [0.268, 0.570, 0.776]]
    vectors = np.asarray(params.get("stain_vectors", default_vectors), dtype=np.float64)
    if vectors.shape != (2, 3) or not np.isfinite(vectors).all() or np.any(vectors < 0):
        raise ValueError("stain_vectors 必须是两个非负、有限的 RGB 光密度方向向量 (2×3)。")
    lengths = np.linalg.norm(vectors, axis=1)
    if np.any(lengths <= 0):
        raise ValueError("染色方向向量不能为零。")
    vectors = vectors / lengths[:, None]
    similarity = float(vectors[0] @ vectors[1])
    if similarity > 0.995:
        raise ValueError("两个染色方向过于接近，无法可靠分离 H 与 DAB。")
    flattened = optical_density.reshape(-1, 3)
    unconstrained = flattened @ np.linalg.pinv(vectors)
    projection_h = np.maximum(flattened @ vectors[0], 0)
    projection_d = np.maximum(flattened @ vectors[1], 0)
    valid_pair = np.all(unconstrained >= 0, axis=1)
    result = np.zeros((flattened.shape[0], 2), dtype=np.float64)
    result[valid_pair] = unconstrained[valid_pair]
    outside = ~valid_pair
    # Unit-length vectors: residual reduction from one positive component is c².
    choose_h = outside & (projection_h >= projection_d)
    choose_d = outside & ~choose_h
    result[choose_h, 0] = projection_h[choose_h]
    result[choose_d, 1] = projection_d[choose_d]
    concentrations = result.reshape(*optical_density.shape[:2], 2)
    tissue_min_od = float(params.get("tissue_min_od", 0.15))
    if not isfinite(tissue_min_od) or tissue_min_od < 0:
        raise ValueError("tissue_min_od 必须为非负数。")
    tissue_mask = np.linalg.norm(optical_density, axis=-1) > tissue_min_od
    return concentrations[..., 0], concentrations[..., 1], tissue_mask, {
        "stain_vectors": vectors.tolist(), "rgb_white_level": white,
        "separation": "beer_lambert_nonnegative_two_stain_fit", "tissue_min_od": tissue_min_od,
        "stain_vectors_source": "user_calibration" if "stain_vectors" in params else "standard_H_DAB",
    }


def analyze_microscopy(image: np.ndarray | Mapping[str, np.ndarray], params: dict[str, Any],
                       *, cancel_event: Any = None) -> dict[str, Any]:
    started = perf_counter()
    method = str(params.get("method", "nuclei"))
    if method == "ihc_advanced":
        from .histology import analyze_histology
        return analyze_histology(image, params, cancel_event=cancel_event)
    if method in {"fiber_cross_section", "myotube"}:
        from .fibers import analyze_fibers
        return analyze_fibers(image, params, cancel_event=cancel_event)
    from .microscopy_measurements import MULTICHANNEL_METHODS, analysis_region, resolve_channels
    if method in MULTICHANNEL_METHODS:
        from .microscopy_measurements import analyze_channels
        return analyze_channels(image, params)
    if method == "cellpose":
        from .cellpose_runtime import analyze_cellpose
        return analyze_cellpose(image, params, cancel_event=cancel_event)
    if method not in {"nuclei", "ihc", "intensity"}:
        raise ValueError("未知显微方法；支持 nuclei、ihc、intensity、channel_intensity、threshold、line_profile、channel_overlap、cellpose。")
    calibration = SpatialCalibration.parse(params)
    min_area = int(params.get("min_area", 20))
    max_area = int(params["max_area"]) if params.get("max_area") is not None else None
    if min_area < 1 or max_area is not None and max_area < min_area:
        raise ValueError("面积范围无效；min_area 至少为 1 px，max_area 应不小于 min_area。")
    max_objects = int(params.get("max_objects", 10000))
    if not 1 <= max_objects <= 100000:
        raise ValueError("max_objects 应在 1–100000 之间。")
    selected_channel = None
    ihc_channel_axis = None
    if isinstance(image, Mapping) or params.get("channels"):
        selected = resolve_channels(image, params, default_all=False)
        if method == "ihc":
            if len(selected) not in (3, 4) or len({str(item.values.dtype) for item in selected}) != 1:
                raise ValueError("IHC 需要同位深的原始 RGB 三通道（可附 alpha）；请选择完整 RGB 原图。")
            source = np.stack([item.values for item in selected], axis=-1)
            ihc_channel_axis = -1
        else:
            if len(selected) != 1:
                raise ValueError(f"{method} 需要明确选择一个测量通道；多个通道强度请使用 channel_intensity。")
            selected_channel = selected[0]
            source = selected_channel.values
    else:
        source = np.asarray(image)
    if method == "ihc":
        if source.ndim != 3:
            raise ValueError("IHC 需要 RGB 图像。")
        declared_axis = ihc_channel_axis if ihc_channel_axis is not None else params.get("channel_axis")
        if declared_axis is not None:
            if isinstance(declared_axis, bool) or not isinstance(declared_axis, (int, np.integer)) or not -3 <= declared_axis < 3:
                raise ValueError("IHC channel_axis 必须明确指定 RGB 颜色轴。")
            color_axis = int(declared_axis) % 3
            if source.shape[color_axis] not in (3, 4):
                raise ValueError("IHC 指定的颜色轴必须恰为原始 RGB/RGBA 3 或 4 个样本，不能把空间轴当成颜色轴。")
        else:
            candidates = [axis for axis in (0, 2) if source.shape[axis] in (3, 4)]
            if len(candidates) != 1:
                raise ValueError("IHC 图像颜色轴不明确；请指定 channel_axis，且必须为原始 RGB/RGBA 数据。")
            color_axis = candidates[0]
        source = np.moveaxis(source, color_axis, -1)
        shape = source.shape[:2]
    else:
        source = select_plane(source, 0 if selected_channel else int(params.get("channel", 0)), params.get("channel_axis"))
        shape = source.shape
    selection = analysis_region(params.get("roi"), shape)
    region = selection.bounds
    cropped = source[region.slices]
    warnings: list[str] = []
    method_details: dict[str, Any] = {"name": method, "measurement_basis": "original_pixels",
                                     "display_lut_used": False, "roi": selection.geometry,
                                     "channel": int(params.get("channel", 0)), "min_area_px": min_area}
    if selected_channel:
        method_details["source_channel"] = selected_channel.metadata()
    elif params.get("source"):
        method_details["source"] = dict(params["source"])
        method_details["channel"] = int(params["source"].get("channel", params.get("channel", 0)))
    if calibration:
        method_details["calibration"] = {"um_per_px_x": calibration.x_um_per_px, "um_per_px_y": calibration.y_um_per_px}
    else:
        warnings.append("未提供像素标定，长度和面积分别以 px 和 px² 报告。输入标定后可换算为 µm 和 µm²。")
    if method == "ihc":
        hematoxylin, signal, tissue_mask, stain_details = _deconvolve_hdab(cropped, params)
        tissue_mask &= selection.mask
        method_details.update(stain_details)
        values = signal[tissue_mask]
        if params.get("threshold") is not None:
            threshold = float(params["threshold"])
            threshold_method = "manual_DAB_optical_density"
        elif values.size > 1 and float(np.ptp(values)) > 1e-12:
            from skimage.filters import threshold_otsu
            threshold = float(threshold_otsu(values))
            threshold_method = "otsu_within_tissue"
        else:
            threshold = float(values[0]) if values.size else 0.0
            threshold_method = "no_separable_DAB_signal"
        if not isfinite(threshold) or threshold < 0:
            raise ValueError("IHC threshold 须为非负的 DAB 光密度数值。")
        positive_mask = (signal > threshold) & tissue_mask
        labels = _labels_after_area_filter(positive_mask, min_area, max_area, False, 1,
                                          bool(params.get("exclude_border", False)))
        if params.get("exclude_border", False):
            labels = _exclude_roi_edge_labels(labels, selection.mask)
        positive_area = int(np.count_nonzero(labels))
        tissue_area = int(np.count_nonzero(tissue_mask))
        objects, count, object_warnings = _region_objects(labels, signal, region, calibration,
                                                         params.get("include_contours", True), max_objects, selection.mask)
        warnings.extend(object_warnings)
        summary = {"tissue_area_px": tissue_area, "positive_area_px": positive_area,
                   "positive_fraction": positive_area / tissue_area if tissue_area else None,
                   "mean_dab_od_in_tissue": float(np.mean(values)) if values.size else None,
                   "integrated_dab_od": float(np.sum(values)) if values.size else 0.0,
                   "mean_hematoxylin_od_in_tissue": float(np.mean(hematoxylin[tissue_mask])) if tissue_area else None,
                   "object_type": "connected_DAB_positive_region", "threshold": threshold}
        if calibration:
            summary.update({"positive_area_um2": positive_area * calibration.pixel_area_um2,
                            "tissue_area_um2": tissue_area * calibration.pixel_area_um2})
        method_details.update({"threshold": threshold, "threshold_method": threshold_method})
        warnings.append("IHC 计数是连通阳性区域数，不等于阳性细胞数；阈值需用阴阳性对照校准，未计算细胞 H-score。")
        if not tissue_area:
            warnings.append("ROI 未检测到达到光密度阈值的组织。")
    else:
        raw = np.asarray(cropped, dtype=np.float64)
        finite = np.isfinite(raw) & selection.mask
        if not finite.any():
            raise ValueError("ROI 没有有效的原始像素。")
        # Polygon/ellipse bounds also contain pixels outside the selected ROI.
        # The summary must use the same original finite pixels as segmentation.
        summary = _summary(raw[finite])
        limits = source_range(source, params)
        summary["source_dtype"] = str(source.dtype)
        summary["source_bit_depth"] = int(source.dtype.itemsize * 8)
        if limits[1] is not None:
            saturated = np.count_nonzero(raw[finite] >= limits[1])
            summary["saturation_fraction"] = float(saturated / np.count_nonzero(finite))
            if saturated:
                warnings.append("ROI 中存在达到探测器上限的像素；显示色阶不改变原始削顶。")
        else:
            summary["saturation_fraction"] = None
        if np.count_nonzero(finite) != selection.area_px:
            warnings.append("无效源像素已从强度统计和分割中排除。")
        if method == "intensity":
            objects, count = [], 0
            if params.get("threshold") is not None:
                threshold = float(params["threshold"])
                if not isfinite(threshold):
                    raise ValueError("threshold 必须是有限数值。")
                positive = (raw > threshold) & finite
                summary.update({"positive_area_px": int(np.count_nonzero(positive)),
                                "positive_fraction": float(np.count_nonzero(positive) / np.count_nonzero(finite))})
                method_details["threshold"] = threshold
            if calibration:
                summary["area_um2"] = float(np.count_nonzero(finite) * calibration.pixel_area_um2)
        else:
            from skimage.filters import threshold_otsu

            sigma = float(params.get("sigma", 1.0))
            if not isfinite(sigma) or not 0 <= sigma <= 20:
                raise ValueError("sigma 须为 0–20 原图像素。")
            # Smoothing affects the segmentation mask only; measurements stay raw.
            finite_median = float(np.median(raw[finite]))
            prepared = np.where(finite, raw, finite_median)
            smoothed = ndimage.gaussian_filter(prepared, sigma=sigma) if sigma else prepared
            polarity = str(params.get("polarity", "bright"))
            if polarity not in {"bright", "dark"}:
                raise ValueError("nuclei polarity 应为 bright 或 dark。")
            selected = smoothed[finite]
            if params.get("threshold") is not None:
                threshold = float(params["threshold"])
                threshold_method = "manual_source_units"
            elif float(np.ptp(selected)) <= max(1.0, abs(float(np.mean(selected)))) * 1e-12:
                threshold = float(selected[0])
                threshold_method = "constant_image_no_objects"
            else:
                threshold = float(threshold_otsu(selected))
                threshold_method = "otsu_source_units"
            if not isfinite(threshold):
                raise ValueError("threshold 必须为有限数值，单位为原始像素强度。")
            mask = ((smoothed < threshold) if polarity == "dark" else (smoothed > threshold)) & finite
            if params.get("fill_holes", True):
                mask = ndimage.binary_fill_holes(mask) & finite
            min_distance = int(params.get("min_distance", max(2, round(sqrt(min_area / np.pi)))))
            if min_distance < 1:
                raise ValueError("min_distance 须至少为 1 px。")
            labels = _labels_after_area_filter(mask, min_area, max_area, bool(params.get("split_touching", True)),
                                              min_distance, bool(params.get("exclude_border", False)))
            if params.get("exclude_border", False):
                labels = _exclude_roi_edge_labels(labels, selection.mask)
            objects, count, object_warnings = _region_objects(labels, raw, region, calibration,
                                                             params.get("include_contours", True), max_objects, selection.mask)
            warnings.extend(object_warnings)
            mask_area = int(np.count_nonzero(labels))
            summary.update({"foreground_area_px": mask_area, "foreground_fraction": mask_area / np.count_nonzero(finite),
                            "threshold": threshold, "object_type": "classical_segmented_nucleus_candidate"})
            if calibration:
                summary["foreground_area_um2"] = mask_area * calibration.pixel_area_um2
            method_details.update({"segmentation": "threshold_connected_components_distance_watershed",
                                   "sigma_px": sigma, "threshold": threshold, "threshold_method": threshold_method,
                                   "min_distance_px": min_distance, "polarity": polarity,
                                   "split_touching": bool(params.get("split_touching", True))})
            warnings.append("核计数使用传统阈值/分水岭候选分割，请叠加原图复核；未声称经验证的模型识别准确率。")
    result = {"kind": "microscopy", "count": count, "objects": objects, "summary": summary,
              "warnings": warnings, "method": method_details, "elapsed_ms": (perf_counter() - started) * 1000}
    if method == "nuclei":
        from .instances import attach_instance_mask
        attach_instance_mask(result, labels, origin=(region.x, region.y), source_shape=shape)
    return result
