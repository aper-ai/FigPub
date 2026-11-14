"""Lossless, replayable instance review on the original pixel grid.

The immutable automatic segmentation is the only replay base. Edits are small,
explicit operations; masks are never reconstructed from simplified contours.
Every operation preserves source intensities, and split/merge preserve the
foreground union. This module does not write projects or source files.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import io
import json
import re
import zlib
from collections.abc import Mapping
from typing import Any

import numpy as np
from scipy import ndimage


MAX_PIXELS = 16_000_000
MAX_COMPRESSED_BYTES = 2 * 1024 * 1024
MAX_INSTANCES = 100_000
MAX_LABEL_ID = 1_000_000
MAX_OPERATIONS = 200
ENCODING = "uint32-le-zlib-base64-v1"


def _integer(value: Any, label: str, low: int = 0, high: int = MAX_PIXELS) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or not low <= value <= high:
        raise ValueError(f"{label} 须为 {low}–{high} 的整数。")
    return int(value)


def _cancel(event: Any) -> None:
    if event is not None and event.is_set():
        raise ValueError("实例复核已取消。")


def _geometry(shape: Any, origin: Any, source_shape: Any) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
    if not isinstance(shape, (tuple, list)) or len(shape) != 2:
        raise ValueError("实例标签须为二维。")
    height, width = [_integer(value, "标签尺寸", 1, MAX_PIXELS) for value in shape]
    if height * width > MAX_PIXELS:
        raise ValueError("实例复核最多处理 1600 万像素，请缩小分析 ROI。")
    if not isinstance(origin, (tuple, list)) or len(origin) != 2:
        raise ValueError("实例标签原点须为原图 x/y。")
    x, y = [_integer(value, "标签原点", 0, 60_000_000) for value in origin]
    if not isinstance(source_shape, (tuple, list)) or len(source_shape) != 2:
        raise ValueError("实例标签必须保留原图高/宽。")
    source_height, source_width = [_integer(value, "原图尺寸", 1, 60_000_000) for value in source_shape]
    if source_height * source_width > 60_000_000 or x + width > source_width or y + height > source_height:
        raise ValueError("实例标签超出原始图像范围。")
    return (height, width), (x, y), (source_height, source_width)


def _digest(raw: bytes, shape: tuple[int, int], origin: tuple[int, int], source_shape: tuple[int, int]) -> str:
    header = json.dumps({"shape": shape, "origin": origin, "source_shape": source_shape,
                         "encoding": ENCODING}, sort_keys=True, separators=(",", ":")).encode("ascii")
    digest = hashlib.sha256(header)
    digest.update(b"\0")
    digest.update(raw)
    return digest.hexdigest()


def _validate_labels(labels: np.ndarray) -> None:
    if labels.ndim != 2 or labels.dtype.kind not in "ui" or not labels.size:
        raise ValueError("实例标签必须为非空二维非负整数数组。")
    if labels.min(initial=0) < 0 or labels.max(initial=0) > MAX_LABEL_ID:
        raise ValueError("实例标签 ID 超出支持范围。")
    sizes = np.bincount(labels.ravel().astype(np.int64, copy=False))
    if np.count_nonzero(sizes[1:]) > MAX_INSTANCES:
        raise ValueError("实例数量超过 100000，请缩小 ROI。")


def encode_instance_mask(labels: np.ndarray, *, origin: tuple[int, int] = (0, 0),
                         source_shape: tuple[int, int] | None = None) -> dict[str, Any]:
    array = np.asarray(labels)
    shape, origin, source_shape = _geometry(array.shape, origin, array.shape if source_shape is None else source_shape)
    _validate_labels(array)
    raw = np.ascontiguousarray(array, dtype="<u4").tobytes()
    compressed = zlib.compress(raw, level=4)
    if len(compressed) > MAX_COMPRESSED_BYTES:
        raise ValueError("实例标签压缩后超过 2 MiB，请缩小 ROI 后复核。")
    return {"encoding": ENCODING, "shape": list(shape), "origin": list(origin),
            "source_shape": list(source_shape), "nbytes": len(raw),
            "sha256": _digest(raw, shape, origin, source_shape),
            "data": base64.b64encode(compressed).decode("ascii")}


def decode_instance_mask(payload: Mapping[str, Any]) -> np.ndarray:
    if not isinstance(payload, Mapping) or payload.get("encoding") != ENCODING:
        raise ValueError("不支持的实例标签编码。")
    shape, origin, source_shape = _geometry(payload.get("shape"), payload.get("origin"), payload.get("source_shape"))
    expected = shape[0] * shape[1] * 4
    if payload.get("nbytes") != expected or isinstance(payload.get("nbytes"), bool):
        raise ValueError("实例标签字节数与几何不一致。")
    encoded = payload.get("data")
    if not isinstance(encoded, str) or len(encoded) > ((MAX_COMPRESSED_BYTES + 2) // 3) * 4:
        raise ValueError("实例标签压缩数据超过限制。")
    try:
        compressed = base64.b64decode(encoded, validate=True)
        if len(compressed) > MAX_COMPRESSED_BYTES:
            raise ValueError("实例标签压缩数据超过限制。")
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, expected + 1)
        if len(raw) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError("实例标签长度不符，或包含额外压缩流。")
    except (ValueError, zlib.error) as exc:
        raise ValueError("实例标签数据损坏或超出解压上限。") from exc
    digest = payload.get("sha256")
    if not isinstance(digest, str) or not hmac.compare_digest(digest, _digest(raw, shape, origin, source_shape)):
        raise ValueError("实例标签哈希不匹配，未使用损坏或错位的数据。")
    result = np.frombuffer(raw, dtype="<u4").reshape(shape)
    _validate_labels(result)
    # The bytes-backed array is read-only, including when called by export.
    return result


def attach_instance_mask(result: dict[str, Any], labels: np.ndarray, *, origin=(0, 0), source_shape=None) -> dict[str, Any]:
    """Do not discard completed scientific results solely due to editor limits."""
    try:
        result["instance_mask"] = encode_instance_mask(labels, origin=origin, source_shape=source_shape)
    except ValueError as exc:
        result["instance_review_unavailable"] = str(exc)
        result.setdefault("warnings", []).append(str(exc))
    return result


def _ids(value: Any, *, minimum: int = 1, maximum: int = 1000) -> list[int]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"请选择 {minimum}–{maximum} 个实例 ID。")
    identifiers = [_integer(item, "实例 ID", 1, MAX_LABEL_ID) for item in value]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("同一实例不能重复选择。")
    return identifiers


def _bbox(mask: np.ndarray) -> tuple[slice, slice]:
    rows, columns = np.nonzero(mask)
    if not rows.size:
        raise ValueError("所选实例不存在或已被移除。")
    return slice(int(rows.min()), int(rows.max()) + 1), slice(int(columns.min()), int(columns.max()) + 1)


def _point(value: Any, origin: tuple[int, int], shape: tuple[int, int]) -> tuple[int, int]:
    if (not isinstance(value, (tuple, list)) or len(value) != 2
            or any(isinstance(item, bool) or not isinstance(item, (int, float)) or not np.isfinite(item) for item in value)):
        raise ValueError("拆分种子必须为原始图像 x/y 坐标。")
    x, y = int(np.floor(value[0] - origin[0])), int(np.floor(value[1] - origin[1]))
    if not 0 <= x < shape[1] or not 0 <= y < shape[0]:
        raise ValueError("拆分种子超出分析区域。")
    return y, x


def _simple_polygon(points: np.ndarray) -> bool:
    # Vectorized proper crossings; repeated non-neighbouring vertices also fail.
    if len(np.unique(points, axis=0)) != len(points):
        return False
    start, end = points, np.roll(points, -1, axis=0)
    a, b = start[:, None, :], end[:, None, :]
    c, d = start[None, :, :], end[None, :, :]
    cross = lambda u, v: u[..., 0] * v[..., 1] - u[..., 1] * v[..., 0]
    first = cross(b - a, c - a) * cross(b - a, d - a)
    second = cross(d - c, a - c) * cross(d - c, b - c)
    crossing = (first <= 0) & (second <= 0)
    overlap = ((np.maximum(np.minimum(a[..., 0], b[..., 0]), np.minimum(c[..., 0], d[..., 0]))
                <= np.minimum(np.maximum(a[..., 0], b[..., 0]), np.maximum(c[..., 0], d[..., 0])))
               & (np.maximum(np.minimum(a[..., 1], b[..., 1]), np.minimum(c[..., 1], d[..., 1]))
                  <= np.minimum(np.maximum(a[..., 1], b[..., 1]), np.maximum(c[..., 1], d[..., 1]))))
    invalid = crossing & overlap
    np.fill_diagonal(invalid, False)
    indexes = np.arange(len(points))
    invalid[indexes, (indexes + 1) % len(points)] = False
    invalid[(indexes + 1) % len(points), indexes] = False
    return not invalid.any()


def _manual_region(shape: Mapping[str, Any], origin: tuple[int, int], canvas_shape: tuple[int, int]) -> np.ndarray:
    from .microscopy_measurements import analysis_region

    if not isinstance(shape, Mapping) or shape.get("type", "rectangle") not in {"rectangle", "ellipse", "polygon"}:
        raise ValueError("补画对象仅支持矩形、椭圆或简单多边形。")
    region = copy.deepcopy(dict(shape))
    if region.get("type") == "polygon":
        values = region.get("points")
        if not isinstance(values, list) or not 3 <= len(values) <= 512:
            raise ValueError("补画多边形需 3–512 个顶点。")
        points = np.asarray(values, dtype=np.float64)
        if points.shape != (len(values), 2) or not np.isfinite(points).all() or not _simple_polygon(points):
            raise ValueError("补画多边形必须为不自交、无重复顶点的有限坐标。")
        points -= np.asarray(origin)
        if np.any(points < 0) or np.any(points[:, 0] > canvas_shape[1]) or np.any(points[:, 1] > canvas_shape[0]):
            raise ValueError("补画多边形超出原分析区域。")
        if abs(float(np.sum(points[:, 0] * np.roll(points[:, 1], -1) - points[:, 1] * np.roll(points[:, 0], -1)))) < 1e-9:
            raise ValueError("补画多边形不能退化为线。")
        region.update(points=points.tolist(), x=float(points[:, 0].min()), y=float(points[:, 1].min()),
                      width=float(np.ptp(points[:, 0])), height=float(np.ptp(points[:, 1])))
    else:
        for key in ("x", "y", "width", "height"):
            if isinstance(region.get(key), bool) or not isinstance(region.get(key), (int, float)) or not np.isfinite(region[key]):
                raise ValueError("补画区域几何必须是有限数值。")
        region["x"] -= origin[0]
        region["y"] -= origin[1]
        if (region["x"] < 0 or region["y"] < 0 or region["width"] <= 0 or region["height"] <= 0
                or region["x"] + region["width"] > canvas_shape[1]
                or region["y"] + region["height"] > canvas_shape[0]):
            raise ValueError("补画区域超出原分析区域。")
    selected = analysis_region(region, canvas_shape)
    output = np.zeros(canvas_shape, dtype=bool)
    output[selected.bounds.slices] = selected.mask
    if not output.any():
        raise ValueError("补画区域没有覆盖原始像素中心。")
    if ndimage.label(output, structure=np.ones((3, 3), dtype=np.uint8))[1] != 1:
        raise ValueError("补画对象必须连通。")
    return output


def validate_review_operations(operations: Any, cursor: int | None = None) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(operations, list) or len(operations) > MAX_OPERATIONS:
        raise ValueError("每个分析结果最多保存 200 步实例修订。")
    cursor = len(operations) if cursor is None else _integer(cursor, "复核游标", 0, len(operations))
    output = []
    seen = set()
    for item in operations:
        if not isinstance(item, dict):
            raise ValueError("实例修订必须为操作记录对象。")
        identifier = item.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier) or identifier in seen:
            raise ValueError("操作 ID 必须非空且唯一；同一操作不能重复追加。")
        seen.add(identifier)
        kind = item.get("type")
        if kind not in {"merge", "split", "delete", "add"}:
            raise ValueError("未知实例修订类型。")
        allowed = {"id", "type"} | {"merge": {"ids", "allow_disconnected"}, "split": {"instance_id", "seeds"},
                                    "delete": {"ids"}, "add": {"region"}}[kind]
        if set(item) - allowed:
            raise ValueError("实例修订含不支持的字段。")
        if len(json.dumps(item, ensure_ascii=True, allow_nan=False)) > 64 * 1024:
            raise ValueError("单步实例修订超过大小限制。")
        output.append(copy.deepcopy(item))
    return output, cursor


def _raw_plane(image: np.ndarray | Mapping[str, np.ndarray], params: dict[str, Any], method: str) -> np.ndarray:
    if isinstance(image, Mapping):
        from .microscopy_measurements import resolve_channels
        channels = resolve_channels(image, params, default_all=False)
        if len(channels) != 1:
            raise ValueError("实例强度复核需要明确的单个原始测量通道。")
        return channels[0].values
    array = np.asarray(image)
    if method == "ihc":
        from .microscopy import _deconvolve_hdab
        axis = params.get("channel_axis", -1)
        return _deconvolve_hdab(np.moveaxis(array, axis, -1), params)[1]
    if array.ndim != 2:
        raise ValueError("实例强度复核需要二维原始平面。")
    return array


def _allowed_pixels(image: np.ndarray | Mapping[str, np.ndarray], params: dict[str, Any], method: str,
                    shape: tuple[int, int], origin: tuple[int, int], source_shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray | None]:
    from .microscopy_measurements import analysis_region

    region = analysis_region(params.get("roi"), source_shape)
    if (region.bounds.x, region.bounds.y) != origin or (region.bounds.height, region.bounds.width) != shape:
        raise ValueError("实例标签几何与原始分析 ROI 不一致。")
    if method in {"fiber_cross_section", "myotube"}:
        if isinstance(image, Mapping):
            channel_id = params.get("fiber_channel_id") or params.get("fiber_channel") or next(iter(image), None)
            raw = np.asarray(image[channel_id]) if channel_id in image else np.asarray(next(iter(image.values())))
        else:
            raw = np.asarray(image)
    else:
        raw = _raw_plane(image, params, method)
    if raw.shape != source_shape:
        raise ValueError("复核原始图像尺寸与不可变实例标签不一致。")
    crop = raw[origin[1]:origin[1] + shape[0], origin[0]:origin[0] + shape[1]]
    # Imported fiber labels are independent geometric evidence: an intensity
    # channel may contain NaN without erasing valid imported geometry. The
    # specialized fiber recomputation reports finite intensity sample counts.
    allowed = region.mask if method in {"fiber_cross_section", "myotube"} else region.mask & np.isfinite(crop)
    return allowed, crop


def _apply_operation(labels: np.ndarray, operation: dict[str, Any], origin: tuple[int, int],
                     allowed: np.ndarray, next_id: int) -> tuple[int, dict[str, Any]]:
    kind = operation["type"]
    before = int(np.count_nonzero(labels))
    record = {"id": operation["id"], "type": kind}
    present = set(int(value) for value in np.unique(labels) if value)
    if kind in {"merge", "delete"}:
        identifiers = _ids(operation.get("ids"), minimum=2 if kind == "merge" else 1, maximum=32 if kind == "merge" else 1000)
        if not set(identifiers) <= present:
            raise ValueError("所选实例已不存在，请重新选择当前对象。")
        mask = np.isin(labels, identifiers)
        if kind == "merge":
            disconnected = operation.get("allow_disconnected", False)
            if not isinstance(disconnected, bool):
                raise ValueError("allow_disconnected 必须明确为布尔值。")
            box = _bbox(mask)
            components = ndimage.label(mask[box], structure=np.ones((3, 3), dtype=np.uint8))[1]
            if components != 1 and not disconnected:
                raise ValueError("所选实例不相邻；仅在明确允许分离区域时合并，软件不会填补间隙。")
            target = min(identifiers)
            labels[mask] = target
            record.update(input_ids=identifiers, output_ids=[target], disconnected_components=int(components))
        else:
            labels[mask] = 0
            record.update(input_ids=identifiers, output_ids=[])
    elif kind == "split":
        identifier = _integer(operation.get("instance_id"), "待拆分实例 ID", 1, MAX_LABEL_ID)
        if identifier not in present:
            raise ValueError("待拆分实例已不存在。")
        seeds = operation.get("seeds")
        if not isinstance(seeds, list) or len(seeds) != 2:
            raise ValueError("拆分需要在同一实例内点选两个不同的种子。")
        first, second = [_point(point, origin, labels.shape) for point in seeds]
        if first == second or labels[first] != identifier or labels[second] != identifier:
            raise ValueError("两个拆分种子必须位于同一所选实例的不同原始像素。")
        if next_id > MAX_LABEL_ID:
            raise ValueError("实例 ID 已达上限，请保存并重新分析较小 ROI。")
        from skimage.segmentation import watershed
        mask = labels == identifier
        box = _bbox(mask)
        local = mask[box]
        markers = np.zeros(local.shape, dtype=np.int32)
        markers[first[0] - box[0].start, first[1] - box[1].start] = 1
        markers[second[0] - box[0].start, second[1] - box[1].start] = 2
        # Manual seeds express the intended two parts. A negative-distance
        # surface can let a seed on a deeper plateau swallow almost the whole
        # object, leaving the second seed a one-pixel child. A flat surface
        # makes watershed grow geodesically from both explicit seeds inside
        # the object, without deleting boundary pixels or using display values.
        divided = watershed(np.zeros(local.shape, dtype=np.uint8), markers=markers,
                            mask=local, connectivity=np.ones((3, 3), dtype=np.uint8))
        if not np.array_equal(divided > 0, local) or not np.any(divided == 1) or not np.any(divided == 2):
            raise ValueError("两个种子未覆盖该实例的全部连通部分，未丢弃任何像素。")
        view = labels[box]
        view[divided == 2] = next_id
        record.update(input_ids=[identifier], output_ids=[identifier, next_id], seeds=copy.deepcopy(seeds),
                      algorithm="two_seed_geodesic_watershed_union_preserving")
        next_id += 1
    else:
        if next_id > MAX_LABEL_ID:
            raise ValueError("实例 ID 已达上限。")
        manual = _manual_region(operation.get("region"), origin, labels.shape)
        if np.any(manual & ~allowed):
            raise ValueError("补画区域包含分析 ROI 之外或原始值无效的像素。")
        if np.any(labels[manual] != 0):
            raise ValueError("补画区域与现有实例重叠；请调整轮廓，原有标签不会被覆盖。")
        labels[manual] = next_id
        record.update(input_ids=[], output_ids=[next_id], region=copy.deepcopy(operation["region"]))
        next_id += 1
    record.update(foreground_area_before_px=before, foreground_area_after_px=int(np.count_nonzero(labels)))
    if kind in {"merge", "split"} and record["foreground_area_before_px"] != record["foreground_area_after_px"]:
        raise RuntimeError("实例修订违反像素并集守恒。")
    if len(present) + (1 if kind in {"add", "split"} else 0) > MAX_INSTANCES:
        raise ValueError("实例数量超过 100000。")
    return next_id, record


def review_instances(base_result: Mapping[str, Any], image: np.ndarray | Mapping[str, np.ndarray],
                     operations: list[dict[str, Any]], cursor: int | None = None,
                     params: dict[str, Any] | None = None, *, cancel_event: Any = None) -> dict[str, Any]:
    """Replay an edit prefix; repeating identical input produces identical masks."""
    _cancel(cancel_event)
    operations, cursor = validate_review_operations(operations, cursor)
    if not isinstance(base_result, Mapping) or not isinstance(base_result.get("instance_mask"), Mapping):
        raise ValueError("原始分析没有可复核的无损实例标签，请重新运行分割。")
    if base_result.get("instance_review"):
        raise ValueError("复核必须以未修改的原始分割结果为基准，不能叠套衍生结果。")
    payload = base_result["instance_mask"]
    original = decode_instance_mask(payload)
    labels = original.copy()
    origin, source_shape = tuple(payload["origin"]), tuple(payload["source_shape"])
    params = copy.deepcopy(params or {})
    method_details = base_result.get("method", {})
    method = method_details.get("name", params.get("method", "nuclei")) if isinstance(method_details, Mapping) else str(method_details)
    allowed, raw = _allowed_pixels(image, params, method, labels.shape, origin, source_shape)
    if np.any((labels > 0) & ~allowed):
        raise ValueError("原始分割包含分析范围之外或无效源像素。")
    next_id = int(labels.max(initial=0)) + 1
    trail = []
    for operation in operations[:cursor]:
        _cancel(cancel_event)
        next_id, record = _apply_operation(labels, operation, origin, allowed, next_id)
        trail.append(record)
    _cancel(cancel_event)
    if method in {"fiber_cross_section", "myotube"}:
        from .fibers import recompute_fiber_result
        result = recompute_fiber_result(image, params, labels, origin=origin, cancel_event=cancel_event)
        if not result.get("instance_mask"):
            raise ValueError(result.get("instance_review_unavailable", "修订标签超过复核保存范围，未应用此操作。"))
    else:
        from .microscopy import SpatialCalibration, _region_objects
        from .analysis import PixelRegion
        calibration = SpatialCalibration.parse(params)
        ids = np.unique(labels)
        ids = ids[ids > 0]
        mapping = np.zeros(int(labels.max(initial=0)) + 1, dtype=np.int32)
        mapping[ids] = np.arange(1, len(ids) + 1)
        dense = mapping[labels]
        objects, count, notes = _region_objects(dense, raw, PixelRegion(origin[0], origin[1], labels.shape[1], labels.shape[0]),
                                               calibration, True, min(10000, int(params.get("max_objects", 10000))), allowed)
        source_channel = method_details.get("source_channel", {}) if isinstance(method_details, Mapping) else {}
        for item in objects:
            item["id"] = int(ids[item["id"] - 1])
            item["object_type"] = "reviewed_" + str(base_result.get("summary", {}).get("object_type", "instance"))
            if source_channel.get("id") or source_channel.get("channel_id"):
                item["channel_id"] = source_channel.get("id") or source_channel["channel_id"]
        area = int(np.count_nonzero(labels))
        summary = {**base_result.get("summary", {}), "count": count,
                   "foreground_area_px": area, "foreground_fraction": area / int(allowed.sum()) if allowed.any() else None}
        if method == "ihc":
            summary.update(positive_area_px=area, positive_fraction=area / summary["tissue_area_px"] if summary.get("tissue_area_px") else None)
        if calibration:
            summary["foreground_area_um2"] = area * calibration.pixel_area_um2
            if method == "ihc":
                summary["positive_area_um2"] = summary["foreground_area_um2"]
        result = {**base_result, "objects": objects, "rows": objects, "count": count, "summary": summary,
                  "warnings": list(base_result.get("warnings", [])) + notes,
                  "method": {**method_details, "instance_geometry": "reviewed_native_source_pixel_grid"}}
        result["instance_mask"] = encode_instance_mask(labels, origin=origin, source_shape=source_shape)
    _cancel(cancel_event)
    original_count = int(np.count_nonzero(np.bincount(original.ravel().astype(np.int64, copy=False))[1:]))
    result["instance_review"] = {"schema_version": 1, "base_mask_sha256": payload["sha256"],
                                 "cursor": cursor, "total_operations": len(operations),
                                 "original_count": original_count, "reviewed_count": int(result["count"]),
                                 "original_foreground_area_px": int(np.count_nonzero(original)),
                                 "reviewed_foreground_area_px": int(np.count_nonzero(labels)),
                                 "operations": trail, "source_pixels_modified": False,
                                 "measurement_basis": "original_source_values_in_reviewed_instance_masks"}
    result.setdefault("warnings", []).append("这是人工复核后的实例结果；原始自动分割和原始图像保持不变。")
    return result


def label_tiff(result: Mapping[str, Any]) -> bytes:
    """Export native uint32 ROI labels and explicit original-image coordinates."""
    import tifffile

    payload = result.get("instance_mask")
    labels = decode_instance_mask(payload)
    metadata = {"application": "Figure Workbench", "kind": "instance_labels",
                "label_background": 0, "label_dtype": "uint32", "roi_origin_xy": payload["origin"],
                "source_shape_yx": payload["source_shape"], "mask_sha256": payload["sha256"],
                "method": result.get("method"), "instance_review": result.get("instance_review")}
    stream = io.BytesIO()
    tifffile.imwrite(stream, labels, photometric="minisblack", compression="deflate",
                     metadata=None, description=json.dumps(metadata, ensure_ascii=True, allow_nan=False))
    return stream.getvalue()
