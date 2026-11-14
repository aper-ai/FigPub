"""Original-pixel microscopy measurements with explicit channels and geometry.

This is a new implementation. Display LUTs, colors and visibility never enter
these calculations. Channel identities refer to the original asset/series/frame
and can retain different source dtypes without stacking or resampling them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import combinations
from math import ceil, floor, isfinite, hypot
from time import perf_counter
from typing import Any
import warnings as warning_capture

import numpy as np
from scipy import ndimage

from .analysis import PixelRegion, select_plane, source_range


MAX_CHANNELS = 32
MAX_PROFILE_SAMPLES = 10_000
MAX_PROFILE_WORK = 2_000_000
THRESHOLD_METHODS = frozenset({"manual", "otsu", "triangle", "li"})
MULTICHANNEL_METHODS = frozenset({"channel_intensity", "threshold", "line_profile", "channel_overlap"})


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} 必须为整数。")
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label} 必须为整数。") from exc
    if not isfinite(number) or number != int(number) or not minimum <= number <= maximum:
        raise ValueError(f"{label} 必须为 {minimum}–{maximum} 的整数。")
    return int(number)


def _finite_number(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label} 必须为有限数值。") from exc
    if not isfinite(result):
        raise ValueError(f"{label} 必须为有限数值。")
    return result


@dataclass(frozen=True, slots=True)
class ChannelPlane:
    channel_id: str
    name: str
    source: dict[str, Any]
    values: np.ndarray
    options: dict[str, Any]

    def metadata(self) -> dict[str, Any]:
        return {"id": self.channel_id, "name": self.name, "source": dict(self.source),
                "dtype": str(self.values.dtype), "bit_depth": self.values.dtype.itemsize * 8,
                "width_px": self.values.shape[1], "height_px": self.values.shape[0]}


def resolve_channels(image: np.ndarray | Mapping[str, np.ndarray], params: Mapping[str, Any],
                     *, default_all: bool = True) -> list[ChannelPlane]:
    """Resolve native planes without casting a whole image or mixing dtypes.

    A map contains already-selected YX planes supplied by storage.read_plane.
    ``source.channel`` remains provenance and is not indexed into that YX plane.
    Legacy YXC/CYX arrays still support an explicit channel axis.
    """
    descriptors = params.get("channels")
    is_map = isinstance(image, Mapping)
    if descriptors is None:
        if is_map:
            if len(image) != 1:
                raise ValueError("多个原始平面必须同时提供 channels 与完整 source 身份。")
            key = next(iter(image))
            descriptors = [{"id": "c0", "array_key": key, "source": params.get("source", {"asset_id": key})}]
        else:
            array = np.asarray(image)
            if array.ndim == 2 or not default_all:
                indices = [params.get("channel", 0)]
            elif array.ndim == 3:
                axis = params.get("channel_axis")
                if axis is None:
                    axis = -1 if array.shape[-1] <= 4 else 0 if array.shape[0] <= 4 else None
                if axis not in (0, 2, -1):
                    raise ValueError("多通道图像须明确 channel_axis=0 或 -1。")
                indices = list(range(array.shape[axis]))
            else:
                raise ValueError("请先选择时间点/Z 层，提供 YX 或带通道的三维原始数组。")
            descriptors = [{"id": f"c{index}", "name": f"Channel {int(index) + 1}",
                            "channel": index, "source": params.get("source", {})} for index in indices]
    if not isinstance(descriptors, list) or not 1 <= len(descriptors) <= MAX_CHANNELS:
        raise ValueError(f"channels 必须包含 1–{MAX_CHANNELS} 个显式通道。")
    channels: list[ChannelPlane] = []
    seen: set[str] = set()
    for ordinal, item in enumerate(descriptors):
        if not isinstance(item, Mapping):
            raise ValueError("每个 channel 必须包含稳定 id 和原始 source。")
        identifier = str(item.get("id", f"c{ordinal}"))
        if not identifier or len(identifier) > 100 or identifier in seen:
            raise ValueError("通道 id 必须非空、唯一且不超过 100 字符。")
        seen.add(identifier)
        nested_source = item.get("source", {})
        if not isinstance(nested_source, Mapping):
            raise ValueError("通道 source 必须是对象。")
        source = dict(nested_source)
        for key in ("asset_id", "series", "frame", "channel"):
            if key not in source and key in item:
                source[key] = item[key]
        for key in ("series", "frame", "channel"):
            source[key] = _integer(source.get(key, 0), key, 0, 1_000_000)
        if source.get("asset_id") is not None:
            source["asset_id"] = str(source["asset_id"])
        if is_map:
            candidates = [item.get("array_key"), identifier, source.get("asset_id")]
            array_key = next((candidate for candidate in candidates if candidate is not None and candidate in image), None)
            if array_key is None:
                raise ValueError(f"通道 {identifier} 的原始平面未提供。")
            array = np.asarray(image[array_key])
        else:
            array = np.asarray(image)
        index = item.get("channel", source["channel"])
        index = _integer(index, "channel", 0, 4095)
        # A map's two-dimensional array has already selected source.channel.
        index_to_select = 0 if is_map and array.ndim == 2 else index
        plane = select_plane(array, index_to_select, item.get("channel_axis", params.get("channel_axis")))
        if not plane.size:
            raise ValueError(f"通道 {identifier} 的原始平面为空。")
        if channels and plane.shape != channels[0].values.shape:
            raise ValueError("所选通道尺寸不同；请先明确配准，分析不会静默缩放或对齐。")
        if not is_map and array.ndim != 2:
            source["channel"] = index
        channels.append(ChannelPlane(identifier, str(item.get("name", f"Channel {ordinal + 1}")),
                                     source, plane, dict(item)))
    return channels


@dataclass(frozen=True, slots=True)
class AnalysisRegion:
    bounds: PixelRegion
    mask: np.ndarray
    geometry: dict[str, Any]

    @property
    def area_px(self) -> int:
        return int(np.count_nonzero(self.mask))


def analysis_region(geometry: Mapping[str, Any] | None, shape: tuple[int, int]) -> AnalysisRegion:
    """Rasterize source-pixel ROI; ellipse/polygon membership uses pixel centers."""
    if geometry is None:
        geometry = {"type": "rectangle", "x": 0, "y": 0, "width": shape[1], "height": shape[0]}
    if not isinstance(geometry, Mapping):
        raise ValueError("ROI 必须是原始像素几何对象。")
    kind = str(geometry.get("type", "rectangle"))
    if kind == "freehand":
        kind = "polygon"
    if kind not in {"rectangle", "ellipse", "polygon"}:
        raise ValueError("ROI 类型仅支持 rectangle、ellipse、polygon。")
    if kind == "polygon":
        points = np.asarray(geometry.get("points", []), dtype=np.float64)
        if points.ndim != 2 or points.shape[1:] != (2,) or not 3 <= len(points) <= 10000 or not np.isfinite(points).all():
            raise ValueError("多边形需要 3–10000 个有限原图坐标点。")
        if np.any(points < 0) or np.any(points[:, 0] > shape[1]) or np.any(points[:, 1] > shape[0]):
            raise ValueError("多边形 ROI 超出原图边界。")
        x0, y0 = np.min(points, axis=0)
        x1, y1 = np.max(points, axis=0)
        bounds = PixelRegion.parse({"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}, shape)
        from skimage.draw import polygon2mask
        vertices = points[:, ::-1] - np.array([bounds.y + .5, bounds.x + .5])
        mask = polygon2mask((bounds.height, bounds.width), vertices)
        saved = {"type": "polygon", "points": points.tolist(), **bounds.to_dict()}
    else:
        bounds = PixelRegion.parse(geometry, shape)
        if kind == "rectangle":
            mask = np.ones((bounds.height, bounds.width), dtype=bool)
        else:
            x = _finite_number(geometry.get("x", 0), "ROI x")
            y = _finite_number(geometry.get("y", 0), "ROI y")
            width = _finite_number(geometry.get("width", geometry.get("w", bounds.width)), "ROI width")
            height = _finite_number(geometry.get("height", geometry.get("h", bounds.height)), "ROI height")
            yy, xx = np.ogrid[bounds.y:bounds.y + bounds.height, bounds.x:bounds.x + bounds.width]
            mask = ((xx + .5 - x - width / 2) / (width / 2)) ** 2 + ((yy + .5 - y - height / 2) / (height / 2)) ** 2 <= 1
        saved = ({"type": kind, **bounds.to_dict()} if kind == "rectangle" else
                 {"type": "ellipse", "x": x, "y": y, "width": width, "height": height,
                  "bounding_box_px": bounds.to_dict()})
    if not np.any(mask):
        raise ValueError("ROI 没有覆盖任何原始像素中心。")
    mask.setflags(write=False)
    return AnalysisRegion(bounds, mask, saved)


def _calibration(params: Mapping[str, Any]):
    from .microscopy import SpatialCalibration
    return SpatialCalibration.parse(params)


def _channel_values(channel: ChannelPlane, region: AnalysisRegion) -> tuple[np.ndarray, np.ndarray]:
    cropped = np.asarray(channel.values[region.bounds.slices], dtype=np.float64)
    finite = region.mask & np.isfinite(cropped)
    return cropped, finite


def _stats(values: np.ndarray) -> dict[str, Any]:
    from .microscopy import _summary
    summary = _summary(values)
    return {"pixel_count": summary["pixel_count"], "raw_mean": summary["mean_intensity"],
            "raw_integrated": summary["integrated_intensity"], "raw_min": summary["min_intensity"],
            "raw_max": summary["max_intensity"], "raw_sd": summary["sd_intensity"]}


def _background(channel: ChannelPlane, params: Mapping[str, Any]) -> tuple[float | None, dict[str, Any], list[str]]:
    options = channel.options.get("background", {})
    if isinstance(options, (float, int)) and not isinstance(options, bool):
        options = {"method": "fixed", "value": options}
    if not isinstance(options, Mapping):
        raise ValueError("通道 background 必须是方法对象或明确的固定背景值。")
    method = str(options.get("method", params.get("background_method", "none")))
    warnings: list[str] = []
    if method == "none":
        return None, {"method": "none"}, warnings
    if method == "fixed":
        values = params.get("background_values", {})
        if not isinstance(values, Mapping):
            raise ValueError("background_values 必须按通道 id 提供原始尺度的背景值。")
        value = options.get("value", values.get(channel.channel_id))
        if value is None:
            raise ValueError(f"通道 {channel.channel_id} 缺少固定背景值。")
        number = _finite_number(value, "固定背景")
        return number, {"method": "fixed", "value": number, "units": "source_intensity"}, warnings
    if method == "roi":
        geometry = options.get("roi", params.get("background_roi"))
        if geometry is None:
            raise ValueError("背景方法为 roi 时必须指定独立 background_roi。")
        region = analysis_region(geometry, channel.values.shape)
        values, valid = _channel_values(channel, region)
        if not np.any(valid):
            raise ValueError(f"通道 {channel.channel_id} 的背景 ROI 没有有限像素。")
        mean = float(np.mean(values[valid]))
        if not isfinite(mean):
            raise ValueError("背景统计超出 float64 范围。")
        if np.count_nonzero(valid) != region.area_px:
            warnings.append(f"{channel.name} 背景 ROI 的非有限像素已排除。")
        return mean, {"method": "roi", "roi": region.geometry, "pixel_count": int(np.count_nonzero(valid)),
                      "value": mean, "units": "source_intensity"}, warnings
    raise ValueError("background_method 仅支持 none、roi、fixed。")


def resolve_threshold(channel: ChannelPlane, values: np.ndarray, params: Mapping[str, Any]) -> tuple[float | None, dict[str, Any], list[str]]:
    overrides = params.get("thresholds", {})
    if not isinstance(overrides, Mapping):
        raise ValueError("thresholds 必须按通道 id 提供阈值方法。")
    selected = channel.options.get("threshold", overrides.get(channel.channel_id, {}))
    if isinstance(selected, (int, float)) and not isinstance(selected, bool):
        selected = {"method": "manual", "value": selected}
    if not isinstance(selected, Mapping):
        raise ValueError("每通道 threshold 必须是 {method,value} 或数值。")
    method = str(selected.get("method", params.get("threshold_method", "otsu"))).lower()
    if method not in THRESHOLD_METHODS:
        raise ValueError("threshold_method 仅支持 manual、otsu、triangle、li。")
    details: dict[str, Any] = {"method": method, "basis": "original_roi_pixels", "comparison": ">", "units": "source_intensity"}
    warnings: list[str] = []
    finite_values = np.asarray(values, dtype=np.float64)
    finite_values = finite_values[np.isfinite(finite_values)]
    if method == "manual":
        value = selected.get("value", params.get("threshold"))
        if value is None:
            raise ValueError(f"通道 {channel.channel_id} 的 manual 阈值未填写。")
        threshold = _finite_number(value, "manual threshold")
    elif finite_values.size == 0:
        details.update(value=None, status="no_finite_roi_pixels")
        return None, details, [f"{channel.name} 没有有限 ROI 像素，未计算自动阈值。"]
    elif float(np.ptp(finite_values)) == 0:
        threshold = float(finite_values[0])
        details["status"] = "constant_image_no_separation"
        warnings.append(f"{channel.name} ROI 为恒定强度，自动阈值不能分离信号，阳性面积为 0。")
    else:
        from skimage.filters import threshold_li, threshold_otsu, threshold_triangle
        bins = _integer(params.get("histogram_bins", 4096), "histogram_bins", 32, 65536)
        with warning_capture.catch_warnings(record=True) as emitted:
            warning_capture.simplefilter("always", RuntimeWarning)
            if method == "otsu":
                threshold = float(threshold_otsu(finite_values, nbins=bins))
            elif method == "triangle":
                threshold = float(threshold_triangle(finite_values, nbins=bins))
            else:
                threshold = float(threshold_li(finite_values))
        if not isfinite(threshold):
            details.update(value=None, status="automatic_threshold_undefined")
            return None, details, [f"{channel.name} 的 {method} 自动阈值无定义，请复核并指定手工阈值。"]
        if emitted:
            warnings.append(f"{channel.name} 的 {method} 阈值求解出现数值警告，请结合原图复核。")
        if method in {"otsu", "triangle"}:
            details["histogram_bins"] = bins
    details.update(value=threshold)
    details.setdefault("status", "ok")
    return threshold, details, warnings


def _channel_row(channel: ChannelPlane, region: AnalysisRegion, values: np.ndarray, valid: np.ndarray,
                 params: Mapping[str, Any], calibration) -> tuple[dict[str, Any], list[str]]:
    summary = _stats(values[valid])
    row = {"channel_id": channel.channel_id, "channel_name": channel.name, "source": dict(channel.source),
           "source_dtype": str(channel.values.dtype), "source_bit_depth": channel.values.dtype.itemsize * 8,
           "area_px": region.area_px, "invalid_pixels": region.area_px - int(np.count_nonzero(valid)), **summary}
    warnings: list[str] = []
    if row["invalid_pixels"]:
        warnings.append(f"{channel.name} 的 {row['invalid_pixels']} 个非有限 ROI 像素未参与测量。")
    if not row["pixel_count"]:
        warnings.append(f"{channel.name} ROI 没有有限像素，强度统计为空。")
    bounds = source_range(channel.values, {**params, **channel.options})
    row["saturation_fraction"] = (float(np.count_nonzero(values[valid] >= bounds[1]) / row["pixel_count"])
                                  if bounds[1] is not None and row["pixel_count"] else None)
    if row["saturation_fraction"]:
        warnings.append(f"{channel.name} ROI 存在达到探测器上限的原始像素。")
    if calibration:
        row["area_um2"] = region.area_px * calibration.pixel_area_um2
        row["valid_area_um2"] = row["pixel_count"] * calibration.pixel_area_um2
    return row, warnings


def _positive_objects(mask: np.ndarray, values: np.ndarray, channel: ChannelPlane,
                      region: AnalysisRegion, calibration, params: Mapping[str, Any]):
    from .microscopy import _region_objects
    labels, _ = ndimage.label(mask)
    maximum = _integer(params.get("max_objects", 1000), "max_objects", 1, 100000)
    objects, count, warnings = _region_objects(labels, values, region.bounds, calibration,
                                              bool(params.get("include_contours", True)), maximum, region.mask)
    for item in objects:
        item.update(channel_id=channel.channel_id, channel_name=channel.name,
                    object_type="threshold_connected_region")
    return objects, count, warnings


def _profile(channels: list[ChannelPlane], params: Mapping[str, Any], calibration) -> dict[str, Any]:
    line = params.get("line")
    if not isinstance(line, Mapping) or any(key not in line for key in ("x1", "y1", "x2", "y2")):
        raise ValueError("线剖面需要原图坐标 line={x1,y1,x2,y2}。")
    x1, y1, x2, y2 = [_finite_number(line[key], key) for key in ("x1", "y1", "x2", "y2")]
    height, width = channels[0].values.shape
    if not (0 <= x1 <= width - 1 and 0 <= x2 <= width - 1 and 0 <= y1 <= height - 1 and 0 <= y2 <= height - 1):
        raise ValueError("线端点必须位于原图像素中心范围内（0 到尺寸−1）。")
    length = hypot(x2 - x1, y2 - y1)
    if length == 0:
        raise ValueError("线剖面两端点不能重合。")
    samples = _integer(params.get("samples", 256), "samples", 2, MAX_PROFILE_SAMPLES)
    line_width = _integer(params.get("line_width_px", 1), "line_width_px", 1, 101)
    if samples * line_width * len(channels) > MAX_PROFILE_WORK:
        raise ValueError("线剖面采样工作量过大，请减少采样点、线宽或通道数。")
    offsets = np.arange(line_width) - (line_width - 1) / 2
    xs = np.linspace(x1, x2, samples)[None, :] - offsets[:, None] * (y2 - y1) / length
    ys = np.linspace(y1, y2, samples)[None, :] + offsets[:, None] * (x2 - x1) / length
    left = max(0, floor(float(xs.min())) - 1)
    top = max(0, floor(float(ys.min())) - 1)
    right = min(width, ceil(float(xs.max())) + 2)
    bottom = min(height, ceil(float(ys.max())) + 2)
    distances = np.linspace(0, length, samples).tolist()
    physical = (np.linspace(0, hypot((x2 - x1) * calibration.x_um_per_px,
                                    (y2 - y1) * calibration.y_um_per_px), samples).tolist() if calibration else None)
    profiles, rows, warnings = [], [], []
    for channel in channels:
        crop = np.asarray(channel.values[top:bottom, left:right], dtype=np.float64)
        sampled = ndimage.map_coordinates(crop, [ys - top, xs - left], order=1, mode="constant", cval=np.nan, prefilter=False)
        finite = np.isfinite(sampled)
        counts = np.count_nonzero(finite, axis=0)
        totals = np.sum(np.where(finite, sampled, 0), axis=0, dtype=np.float64)
        if np.any(~np.isfinite(totals)):
            raise ValueError("线剖面统计超出 float64 范围。")
        mean = np.divide(totals, counts, out=np.zeros_like(totals), where=counts > 0)
        values = [float(value) if count else None for value, count in zip(mean, counts)]
        profiles.append({"channel_id": channel.channel_id, "channel_name": channel.name,
                         "source": dict(channel.source), "distance_px": distances, "distance_um": physical,
                         "values": values, "valid_width_samples": counts.tolist()})
        for index, value in enumerate(values):
            rows.append({"channel_id": channel.channel_id, "channel_name": channel.name, "sample_index": index,
                         "distance_px": distances[index], "distance_um": physical[index] if physical else None,
                         "intensity": value, "valid_width_samples": int(counts[index])})
        if np.any(counts < line_width):
            warnings.append(f"{channel.name} 的部分线宽采样超出原图或遇到无效像素；只平均有效采样，无有效值时为空。")
    return {"profiles": profiles, "rows": rows, "pairs": [], "objects": [], "count": 0,
            "summary": {"channel_count": len(channels), "samples_per_channel": samples,
                        "line_length_px": length, "line_length_um": physical[-1] if physical else None},
            "warnings": warnings, "method": {"name": "line_profile", "line": dict(zip(("x1", "y1", "x2", "y2"), (x1, y1, x2, y2))),
                                               "samples": samples, "line_width_px": line_width,
                                               "interpolation": "bilinear_source_intensity", "width_aggregation": "mean_of_finite_samples",
                                               "roi_used": False, "background_used": False}}


def _ratio(numerator: float | int, denominator: float | int) -> float | None:
    return float(numerator / denominator) if denominator else None


def _pair_metrics(first: ChannelPlane, second: ChannelPlane, region: AnalysisRegion,
                  first_threshold: float | None, second_threshold: float | None,
                  calibration) -> tuple[dict[str, Any], list[str]]:
    a, finite_a = _channel_values(first, region)
    b, finite_b = _channel_values(second, region)
    joint = finite_a & finite_b
    a, b = a[joint], b[joint]
    count = int(a.size)
    row: dict[str, Any] = {"channel_a_id": first.channel_id, "channel_a_name": first.name,
                           "channel_b_id": second.channel_id, "channel_b_name": second.name,
                           "valid_pixels": count, "roi_area_px": region.area_px,
                           "threshold_a": first_threshold, "threshold_b": second_threshold,
                           "pearson_r": None, "positive_a_px": None, "positive_b_px": None,
                           "intersection_px": None, "union_px": None, "jaccard": None, "dice": None,
                           "overlap_fraction_a": None, "overlap_fraction_b": None,
                           "manders_m1": None, "manders_m2": None, "manders_overlap_coefficient": None}
    notes: list[str] = []
    if not count:
        notes.append("no_joint_finite_pixels")
    else:
        # Scale before dot products to avoid overflow while preserving ratios.
        scaled_a = a / max(float(np.max(np.abs(a))), 1.0)
        scaled_b = b / max(float(np.max(np.abs(b))), 1.0)
        centered_a, centered_b = scaled_a - np.mean(scaled_a), scaled_b - np.mean(scaled_b)
        norm_a, norm_b = float(np.linalg.norm(centered_a)), float(np.linalg.norm(centered_b))
        if count >= 2 and norm_a > 0 and norm_b > 0:
            row["pearson_r"] = float(np.clip(np.dot(centered_a / norm_a, centered_b / norm_b), -1, 1))
        else:
            notes.append("pearson_undefined_constant_or_insufficient_pixels")
        if first_threshold is not None and second_threshold is not None:
            positive_a, positive_b = a > first_threshold, b > second_threshold
            area_a, area_b = int(np.count_nonzero(positive_a)), int(np.count_nonzero(positive_b))
            intersection = int(np.count_nonzero(positive_a & positive_b))
            union = area_a + area_b - intersection
            row.update(positive_a_px=area_a, positive_b_px=area_b, intersection_px=intersection, union_px=union,
                       jaccard=_ratio(intersection, union), dice=_ratio(2 * intersection, area_a + area_b),
                       overlap_fraction_a=_ratio(intersection, area_a), overlap_fraction_b=_ratio(intersection, area_b))
            if np.min(a) >= 0 and np.min(b) >= 0:
                row["manders_m1"] = _ratio(float(np.sum(scaled_a[positive_b])), float(np.sum(scaled_a)))
                row["manders_m2"] = _ratio(float(np.sum(scaled_b[positive_a])), float(np.sum(scaled_b)))
            else:
                notes.append("manders_undefined_for_negative_source_intensity")
            if not union:
                notes.append("both_threshold_masks_empty")
            if calibration:
                row["intersection_um2"] = intersection * calibration.pixel_area_um2
        else:
            notes.append("threshold_unavailable")
        if np.min(a) >= 0 and np.min(b) >= 0:
            norm_a, norm_b = float(np.linalg.norm(scaled_a)), float(np.linalg.norm(scaled_b))
            if norm_a and norm_b:
                row["manders_overlap_coefficient"] = float(np.clip(np.dot(scaled_a / norm_a, scaled_b / norm_b), 0, 1))
    row["status"] = "ok" if not notes else "review"
    row["notes"] = notes
    return row, ([f"{first.name} / {second.name}: {', '.join(notes)}"] if notes else [])


def analyze_channels(image: np.ndarray | Mapping[str, np.ndarray], params: dict[str, Any]) -> dict[str, Any]:
    started = perf_counter()
    method = str(params.get("method", "channel_intensity"))
    if method not in MULTICHANNEL_METHODS:
        raise ValueError("未知多通道测量方法。")
    channels = resolve_channels(image, params)
    calibration = _calibration(params)
    warnings: list[str] = []
    if not calibration:
        warnings.append("未提供像素标定，长度/面积仅报告 px / px²。")
    common_method = {"measurement_basis": "original_pixels", "display_lut_used": False,
                     "channels": [channel.metadata() for channel in channels],
                     "precision": "float64_without_intermediate_rounding"}
    if calibration:
        common_method["calibration"] = {"um_per_px_x": calibration.x_um_per_px, "um_per_px_y": calibration.y_um_per_px}
    if method == "line_profile":
        result = _profile(channels, params, calibration)
        if params.get("roi") is not None:
            warnings.append("线剖面按显式 line 采样，ROI 不参与线剖面。")
        if params.get("background_method", "none") != "none":
            warnings.append("线剖面报告原始强度，背景扣除设置未应用。")
        result["method"].update(common_method)
        result["warnings"] = list(dict.fromkeys(warnings + result["warnings"]))
        return {"kind": "microscopy", "channels": common_method["channels"], **result,
                "elapsed_ms": (perf_counter() - started) * 1000}
    region = analysis_region(params.get("roi"), channels[0].values.shape)
    rows, objects, pairs = [], [], []
    thresholds, threshold_details, background_details = {}, {}, {}
    total_objects = 0
    if method == "channel_overlap" and len(channels) < 2:
        raise ValueError("通道重叠至少需要两个明确选择的原始通道。")
    for channel in channels:
        values, valid = _channel_values(channel, region)
        row, notes = _channel_row(channel, region, values, valid, params, calibration)
        warnings.extend(notes)
        if method == "channel_intensity":
            background, detail, notes = _background(channel, params)
            warnings.extend(notes)
            background_details[channel.channel_id] = detail
            correction = 0.0 if background is None else background
            row.update(background_mean=background,
                       corrected_mean=row["raw_mean"] - correction if row["raw_mean"] is not None else None,
                       corrected_integrated=(row["raw_integrated"] - correction * row["pixel_count"]
                                             if row["raw_integrated"] is not None else None),
                       correction_applied=background is not None)
            if any(value is not None and not isfinite(value) for value in (row["corrected_mean"], row["corrected_integrated"])):
                raise ValueError("背景扣除超出 float64 范围。")
            if row["corrected_mean"] is not None and row["corrected_mean"] < 0:
                warnings.append(f"{channel.name} 扣背景结果为负，已保留有符号结果，未截断为 0。")
        else:
            threshold, detail, notes = resolve_threshold(channel, values[valid], params)
            warnings.extend(notes)
            thresholds[channel.channel_id], threshold_details[channel.channel_id] = threshold, detail
            positive = (values > threshold) & valid if threshold is not None else np.zeros_like(valid)
            area = int(np.count_nonzero(positive)) if threshold is not None else None
            row.update(threshold=threshold, threshold_method=detail["method"], positive_area_px=area,
                       positive_fraction=_ratio(area, row["pixel_count"]) if area is not None else None,
                       positive_integrated=float(np.sum(values[positive])) if area else (0.0 if area == 0 else None))
            if calibration and area is not None:
                row["positive_area_um2"] = area * calibration.pixel_area_um2
            if row["positive_integrated"] is not None and not isfinite(row["positive_integrated"]):
                raise ValueError("阈值阳性强度积分超出 float64 范围。")
            if method == "threshold":
                found, count, notes = _positive_objects(positive, values, channel, region, calibration, params)
                objects.extend(found)
                total_objects += count
                warnings.extend(notes)
        rows.append(row)
    if method == "channel_overlap":
        for first, second in combinations(channels, 2):
            pair, notes = _pair_metrics(first, second, region, thresholds[first.channel_id], thresholds[second.channel_id], calibration)
            pairs.append(pair)
            warnings.extend(notes)
    if method in {"threshold", "channel_overlap"} and (params.get("background_method", "none") != "none"
                                                        or any(channel.options.get("background") for channel in channels)):
        warnings.append("阈值/重叠使用原始像素；背景扣除设置仅用于 ROI 强度测量，未用于本次阈值或相关指标。")
    summary = {"channel_count": len(channels), "roi_area_px": region.area_px,
               "pair_count": len(pairs), "object_type": "threshold_connected_region" if method == "threshold" else None}
    if calibration:
        summary["roi_area_um2"] = region.area_px * calibration.pixel_area_um2
    details = {"name": method, **common_method, "roi": region.geometry,
               "background": background_details, "thresholds": threshold_details}
    if method == "channel_intensity":
        details["background_formula"] = "corrected_integrated = raw_integrated - finite_pixel_count * background_mean; signed result retained"
    if method == "channel_overlap":
        details["metrics"] = {
            "pixel_domain": "pairwise_joint_finite_pixels_within_roi",
            "pearson_r": "centered Pearson correlation of original intensities; null for zero variance or fewer than 2 pixels",
            "jaccard": "intersection / union of threshold-positive masks; null when both masks empty",
            "dice": "2 * intersection / (positive_a + positive_b); null when both masks empty",
            "overlap_fraction_a": "intersection / positive_a; null when a mask empty",
            "overlap_fraction_b": "intersection / positive_b; null when b mask empty",
            "manders_m1": "sum(original A where original B > threshold B) / sum(original A)",
            "manders_m2": "sum(original B where original A > threshold A) / sum(original B)",
            "manders_overlap_coefficient": "dot(A,B) / (L2(A)*L2(B)) without centering",
            "manders_domain": "nonnegative original intensities; null for negative intensities or zero denominator",
            "interpretation": "descriptive pixel association, not evidence of molecular interaction; no pixel-level significance test",
        }
    return {"kind": "microscopy", "count": total_objects, "objects": objects, "rows": rows,
            "pairs": pairs, "profiles": [], "channels": common_method["channels"], "summary": summary,
            "warnings": list(dict.fromkeys(warnings)), "method": details,
            "elapsed_ms": (perf_counter() - started) * 1000}
