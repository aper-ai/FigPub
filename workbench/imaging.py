"""Shared display pipeline. Raw numerical analysis never calls this module's LUT.

All image geometry is in original pixels. The display range is calculated before
cropping, so moving a crop does not silently change the apparent contrast.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
import math

import numpy as np
from PIL import Image, ImageOps


MAX_SOURCE_PIXELS = 160_000_000


@dataclass(frozen=True, slots=True)
class PixelRect:
    x: int
    y: int
    width: int
    height: int

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None, width: int, height: int) -> "PixelRect":
        value = value or {}
        numbers = [float(value.get("x", 0)), float(value.get("y", 0)),
                   float(value.get("width", width)), float(value.get("height", height))]
        if not all(math.isfinite(number) for number in numbers):
            raise ValueError("裁切坐标必须是有限数字")
        x, y = max(0, math.floor(numbers[0])), max(0, math.floor(numbers[1]))
        right = min(width, math.ceil(numbers[0] + numbers[2]))
        bottom = min(height, math.ceil(numbers[1] + numbers[3]))
        if right <= x or bottom <= y:
            raise ValueError("裁切区域没有覆盖原图像素")
        return cls(x, y, right - x, bottom - y)


def load_image_array(path: str | Path, *, page: int = 0, channel: int | None = None,
                     series: int = 0, frame: int | None = None) -> np.ndarray:
    """Decode a display plane without reducing source precision.

    Complex microscopy analysis owns its explicit channel/series selection. This
    function chooses a documented plane for figure rendering, never a projection.
    """
    from .planes import describe_raster, read_raster
    metadata = describe_raster(path)
    if not 0 <= series < len(metadata['metadata']['series']):
        raise ValueError('Image series is outside its source range.')
    spec = metadata['metadata']['series'][series]
    selected = channel
    if selected is None and not spec.get('rgb_samples') and spec['channels'] > 1:
        # Legacy single-image panels display their first scalar signal. A
        # multichannel composition must carry an explicit channel_view recipe.
        selected = 0
    if selected is None and spec.get('logical_channels', 1) > 1:
        selected = 0
    return read_raster(path, series=series, frame=page if frame is None else frame, channel=selected)


def display_bounds(array: np.ndarray, display: dict[str, Any] | None = None) -> tuple[float, float]:
    settings = display or {}
    if array.ndim == 3 and array.dtype.kind in 'bui':
        # RGB components use their recorded integer scale, including 16-bit
        # PNG/TIFF. This matches multichannel defaults without a contrast jump.
        default_low, default_high = (0.0, 1.0) if array.dtype.kind == 'b' else (float(np.iinfo(array.dtype).min), float(np.iinfo(array.dtype).max))
    else:
        # Deterministic full-image range: neither a percentile clip nor per-crop auto-contrast.
        minimum = np.nanmin(array) if array.size else 0
        maximum = np.nanmax(array) if array.size else 1
        default_low = float(minimum) if np.isfinite(minimum) else 0.0
        default_high = float(maximum) if np.isfinite(maximum) else default_low + 1.0
    low = default_low if settings.get("low") is None else float(settings["low"])
    high = default_high if settings.get("high") is None else float(settings["high"])
    if not math.isfinite(low) or not math.isfinite(high):
        raise ValueError("显示上下限必须是有限数字")
    if high <= low:
        if settings.get("low") is not None or settings.get("high") is not None:
            raise ValueError("显示上限必须大于下限")
        high = low + 1.0
    return low, high


def display_image(array: np.ndarray, display: dict[str, Any] | None = None) -> Image.Image:
    settings = display or {}
    low, high = display_bounds(array, settings)
    gamma = float(settings.get("gamma", 1) or 1)
    if not math.isfinite(gamma) or not 0.1 <= gamma <= 5:
        raise ValueError("Gamma 必须在 0.1–5 之间")
    alpha = None
    values = array
    if values.ndim == 3 and values.shape[-1] == 4:
        alpha = values[..., 3]
        values = values[..., :3]
    intensity = np.nan_to_num(values.astype(np.float32), nan=low, posinf=high, neginf=low)
    intensity -= low
    intensity *= 1.0 / (high - low)
    np.clip(intensity, 0, 1, out=intensity)
    if gamma != 1:
        np.power(intensity, 1.0 / gamma, out=intensity)
    if settings.get("invert", False):
        np.subtract(1.0, intensity, out=intensity)
    pixels = np.rint(intensity * 255).astype(np.uint8)
    if settings.get("grayscale") and pixels.ndim == 3:
        pixels = np.rint(pixels[..., :3] @ np.array([0.2126, 0.7152, 0.0722])).astype(np.uint8)
    image = Image.fromarray(pixels)
    if alpha is not None:
        if alpha.dtype != np.uint8:
            ceiling = float(np.iinfo(alpha.dtype).max) if alpha.dtype.kind in 'ui' else 1.0
            alpha = np.rint(np.clip(np.nan_to_num(alpha.astype(float) / ceiling, nan=0, posinf=1, neginf=0), 0, 1) * 255).astype(np.uint8)
        image.putalpha(Image.fromarray(alpha))
    return image


def crop_display_image(array: np.ndarray, panel: dict[str, Any]) -> Image.Image:
    """Crop before LUT to reduce allocations; use bounds from the complete source."""
    height, width = array.shape[:2]
    rectangle = PixelRect.from_mapping(panel.get("crop"), width, height)
    settings = dict(panel.get("display") or {})
    settings["low"], settings["high"] = display_bounds(array, settings)
    crop = array[rectangle.y:rectangle.y + rectangle.height, rectangle.x:rectangle.x + rectangle.width]
    image = display_image(crop, settings)
    if panel.get("flip_h"):
        image = ImageOps.mirror(image)
    if panel.get("flip_v"):
        image = ImageOps.flip(image)
    return image


def png_bytes(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, "PNG", optimize=False, compress_level=3)
    return buffer.getvalue()


def retained_segments(panel: dict[str, Any], source_width: int, source_height: int) -> list[tuple[PixelRect, float, float]]:
    """Source rectangles and relative output x/width for disclosed lane removal.

    Source slices retain their identities. Gaps consume physical output width and
    are never filled with fabricated pixels. Indices in hidden_lanes are 0-based.
    """
    crop = PixelRect.from_mapping(panel.get("crop"), source_width, source_height)
    gel = panel.get("gel") or {}
    centers = [float(value) for value in gel.get("lane_centers", [])]
    hidden = set(int(value) for value in gel.get("hidden_lanes", []))
    if not centers or not hidden:
        return [(crop, 0.0, 1.0)]
    if centers != sorted(centers):
        raise ValueError("泳道位置必须按原图从左到右排序")
    boundaries = [crop.x] + [(left + right) / 2 for left, right in zip(centers, centers[1:])] + [crop.x + crop.width]
    intervals: list[tuple[int, int]] = []
    for index in range(len(centers)):
        left = max(crop.x, round(boundaries[index]))
        right = min(crop.x + crop.width, round(boundaries[index + 1]))
        if index in hidden or right <= left:
            continue
        if intervals and intervals[-1][1] == left:
            intervals[-1] = intervals[-1][0], right
        else:
            intervals.append((left, right))
    if not intervals:
        raise ValueError("所有泳道均已隐藏，无法导出空面板")
    gap_fraction = 0.025
    available_fraction = 1.0 - gap_fraction * (len(intervals) - 1)
    if available_fraction <= 0:
        raise ValueError("拼接片段过多，请拆分图版")
    total_pixels = sum(right - left for left, right in intervals)
    result = []
    position = 0.0
    for left, right in intervals:
        fraction = (right - left) / total_pixels * available_fraction
        result.append((PixelRect(left, crop.y, right - left, crop.height), position, fraction))
        position += fraction + gap_fraction
    return result
