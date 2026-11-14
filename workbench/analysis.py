"""Native-pixel, full-precision Western blot measurements.

New Figure Workbench implementation, licensed AGPL-3.0-only. GelFigure and
ScieFig informed the workflow, but this module does not import either app.

The three independent coordinate/value domains are deliberate:
* regions are in the original image pixel coordinates;
* numerical measurements use source values, never a display LUT;
* figure layout/export has no place in this module.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import ceil, floor, isfinite, sqrt
from time import perf_counter
from typing import Any, Mapping

import numpy as np


@dataclass(frozen=True, slots=True)
class PixelRegion:
    x: int
    y: int
    width: int
    height: int

    @classmethod
    def parse(cls, values: Mapping[str, Any], shape: tuple[int, int]) -> "PixelRegion":
        try:
            x = float(values.get("x", 0))
            y = float(values.get("y", 0))
            width = float(values.get("width", values.get("w", shape[1])))
            height = float(values.get("height", values.get("h", shape[0])))
        except (TypeError, ValueError) as exc:
            raise ValueError("ROI 坐标必须是有限数值。") from exc
        if not all(isfinite(v) for v in (x, y, width, height)):
            raise ValueError("ROI 坐标包含 NaN 或无穷值。")
        if x < 0 or y < 0 or width <= 0 or height <= 0:
            raise ValueError("ROI 必须有正的宽高，且位于原图内。")
        # Include every touched source pixel; record this exact rectangle.
        x0, y0, x1, y1 = floor(x), floor(y), ceil(x + width), ceil(y + height)
        if x1 > shape[1] or y1 > shape[0]:
            raise ValueError(f"ROI 超出原图边界 ({shape[1]} × {shape[0]} px)。")
        return cls(x0, y0, x1 - x0, y1 - y0)

    @property
    def slices(self) -> tuple[slice, slice]:
        return slice(self.y, self.y + self.height), slice(self.x, self.x + self.width)

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True, slots=True)
class BlotRegion:
    roi_id: str
    asset_id: str
    source_identity: tuple[str, int, int, int]
    region: PixelRegion
    role: str
    protein: str
    sample_id: str
    group: str
    biological_replicate: str
    technical_replicate: str
    exposure_id: str
    experiment_id: str
    control_roi_id: str
    included: bool

    @classmethod
    def parse(cls, data: Mapping[str, Any], index: int, shape: tuple[int, int]) -> "BlotRegion":
        def text(key: str, default: str = "") -> str:
            value = data.get(key, default)
            return str(value).strip() if value is not None else ""

        role = text("role", "target")
        if role not in {"target", "control"}:
            raise ValueError("WB ROI role 只支持 target 或 control。")
        return cls(
            roi_id=text("id", f"roi-{index + 1}"),
            asset_id=text("asset_id"),
            source_identity=(text("asset_id"), int((data.get("source") or {}).get("series", 0)),
                             int((data.get("source") or {}).get("frame", 0)), int((data.get("source") or {}).get("channel", 0))),
            region=PixelRegion.parse(data, shape),
            role=role,
            protein=text("protein", "target" if role == "target" else "control"),
            sample_id=text("sample_id"),
            group=text("group"),
            biological_replicate=text("biological_replicate"),
            technical_replicate=text("technical_replicate", "1"),
            exposure_id=text("exposure_id", "1"),
            experiment_id=text("experiment_id", "experiment-1"),
            control_roi_id=text("control_roi_id"),
            included=data.get("included", True) is not False,
        )

    @property
    def sample_key(self) -> tuple[str, str, str, str, str]:
        # A sample label may be reused across experimental groups. Loading
        # controls must belong to the same group as well as the same replicate;
        # otherwise a Treatment band can silently divide by a Control band.
        return self.experiment_id, self.group, self.sample_id, self.biological_replicate, self.technical_replicate

    @property
    def measurement_key(self) -> tuple[str, ...]:
        return (*self.sample_key, self.role, self.protein)


def select_plane(image: np.ndarray, channel: int = 0, channel_axis: int | None = None) -> np.ndarray:
    """Select a source plane as a view. No full-image float/8-bit conversion."""
    array = np.asarray(image)
    if array.dtype.kind not in "buif":
        raise ValueError("分析需要数值型图像。")
    if array.ndim == 2:
        if channel not in (0, None):
            raise ValueError("灰度图像只有一个通道。")
        return array
    if array.ndim != 3:
        raise ValueError("请先选择一个时间点/Z 层；分析输入须为二维或带通道的三维图像。")
    if channel_axis is None:
        if array.shape[-1] <= 4:
            channel_axis = -1
        elif array.shape[0] <= 4:
            channel_axis = 0
        else:
            raise ValueError("无法确定通道轴，请明确提供 channel_axis。")
    if channel_axis not in (0, 2, -1):
        raise ValueError("channel_axis 仅支持 0 或 -1。")
    if not 0 <= int(channel) < array.shape[channel_axis]:
        raise ValueError("选择的通道超出图像通道范围。")
    return array[int(channel), :, :] if channel_axis == 0 else array[:, :, int(channel)]


def source_range(array: np.ndarray, params: Mapping[str, Any]) -> tuple[float | None, float | None]:
    """ADC range is explicit for float or lower-bit detectors in wider storage."""
    if array.dtype.kind in "ui":
        limits = np.iinfo(array.dtype)
        low, high = float(limits.min), float(limits.max)
    elif array.dtype.kind == "b":
        low, high = 0.0, 1.0
    else:
        low, high = None, None
    if params.get("adc_bits") is not None:
        raw_bits = params["adc_bits"]
        numeric_bits = float(raw_bits)
        if (isinstance(raw_bits, bool) or not isfinite(numeric_bits)
                or not numeric_bits.is_integer() or not 1 <= numeric_bits <= 32):
            raise ValueError("adc_bits 应在 1–32 之间。")
        bits = int(numeric_bits)
        low, high = 0.0, float(2**bits - 1)
    # Explicit detector levels take precedence over ADC/storage defaults.
    if params.get("black_level") is not None:
        low = float(params["black_level"])
    if params.get("saturation_value") is not None:
        high = float(params["saturation_value"])
    if any(v is not None and not isfinite(v) for v in (low, high)):
        raise ValueError("探测器范围必须为有限数值。")
    if low is not None and high is not None and high <= low:
        raise ValueError("探测器上限必须大于下限。")
    return low, high


def descriptive_statistics(values: list[float]) -> dict[str, Any]:
    numbers = np.asarray(values, dtype=np.float64)
    numbers = numbers[np.isfinite(numbers)]
    count = int(numbers.size)
    if not count:
        return {"n": 0, "mean": None, "sd": None, "sem": None}
    # Scale before aggregation so a finite source cannot overflow just because
    # a sum or squared deviation has a larger magnitude than its final mean.
    scale = float(np.max(np.abs(numbers)))
    scaled = numbers / scale if scale else numbers
    mean = float(np.mean(scaled)) * scale if scale else 0.0
    deviation = float(np.std(scaled, ddof=1)) * scale if count > 1 else None
    deviation = deviation if deviation is None or isfinite(deviation) else None
    return {
        "n": count,
        "mean": mean,
        "sd": deviation,
        "sem": deviation / sqrt(count) if deviation is not None else None,
    }


def _edge_background(values: np.ndarray, fraction: float) -> tuple[float, float]:
    height, width = values.shape
    thickness_y = max(1, min(height // 2, ceil(height * fraction)))
    thickness_x = max(1, min(width // 2, ceil(width * fraction)))
    mask = np.zeros(values.shape, dtype=bool)
    mask[:thickness_y] = True
    mask[-thickness_y:] = True
    mask[:, :thickness_x] = True
    mask[:, -thickness_x:] = True
    border = values[mask & np.isfinite(values)]
    if not border.size:
        raise ValueError("ROI 的边界没有可用像素，无法估计背景。")
    median = float(np.median(border))
    mad = float(np.median(np.abs(border - median)))
    return median, mad


def _rolling_background(
    plane: np.ndarray, region: PixelRegion, polarity: str, radius: int,
    intensity_height: float,
) -> np.ndarray:
    """A true non-flat rolling ellipsoid, with a documented intensity scale.

    Context includes one radius around the ROI to reduce crop-boundary bias.
    The spatial kernel radius is pixels; intensity height is a fraction of the
    observed context range, not an accidental mixture of raw counts and pixels.
    This makes a uint16 frame with a narrow occupied range usable without
    quantizing that range. The normalization is exactly undone afterwards.
    """
    from skimage.restoration import ellipsoid_kernel, rolling_ball

    padding = radius
    y0, y1 = max(0, region.y - padding), min(plane.shape[0], region.y + region.height + padding)
    x0, x1 = max(0, region.x - padding), min(plane.shape[1], region.x + region.width + padding)
    context = np.array(plane[y0:y1, x0:x1], dtype=np.float64, copy=True)
    finite = np.isfinite(context)
    if not finite.all():
        raise ValueError("Rolling-ball 背景区域含无效像素；请调整 ROI 或先处理缺失数据。")
    lower, upper = float(np.min(context)), float(np.max(context))
    span = upper - lower
    if span <= 0:
        return np.full((region.height, region.width), float(context.flat[0]))
    signal = (upper - context) / span if polarity == "dark" else (context - lower) / span
    kernel = ellipsoid_kernel((2 * radius + 1, 2 * radius + 1), intensity_height)
    background_signal = rolling_ball(signal, kernel=kernel, workers=1)
    background = upper - background_signal * span if polarity == "dark" else lower + background_signal * span
    oy, ox = region.y - y0, region.x - x0
    return background[oy:oy + region.height, ox:ox + region.width]


def analyze_wb(images: dict[str, np.ndarray], params: dict[str, Any], *, cancel_event=None) -> dict[str, Any]:
    """Measure original-pixel ROIs and optionally normalize independent samples.

    Controls match the same sample, biological identity and technical replicate.
    Exposures of one measurement require selection and never increase n.
    Missing identity does not prevent viewing the raw integral, but it prevents
    unsubstantiated group statistics. All numeric outputs are JSON-safe.
    """
    started = perf_counter()
    roi_inputs = params.get("rois", [])
    if not isinstance(roi_inputs, list) or not roi_inputs:
        raise ValueError("请先添加至少一个 WB 测量 ROI。")
    if len(roi_inputs) > 5000:
        raise ValueError("一次最多分析 5000 个 ROI，请分批处理。")
    background_method = str(params.get("background", "border"))
    if background_method not in {"border", "rolling_ball", "none"}:
        raise ValueError("background 只支持 border、rolling_ball 或 none。")
    polarity = str(params.get("polarity", "dark"))
    if polarity not in {"dark", "bright"}:
        raise ValueError("polarity 必须明确选择 dark 或 bright。")
    fraction = float(params.get("border_fraction", 0.1))
    if not 0 < fraction <= 0.4:
        raise ValueError("border_fraction 应大于 0 且不超过 0.4。")
    radius = int(params.get("radius", 25))
    if not 1 <= radius <= 100:
        raise ValueError("Rolling-ball 半径须为 1–100 原图像素。")
    intensity_height = float(params.get("rolling_intensity_height", 0.1))
    if not 0 < intensity_height <= 1:
        raise ValueError("rolling_intensity_height 应为 0–1 之间的正数。")
    saturation_tolerance = float(params.get("saturation_tolerance", 0.0))
    if not 0 <= saturation_tolerance <= 1:
        raise ValueError("saturation_tolerance 须在 0–1 之间。")

    planes: dict[tuple, np.ndarray] = {}
    regions: dict[str, BlotRegion] = {}
    measured: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, roi_input in enumerate(roi_inputs):
        if cancel_event is not None and cancel_event.is_set():
            raise ValueError('WB 分析已取消。')
        source = dict(roi_input.get("source") or {})
        asset_id = str(source.get("asset_id") or roi_input.get("asset_id", ""))
        if roi_input.get("asset_id") and roi_input["asset_id"] != asset_id:
            raise ValueError("WB ROI 的 asset_id 与 source.asset_id 不一致。")
        source = {"asset_id": asset_id, "series": int(source.get("series", params.get("series", 0))),
                  "frame": int(source.get("frame", params.get("frame", params.get("page", 0)))),
                  "channel": int(source.get("channel", params.get("channel", 0)))}
        source_key = (asset_id, source["series"], source["frame"], source["channel"])
        image_key = source_key if source_key in images else asset_id
        if image_key not in images:
            raise ValueError(f"未找到 ROI 的原图：{asset_id}")
        if source_key not in planes:
            planes[source_key] = images[image_key] if image_key == source_key else select_plane(images[image_key], source["channel"], params.get("channel_axis"))
        plane = planes[source_key]
        spec = BlotRegion.parse({**roi_input, "asset_id": asset_id, "source": source}, index, plane.shape)
        if spec.roi_id in regions:
            raise ValueError(f"ROI ID 重复：{spec.roi_id}")
        regions[spec.roi_id] = spec
        source_values = plane[spec.region.slices]
        values = np.asarray(source_values, dtype=np.float64)
        finite = np.isfinite(values)
        if not finite.any():
            raise ValueError(f"ROI {spec.roi_id} 没有有效的源像素。")
        valid_values = values[finite]
        limits = source_range(plane, params)
        qc_flags: list[str] = []
        include = spec.included
        if not finite.all():
            qc_flags.append("nonfinite_source_pixels")
            include = False
        if min(values.shape) < 3:
            qc_flags.append("roi_too_narrow")
            include = False
        if background_method == "border":
            baseline, edge_mad = _edge_background(values, fraction)
            background = np.full(values.shape, baseline)
        elif background_method == "rolling_ball":
            background = _rolling_background(plane, spec.region, polarity, radius, intensity_height)
            baseline, edge_mad = float(np.mean(background)), None
        else:
            if polarity == "dark":
                if limits[1] is None:
                    raise ValueError("浮点暗条带在不扣背景时需要 saturation_value（已知白点），不能用观测最大值代替。")
                baseline = limits[1]
            else:
                baseline = 0.0
            background = np.full(values.shape, baseline)
            edge_mad = None
        corrected = background - values if polarity == "dark" else values - background
        signed_integral = float(np.sum(corrected[finite], dtype=np.float64))
        raw_integral = float(np.sum(valid_values, dtype=np.float64))
        if not isfinite(signed_integral) or not isfinite(raw_integral):
            raise ValueError(f"ROI {spec.roi_id} 的数值总和超出 float64 范围，无法可靠测量。")
        clip_level = limits[0] if polarity == "dark" else limits[1]
        if clip_level is None:
            saturation_fraction = None
            qc_flags.append("detector_range_unknown")
        else:
            clipped = valid_values <= clip_level if polarity == "dark" else valid_values >= clip_level
            saturation_fraction = float(np.count_nonzero(clipped) / valid_values.size)
            if saturation_fraction > saturation_tolerance:
                qc_flags.append("saturated_source_pixels")
                if not params.get("allow_saturated", False):
                    include = False
        if signed_integral <= 0:
            qc_flags.append("signal_at_or_below_background")
            include = False
        if not (spec.sample_id and spec.group and spec.biological_replicate):
            qc_flags.append("independent_sample_identity_missing")
            include = False
        if not spec.included:
            qc_flags.append("excluded_by_user")
        measured.append({
            "id": spec.roi_id, "asset_id": spec.asset_id, "source": source, "roi": spec.region.to_dict(),
            "sample_id": spec.sample_id, "group": spec.group, "biological_replicate": spec.biological_replicate,
            "technical_replicate": spec.technical_replicate, "exposure_id": spec.exposure_id,
            "experiment_id": spec.experiment_id, "protein": spec.protein, "role": spec.role,
            "source_dtype": str(plane.dtype), "source_bit_depth": int(plane.dtype.itemsize * 8),
            "pixel_count": int(valid_values.size), "raw_integrated": raw_integral,
            "raw_intensity": raw_integral, "raw_mean": raw_integral / valid_values.size,
            "background": baseline, "background_mad": edge_mad,
            "background_subtracted": signed_integral, "bg_subtracted": signed_integral,
            "mean_background_subtracted": signed_integral / valid_values.size,
            "positive_only_integral": float(np.sum(np.maximum(corrected[finite], 0), dtype=np.float64)),
            "saturation_fraction": saturation_fraction,
            "normalized": None, "fold": None, "control_roi_id": None,
            "analysis_included": include, "qc_flags": qc_flags,
        })

    by_id = {row["id"]: row for row in measured}
    if cancel_event is not None and cancel_event.is_set():
        raise ValueError('WB 分析已取消。')
    source_regions: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    logical_regions: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in measured:
        spec = regions[row["id"]]
        source_regions[(spec.source_identity, spec.region, spec.role)].append(row)
        if spec.sample_id:
            logical_regions[spec.measurement_key].append(row)
    # Re-labelling the same source pixels must not create independent samples.
    for duplicates in source_regions.values():
        selected_duplicates = [row for row in duplicates if regions[row["id"]].included]
        if len(selected_duplicates) > 1:
            for row in selected_duplicates:
                row["qc_flags"].append("duplicate_source_roi")
                row["analysis_included"] = False
    choices = params.get("selected_exposures", {})
    if not isinstance(choices, Mapping):
        raise ValueError("selected_exposures 必须为 sample_id → exposure_id 对象。")
    for equivalent in logical_regions.values():
        eligible = [row for row in equivalent if regions[row["id"]].included]
        if len(eligible) <= 1:
            continue
        sample_id = eligible[0]["sample_id"]
        selected = choices.get(sample_id)
        if selected is not None:
            chosen = [row for row in eligible if row["exposure_id"] == str(selected)]
        else:
            chosen = []
        if len(chosen) != 1:
            for row in eligible:
                row["analysis_included"] = False
                row["qc_flags"].append("exposure_or_technical_identity_ambiguous")
        else:
            for row in eligible:
                if row is not chosen[0]:
                    row["analysis_included"] = False
                    row["qc_flags"].append("unselected_exposure")

    controls = [row for row in measured if row["role"] == "control"]
    for row in measured:
        if row["role"] != "target":
            continue
        spec = regions[row["id"]]
        if spec.control_roi_id:
            candidate = by_id.get(spec.control_roi_id)
            candidates = [candidate] if candidate is not None and candidate["role"] == "control" else []
        else:
            candidates = [control for control in controls
                          if regions[control["id"]].sample_key == spec.sample_key
                          and control["analysis_included"]]
            if params.get("control_protein"):
                candidates = [control for control in candidates if control["protein"] == params["control_protein"]]
            same_exposure = [control for control in candidates if control["exposure_id"] == spec.exposure_id]
            if len(candidates) > 1 and len(same_exposure) == 1:
                candidates = same_exposure
        if len(candidates) != 1:
            row["qc_flags"].append("control_missing" if not candidates else "control_ambiguous")
            continue
        control = candidates[0]
        if regions[control["id"]].sample_key != spec.sample_key:
            row["qc_flags"].append("control_sample_identity_mismatch")
            continue
        control_spec = regions[control["id"]]
        if control_spec.source_identity == spec.source_identity and control_spec.region == spec.region:
            row["qc_flags"].append("control_is_target_source_roi")
            continue
        row["control_roi_id"] = control["id"]
        if not control["analysis_included"] or control["background_subtracted"] <= 0:
            row["qc_flags"].append("control_not_quantifiable")
            continue
        if row["analysis_included"]:
            normalized_value = row["background_subtracted"] / control["background_subtracted"]
            if isfinite(normalized_value):
                row["normalized"] = normalized_value
            else:
                row["qc_flags"].append("normalization_overflow")

    metric = str(params.get("metric", "normalized" if controls else "background_subtracted"))
    if metric not in {"normalized", "background_subtracted", "mean_background_subtracted"}:
        raise ValueError("metric 只支持 normalized、background_subtracted 或 mean_background_subtracted。")
    buckets: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in measured:
        if row["role"] == "target" and row["analysis_included"] and row.get(metric) is not None:
            buckets[(row["experiment_id"], row["group"], row["protein"], row["biological_replicate"])].append(row)
    biological_rows: list[dict[str, Any]] = []
    for (experiment_id, group, protein, biological_id), technical_rows in buckets.items():
        biological_rows.append({
            "experiment_id": experiment_id, "group": group, "protein": protein,
            "biological_replicate": biological_id, "n_technical": len(technical_rows),
            "source_roi_ids": [row["id"] for row in technical_rows],
            "value": float(np.mean([row[metric] for row in technical_rows])), "fold": None,
        })
    control_group = str(params.get("control_group", "")).strip()
    baselines: dict[tuple[str, str], float] = {}
    if control_group:
        baseline_values: dict[tuple[str, str], list[float]] = defaultdict(list)
        for biological in biological_rows:
            if biological["group"] == control_group:
                baseline_values[(biological["experiment_id"], biological["protein"])].append(biological["value"])
        baselines = {key: float(np.mean(values)) for key, values in baseline_values.items() if np.mean(values) > 0}
        for biological in biological_rows:
            baseline = baselines.get((biological["experiment_id"], biological["protein"]))
            if baseline:
                biological["fold"] = biological["value"] / baseline
        for row in measured:
            baseline = baselines.get((row["experiment_id"], row["protein"]))
            if baseline and row.get(metric) is not None and row["analysis_included"]:
                row["fold"] = row[metric] / baseline
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for biological in biological_rows:
        grouped[(biological["experiment_id"], biological["protein"], biological["group"])].append(biological)
    groups: list[dict[str, Any]] = []
    for (experiment_id, protein, group), entries in grouped.items():
        use_fold = bool(control_group) and all(entry["fold"] is not None for entry in entries)
        values = [entry["fold"] if use_fold else entry["value"] for entry in entries]
        summary = descriptive_statistics(values)
        groups.append({"experiment_id": experiment_id, "protein": protein, "group": group,
                       **summary, "n_biological": summary["n"], "n_technical": sum(e["n_technical"] for e in entries),
                       "metric": "fold" if use_fold else metric, "values": values})
    qc_counts: dict[str, int] = defaultdict(int)
    for row in measured:
        for flag in row["qc_flags"]:
            qc_counts[flag] += 1
    messages = {
        "independent_sample_identity_missing": "未填写样本、组别或生物重复身份：保留原始测量，不计入组统计。",
        "duplicate_source_roi": "相同源像素被重复标注，已排除其组统计；请检查 ROI 对应关系。",
        "exposure_or_technical_identity_ambiguous": "同一测量存在多个曝光或重复条目，请选择曝光或填写技术重复身份。",
        "saturated_source_pixels": "检测到原始像素削顶，默认不用于定量；更改显示色阶不能修复原始饱和。",
        "signal_at_or_below_background": "有 ROI 的信号不高于背景，保留有符号测量，不截成虚假的正值。",
        "detector_range_unknown": "浮点源图未提供探测器范围，无法判断原始饱和。",
        "control_missing": "有目标 ROI 无匹配内参，normalized 为空；未自动跨样本配对。",
        "control_ambiguous": "存在多个候选内参，请用 control_roi_id 明确指定。",
        "control_sample_identity_mismatch": "指定内参与目标的实验/组别/样本/生物重复/技术重复身份不同，未进行归一化。",
        "control_not_quantifiable": "内参存在无效、饱和或非正信号，未进行归一化。",
        "control_is_target_source_roi": "目标与内参指向相同源像素，未进行归一化。",
        "normalization_overflow": "内参过小导致归一化超出数值范围，normalized 已置空。",
        "nonfinite_source_pixels": "原图包含无效数值，该 ROI 不参与组统计。",
        "roi_too_narrow": "ROI 过窄，不能可靠估计背景；原始数值仍予保留。",
    }
    warnings.extend(f"{messages[flag]} ({count})" for flag, count in qc_counts.items() if flag in messages)
    if any(group["n_biological"] < 2 for group in groups):
        warnings.append("部分组只有一个独立生物重复，SD/SEM 为空，不生成显著性结论。")
    if control_group and len(baselines) < len({(r["experiment_id"], r["protein"]) for r in biological_rows}):
        warnings.append("部分实验/蛋白缺少有效对照组，未为其生成 fold change。")
    if not groups:
        warnings.append("当前没有可用于独立样本组统计的数据；可先查看逐 ROI 测量和质控标记。")
    from .statistics import compare_biological_groups
    inference = compare_biological_groups(biological_rows, feature="protein", value="value",
                                         control_group=control_group, test=str(params.get("test", "none")),
                                         basis="biological_" + metric)
    warnings.extend(inference["warnings"])
    return {
        "kind": "wb", "rows": measured, "biological_rows": biological_rows, "groups": groups,
        "comparisons": inference["comparisons"], "omnibus": inference["omnibus"],
        "warnings": warnings, "qc_counts": dict(qc_counts),
        "method": {"measurement_basis": "original_pixels_float64", "background": background_method,
                   "border_statistic": "median", "polarity": polarity, "metric": metric,
                   "radius_px": radius if background_method == "rolling_ball" else None,
                   "rolling_intensity_height": intensity_height if background_method == "rolling_ball" else None,
                   "rolling_intensity_scale": "observed_context_range" if background_method == "rolling_ball" else None,
                   "geometry": "source_pixel_roi_no_resampling", "negative_values": "preserved",
                   "statistics_unit": "biological_replicate", "display_lut_used": False,
                   "control_group": control_group or None, "test": str(params.get("test", "none"))},
        "elapsed_ms": (perf_counter() - started) * 1000,
    }
