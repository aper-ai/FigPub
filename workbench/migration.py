"""Read-only, bounded migration of GelFigure and ScieFig project containers.

This is a new parser for their documented-by-code save formats, not an adapter
that runs either application. All file resolution stays inside the uploaded
container. Legacy JSON is retained as inert text for review; numerical results
are deliberately not promoted to new, independently verified measurements.
"""
from __future__ import annotations

import base64
import binascii
import copy
from dataclasses import dataclass, field
import gzip
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any
import uuid
from zipfile import BadZipFile, ZipFile

from .storage import (
    ALLOWED_EXTENSIONS, MAX_ARCHIVE_BYTES, MAX_FILE_BYTES, MAX_PROJECT_BYTES,
    _validate_zip, inspect_source, new_project, validate_project,
)


MAX_GEL_JSON_BYTES = MAX_FILE_BYTES * 4 // 3 + MAX_PROJECT_BYTES
MAX_SNAPSHOT_BYTES = 6 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class MigrationSource:
    asset_id: str
    name: str
    data: bytes
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MigrationResult:
    project: dict[str, Any]
    sources: list[MigrationSource]
    warnings: list[str]


def _json(payload: bytes, description: str, limit: int = MAX_PROJECT_BYTES) -> dict:
    if len(payload) > limit:
        raise ValueError(f"{description} exceeds the JSON size limit.")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{description} contains a duplicate JSON key.")
            result[key] = value
        return result

    def invalid_number(value):
        raise ValueError(f"{description} contains a non-finite JSON number.")

    try:
        result = json.loads(payload.decode("utf-8-sig"), object_pairs_hook=unique_object,
                            parse_constant=invalid_number)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"{description} is not valid UTF-8 JSON.") from exc
    if not isinstance(result, dict):
        raise ValueError(f"{description} must be a JSON object.")
    pending = [(result, 0)]
    nodes = 0
    while pending:
        value, depth = pending.pop()
        nodes += 1
        if depth > 64 or nodes > 2_000_000:
            raise ValueError(f"{description} is too deeply nested or complex.")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{description} contains a non-finite number.")
        if isinstance(value, dict):
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)
    return result


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _expected_hash(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("A legacy source SHA-256 must be a string.")
    value = value.removeprefix("sha256:")
    if not _SHA256.fullmatch(value):
        raise ValueError("A legacy source SHA-256 is malformed.")
    return value.lower()


def _check_hash(actual: str, expected: Any, description: str) -> bool:
    expected = _expected_hash(expected)
    if expected is None:
        return False
    if actual != expected:
        raise ValueError(f"SHA-256 hash mismatch: {description}.")
    return True


def _canonical_hash(payload: dict) -> str:
    return _digest(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), allow_nan=False).encode("utf-8"))


def _archive_path(value: Any) -> str:
    """Validate an archive member, never an operating-system path."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("Unsafe legacy archive member path.")
    path = PurePosixPath(value)
    if (path.is_absolute() or "\\" in value or ":" in value or "\0" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ValueError("Unsafe legacy archive member path.")
    return value


def _basename(value: Any) -> str:
    # Old projects can contain Windows absolute paths. Only their final name is
    # a hint for matching an already validated, embedded member.
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "_", name)
    if not name or name in {".", ".."}:
        return "source"
    if len(name) > 240:
        suffix = Path(name).suffix[:20]
        name = name[:220] + suffix
    return name


def _number(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Legacy geometry must contain finite numbers.")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError("Legacy geometry is outside the numeric range.") from exc
    if not math.isfinite(number):
        raise ValueError("Legacy geometry must contain finite numbers.")
    return number


def _object(value: Any, description: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{description} must be a JSON object.")
    return value


def _records(value: Any, description: str, maximum: int = 5000) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"Invalid {description} list.")
    return value


class _Builder:
    def __init__(self, title: str, origin: str, original_digest: str):
        self.project = new_project(title)
        self.origin = origin
        self.warnings: list[str] = []
        self.sources: dict[str, MigrationSource] = {}
        self.by_path: dict[str, str] = {}
        self.assets: dict[str, dict] = {}
        self.project["settings"]["migration"] = {
            "format": origin, "original_project_sha256": original_digest,
            "measurement_status": "legacy_results_require_reanalysis",
        }

    def warn(self, text: str):
        if text not in self.warnings:
            if len(self.warnings) < 300:
                self.warnings.append(text)
            elif len(self.warnings) == 300:
                self.warnings.append("其他迁移提示过多，已截断；请检查保留的旧工程状态。")

    def add_source(self, payload: bytes, filename: str, archive_name: str | None = None) -> str | None:
        name = _basename(filename)
        if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
            self.warn(f"原件 {name} 的格式暂不支持迁移，未导入其二进制内容；请在原程序另存为支持的图像或表格格式。")
            return None
        if len(payload) > MAX_FILE_BYTES:
            raise ValueError("An embedded original exceeds the 128 MB source limit.")
        identity = _digest(payload)
        if identity not in self.sources:
            if len(self.sources) >= 256:
                raise ValueError("A migrated project cannot contain more than 256 distinct originals.")
            # The temporary basename is ours. No legacy path is opened or
            # extracted. The same core source validator is used by normal upload.
            with tempfile.TemporaryDirectory(prefix="figure-migrate-") as directory:
                temporary_path = Path(directory) / ("source" + Path(name).suffix.lower())
                temporary_path.write_bytes(payload)
                try:
                    details = inspect_source(temporary_path, name)
                except (OSError, RuntimeError, EOFError, BadZipFile, SyntaxError,
                        OverflowError, KeyError, IndexError, TypeError) as exc:
                    raise ValueError(f"Cannot decode embedded source: {name}.") from exc
            legacy = {"format": self.origin, "archive_names": []}
            metadata = {**details.get("metadata", {}), "legacy": legacy}
            asset = {**details, "id": identity, "sha256": identity, "name": name,
                     "metadata": metadata, "preview_url": f"/api/assets/{identity}/preview"}
            self.sources[identity] = MigrationSource(identity, name, payload, metadata)
            self.assets[identity] = asset
            self.project["assets"].append(asset)
            if details.get("frames", 1) > 1:
                self.warn(f"原件 {name} 包含多页/多帧，全部原始字节已保留；迁移子图默认显示第 1 帧，请复核原工程的帧选择。")
        if archive_name:
            self.by_path[archive_name] = identity
            names = self.assets[identity]["metadata"]["legacy"]["archive_names"]
            if archive_name not in names:
                names.append(archive_name)
        return identity

    def resolve(self, record: dict, extra_paths: tuple[str, ...] = ()) -> str | None:
        expected = next((_expected_hash(record[key]) for key in
                         ("image_hash", "source_hash", "raw_hash", "original_hash")
                         if record.get(key)), None)
        paths = list(extra_paths)
        for key in ("embedded_image_path", "embedded_copy_path", "raw_copy_path"):
            if record.get(key):
                paths.append(_archive_path(record[key]))
        # Historical local paths are inert hints. They never reach Path.open.
        names = [_basename(record[key]) for key in
                 ("original_source", "source_name", "raw_source", "image_path", "source_path", "original_path")
                 if record.get(key)]
        for path in paths:
            if path in self.by_path:
                identity = self.by_path[path]
                if expected and identity != expected:
                    raise ValueError(f"SHA-256 hash mismatch: {_basename(path)}.")
                return identity
        if expected and expected in self.sources:
            return expected
        matches = {identity for path, identity in self.by_path.items() if _basename(path) in names}
        if len(matches) == 1:
            identity = matches.pop()
            if expected and identity != expected:
                raise ValueError("Embedded source does not match its legacy record SHA-256.")
            return identity
        description = ", ".join(names[:2]) or "无文件名"
        self.warn(f"无法唯一恢复旧素材引用（{description}）；未读取任何外部路径，该子图需重新关联原件。")
        return None

    def snapshot(self, payload: dict):
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) <= MAX_SNAPSHOT_BYTES:
            self.project["settings"]["migration"]["legacy_snapshot_json"] = encoded
        else:
            self.warn("旧工程状态超过 6 MB；未将完整状态嵌入新工程，请保留原工程文件用于核对。")

    def crop(self, identity: str, record: dict | None, description: str) -> dict | None:
        if not record:
            return None
        if not isinstance(record, dict):
            raise ValueError("Legacy crop must be an object.")
        asset = self.assets[identity]
        crop = {"x": _number(record.get("x")), "y": _number(record.get("y")),
                "width": _number(record.get("width", record.get("w")), asset["width_px"]),
                "height": _number(record.get("height", record.get("h")), asset["height_px"])}
        if (crop["x"] < 0 or crop["y"] < 0 or crop["width"] <= 0 or crop["height"] <= 0
                or crop["x"] + crop["width"] > asset["width_px"] + 1e-6
                or crop["y"] + crop["height"] > asset["height_px"] + 1e-6):
            self.warn(f"{description} 的旧裁切超出原图；已恢复完整原图，请重新确认裁切。")
            return None
        return crop

    def panel(self, identity: str, title: str = "", crop: dict | None = None) -> dict:
        asset = self.assets[identity]
        width = (crop or {}).get("width", asset["width_px"]) or 1
        height = (crop or {}).get("height", asset["height_px"]) or 1
        return {"id": str(uuid.uuid4()), "asset_id": identity, "title": str(title)[:240],
                "x_mm": 10.0, "y_mm": 12.0, "width_mm": 60.0,
                "height_mm": min(600.0, 60.0 * height / width), "label": "", "crop": crop,
                "rotation": 0.0, "flip_h": False, "flip_v": False,
                "display": {"low": None, "high": None, "gamma": 1.0, "invert": False},
                "gel": {"lane_centers": [], "lane_names": [], "marker_points": [],
                        "marker_unit": "kDa", "hidden_lanes": []}}

    def finish(self) -> MigrationResult:
        if not self.sources:
            self.warn("旧工程中没有可迁移的已嵌入原件；可查看保留状态并重新导入原图。")
        self.project["settings"]["migration_warnings"] = list(self.warnings)
        self.project["audit"].append({"at": self.project["created_at"], "action": "migrate_legacy",
                                      "detail": f"Imported {self.origin}; {len(self.sources)} originals; legacy results require reanalysis"})
        validate_project(self.project)
        return MigrationResult(self.project, list(self.sources.values()), list(self.warnings))


def _annotation(kind: str, x: float, y: float, text: str = "", **fields) -> dict:
    return {"id": str(uuid.uuid4()), "type": kind, "x_mm": x, "y_mm": y,
            "width_mm": 0.0, "height_mm": 0.0, "text": text,
            "font_size_pt": 8.0, "color": "#222222", **fields}


def _migrate_gelfigure(data: bytes, filename: str) -> MigrationResult:
    if data.startswith(b"\x1f\x8b"):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as handle:
                decoded = handle.read(MAX_GEL_JSON_BYTES + 1)
        except (OSError, EOFError) as exc:
            raise ValueError("Invalid gzip GelFigure project.") from exc
    else:
        decoded = data
    old = _json(decoded, "GelFigure project", MAX_GEL_JSON_BYTES)
    if old.get("gelfigProject") != 1:
        raise ValueError("Unsupported GelFigure project version; expected gelfigProject 1.")
    source = old.get("source")
    state = old.get("state")
    if not isinstance(source, dict) or not isinstance(state, dict):
        raise ValueError("GelFigure source or state is missing.")
    encoded = source.get("bytes")
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_FILE_BYTES + 2) // 3):
        raise ValueError("GelFigure embedded source is missing or oversized.")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("GelFigure source is not valid base64.") from exc
    checked = _check_hash(_digest(payload), source.get("sha256"), "GelFigure original")
    builder = _Builder(Path(_basename(filename)).stem, "gelfig", _digest(data))
    if not checked:
        builder.warn("旧 GelFigure 文件未提供原件 SHA-256；已为导入原件建立新哈希，无法验证旧记录。")
    identity = builder.add_source(payload, source.get("name", "source.tif"), "source/" + _basename(source.get("name")))
    builder.snapshot({key: value for key, value in old.items() if key != "source"} | {
        "source": {key: value for key, value in source.items() if key != "bytes"}})
    if identity is None:
        return builder.finish()
    asset = builder.assets[identity]
    orientation = state.get("orient", "")
    is_identity = orientation in ("", 0, None) or orientation == {"r": 0, "f": 0}
    rotation = _number(state.get("rotation"))
    safe_coordinates = is_identity and rotation == 0
    crop = builder.crop(identity, state.get("crop"), "GelFigure") if safe_coordinates else None
    panel = builder.panel(identity, crop=crop)
    panel["legacy"] = {"format": "gelfig", "coordinates_mapped": safe_coordinates}
    levels = _object(state.get("levels"), "GelFigure levels")
    if asset["dtype"].startswith("uint") or asset["dtype"] in {"bool", "boolean"}:
        full_scale = 2 ** asset["bit_depth"] - 1
        gamma = _number(levels.get("gamma"), 1.0)
        if .1 <= gamma <= 5:
            panel["display"] = {"low": _number(levels.get("black")) * full_scale,
                                "high": _number(levels.get("white"), 1.0) * full_scale,
                                "gamma": gamma, "invert": bool(levels.get("invert", False))}
        else:
            builder.warn("旧 gamma 超出新显示范围；已保留旧参数并恢复默认显示。")
    else:
        builder.warn("该源的强度范围无法与 GelFigure 归一化色阶可靠对应；已恢复默认显示，原始像素未变。")
    style = _object(state.get("style"), "GelFigure style")
    lanes = _records(state.get("lanes"), "GelFigure lane")
    visible_count = sum(not lane.get("hidden") and (crop is None or crop["x"] <= _number(lane.get("x")) <= crop["x"] + crop["width"]) for lane in lanes)
    desired_width = _number(style.get("gelWidthMm"), 45.0)
    if style.get("autoWidth", True):
        desired_width = max(22.0, min(160.0, math.floor((visible_count or 8) * _number(style.get("mmPerLane"), 5.5) + .5)))
    if desired_width <= 0 or desired_width > 600:
        raise ValueError("GelFigure physical width is outside the migration range.")
    extent = crop or {"x": 0.0, "y": 0.0, "width": asset["width_px"], "height": asset["height_px"]}
    panel.update(x_mm=15.0, y_mm=18.0, width_mm=desired_width,
                 height_mm=desired_width * extent["height"] / extent["width"],
                 label=str(style.get("panelLetter", "")))
    if panel["height_mm"] > 800:
        factor = 800 / panel["height_mm"]
        panel["width_mm"] *= factor
        panel["height_mm"] = 800
        builder.warn("旧图长宽比过大，迁移时按比例缩至 800 mm 高；请复核目标物理尺寸。")
    builder.project["panels"].append(panel)
    builder.project["figure"] = {"width_mm": max(55.0, panel["width_mm"] + 45),
                                  "height_mm": max(55.0, panel["height_mm"] + 40), "background": "#ffffff"}
    if safe_coordinates:
        panel["gel"].update(lane_centers=[_number(lane.get("x")) for lane in lanes],
                             lane_names=[str(lane.get("name", "")) for lane in lanes],
                             hidden_lanes=[index for index, lane in enumerate(lanes) if lane.get("hidden")],
                             mode=state.get("gelType", "protein"))
        markers = _records(state.get("markers"), "GelFigure marker")
        units = {str(marker.get("unit", "auto")) for marker in markers}
        unit = next(iter(units)) if len(units) == 1 else "auto"
        unit = ("bp" if state.get("gelType") == "dna" else "kDa") if unit == "auto" else unit
        panel["gel"]["marker_unit"] = unit
        for marker in markers:
            for band in _records(marker.get("bands"), "GelFigure marker band"):
                if band.get("show", True) and not band.get("excluded", False):
                    if len(panel["gel"]["marker_points"]) >= 5000:
                        raise ValueError("Too many GelFigure marker annotations.")
                    size = _number(band.get("bp"))
                    panel["gel"]["marker_points"].append({"y": _number(band.get("y")),
                                                            "size": size / 1000 if unit == "kb" else size})
        if len(markers) > 1 or any(marker.get("side", "auto") == "left" for marker in markers):
            builder.warn("Marker 数值及原图纵坐标已恢复；新布局统一显示在图右侧，请复核多套 Marker 的位置。")
        if len(units) > 1:
            builder.warn("旧工程混用了 Marker 单位；已统一为基础单位并保留原始状态。")
        if panel["gel"]["hidden_lanes"]:
            builder.warn("已恢复隐藏泳道及可见拼接线；旧自由标注位于拼接前坐标，暂仅保留在旧状态中，请重新放置。")
        else:
            _gel_annotations(builder, panel, state, extent)
        if any(lane.get("nameRuns") or lane.get("nameStyle") for lane in lanes):
            builder.warn("泳道名称已恢复为可编辑纯文本，旧富文本样式保留在旧状态中。")
    else:
        builder.warn("GelFigure 在方向变换/纠偏后裁切，而新工程采用原图坐标；已恢复完整原图，旧裁切、泳道和标注坐标仅保留在旧状态中，请重新确认。")
    builder.project["settings"]["migration"]["source_count_note"] = "GelFigure embeds only the selected exposure."
    builder.warn(".gelfig 仅包含保存时选中的曝光原件；其他曝光需从原文件另行导入。")
    builder.warn("已恢复可对应的图像与标注；字体、自动边距和旧版排版细节需在新画布复核。")
    return builder.finish()


def _gel_annotations(builder: _Builder, panel: dict, state: dict, extent: dict):
    annotations = builder.project["annotations"]
    boxes = _records(state.get("boxes"), "GelFigure box")
    labels = _records(state.get("labels"), "GelFigure label")
    groups = _records(state.get("groups"), "GelFigure group")
    conditions = _records(state.get("conditions"), "GelFigure condition")
    lanes = state.get("lanes") or []
    if len(boxes) + len(labels) + 2*len(groups) + len(conditions)*(len(lanes) + 1) > 5000:
        raise ValueError("Too many GelFigure canvas annotations.")
    scale_x = panel["width_mm"] / extent["width"]
    scale_y = panel["height_mm"] / extent["height"]
    x_of = lambda value: panel["x_mm"] + (_number(value) - extent["x"]) * scale_x
    y_of = lambda value: panel["y_mm"] + (_number(value) - extent["y"]) * scale_y
    for box in boxes:
        annotations.append(_annotation("rect", x_of(box.get("x")), y_of(box.get("y")),
                                       width_mm=_number(box.get("w", box.get("width"))) * scale_x,
                                       height_mm=_number(box.get("h", box.get("height"))) * scale_y,
                                       color=str(box.get("color", "#222222"))))
    for label in labels:
        at = _number(label.get("at"), .5)
        position = label.get("pos", "below")
        x = panel["x_mm"] + at * panel["width_mm"] if position == "below" else panel["x_mm"] + (-12 if position == "left" else panel["width_mm"] + 3)
        y = panel["y_mm"] + panel["height_mm"] + 7 if position == "below" else panel["y_mm"] + at * panel["height_mm"]
        annotations.append(_annotation("text", x, y, str(label.get("text", "")),
                                       font_size_pt=_number(label.get("pt"), 7), color=str(label.get("color", "#222222"))))
    for group in groups:
        first, last = group.get("from"), group.get("to")
        if not isinstance(first, int) or not isinstance(last, int) or not 0 <= first <= last < len(lanes):
            builder.warn("一个旧泳道分组索引无效，已保留状态但未生成该分组标注。")
            continue
        left, right = x_of(lanes[first].get("x")), x_of(lanes[last].get("x"))
        annotations.append(_annotation("text", (left + right) / 2, panel["y_mm"] - 9,
                                       str(group.get("text", "")), anchor="middle", font_size_pt=_number(group.get("pt"), 7)))
        annotations.append(_annotation("line", left, panel["y_mm"] - 3, width_mm=right-left))
    for index, condition in enumerate(conditions):
        values = condition.get("values") or []
        if not isinstance(values, list):
            raise ValueError("GelFigure condition values must be a list.")
        for lane, value in zip(lanes, values):
            if not lane.get("hidden"):
                annotations.append(_annotation("text", x_of(lane.get("x")), panel["y_mm"] + panel["height_mm"] + 8 + index * 4,
                                               str(value), font_size_pt=_number(condition.get("pt"), 7), anchor="middle"))
        annotations.append(_annotation("text", 1, panel["y_mm"] + panel["height_mm"] + 8 + index*4,
                                       str(condition.get("name", "")), font_size_pt=_number(condition.get("pt"), 7)))
    if state.get("arrows") or state.get("brackets"):
        builder.warn("旧箭头和侧边括号的自动排版尚未转换为画布对象；全部参数保留在旧状态中。")
    if state.get("conditions"):
        builder.project["figure"]["height_mm"] += min(200, len(state["conditions"]) * 4)


def _resolve_manifest_name(name: Any, names: set[str]) -> str:
    name = _archive_path(name)
    if name in names:
        return name
    candidates = {candidate for candidate in names if candidate.endswith("/" + name)} if not name.startswith("data/") else set()
    if len(candidates) != 1:
        raise ValueError(f"Missing or ambiguous legacy manifest member: {_basename(name)}.")
    return candidates.pop()


def _migrate_sciefig(data: bytes, filename: str) -> MigrationResult:
    builder = _Builder(Path(_basename(filename)).stem, "sfig", _digest(data))
    try:
        archive = ZipFile(io.BytesIO(data))
    except BadZipFile as exc:
        raise ValueError("ScieFig project is not a valid ZIP container.") from exc
    with archive:
        members = _validate_zip(archive)
        names = set(members)
        if not {"manifest.json", "project.json"} <= names:
            raise ValueError("ScieFig manifest.json or project.json is missing.")
        manifest = _json(archive.read("manifest.json"), "ScieFig manifest")
        old = _json(archive.read("project.json"), "ScieFig project")
        if not _check_hash(_canonical_hash(old), manifest.get("project_json_hash"), "ScieFig project.json"):
            builder.warn("旧 .sfig 未声明 project.json 哈希；无法验证旧布局记录。")
        builder.project["title"] = str(old.get("title") or builder.project["title"])[:240]
        composers = {}
        composer_hashes = manifest.get("composer_json_hashes") or {}
        raw_hashes = manifest.get("raw_hashes") or {}
        if not isinstance(composer_hashes, dict) or not isinstance(raw_hashes, dict):
            raise ValueError("ScieFig manifest hashes must be objects.")
        for declared_name, expected in composer_hashes.items():
            name = _archive_path(declared_name)
            if name not in names:
                raise ValueError("Missing declared ScieFig Composer member.")
            composer = _json(archive.read(name), "ScieFig Composer")
            if not _check_hash(_canonical_hash(composer), expected, name):
                raise ValueError("A declared Composer hash is missing.")
            composers[name] = composer
        raw_members = {}
        for declared_name, expected in raw_hashes.items():
            name = _resolve_manifest_name(declared_name, names)
            if members[name].file_size > MAX_FILE_BYTES:
                raise ValueError("A legacy original exceeds the 128 MB source limit.")
            payload = archive.read(name)
            if not _check_hash(_digest(payload), expected, name):
                raise ValueError("A declared original hash is missing.")
            raw_members[name] = payload
        for name in sorted(names):
            if name.endswith("/") or not (name.startswith("data/") or name in raw_members):
                continue
            payload = raw_members.pop(name, None)
            if payload is None:
                if members[name].file_size > MAX_FILE_BYTES:
                    raise ValueError("A legacy original exceeds the 128 MB source limit.")
                payload = archive.read(name)
                builder.warn(f"旧清单未声明原件 {_basename(name)} 的哈希；已建立新哈希，无法核对旧记录。")
            builder.add_source(payload, name, name)
        for record in _records(old.get("composer_figures"), "ScieFig Composer index", 1000):
            if record.get("file"):
                name = _archive_path(record["file"])
                if name not in names:
                    raise ValueError("Referenced ScieFig Composer file is missing.")
                if name not in composers:
                    composers[name] = _json(archive.read(name), "ScieFig Composer")
                    builder.warn(f"旧清单未声明 Composer {_basename(name)} 的哈希；布局已读取但无法核对旧记录。")
        builder.snapshot({"manifest": manifest, "project": old, "composers": composers})
    by_panel = {}
    template_count = 0
    for old_panel in _records(old.get("panels"), "ScieFig panel", 1000):
        old_id = str(old_panel.get("panel_uuid", ""))
        if not old_id or old_id in by_panel:
            raise ValueError("ScieFig panels must have distinct panel_uuid identities.")
        by_panel[old_id] = _sciefig_panel(builder, old_panel)
        template_count += len(by_panel[old_id])
        if template_count > 1000:
            raise ValueError("Too many migrated image or strip panels.")
    active = old.get("active_figure_uid")
    selected = next((composer for composer in composers.values() if composer.get("figure_uid") == active), None)
    if selected is None and composers:
        selected = next(iter(composers.values()))
        if active:
            builder.warn("旧活动图稿未找到，已迁移第一个 Composer 图稿。")
    if len(composers) > 1:
        builder.warn("旧工程包含多个 Composer 图稿：本次恢复活动图稿，其他图稿保留在旧状态中，所有支持的原件均已导入。")
    if selected is None and old.get("composer_layout"):
        layout = _object(old["composer_layout"], "Legacy Composer layout")
        selected = {"canvas": layout, "panel_refs": [
            {**cell, "panel_uid": cell.get("panel_uuid"), "figure_label": cell.get("panel_letter", "")}
            for cell in _records(layout.get("cells"), "legacy Composer cell", 1000) if cell.get("panel_uuid")], "annotations": []}
    if selected:
        _place_composer(builder, selected, by_panel)
    else:
        _place_default(builder, [panel for panels in by_panel.values() for panel in panels])
    builder.warn("旧版计算结果与分析参数保留为历史状态；新测量表保持为空，请使用原始数据重新运行分析。")
    return builder.finish()


def _migrate_channel_view(builder: _Builder, identity: str, image: dict) -> dict | None:
    """Map only auditable scalar sources; preserve ambiguous old effects as history."""
    from .channels import validate_view
    records = _records(image.get('channels'), 'ScieFig channels', 4096)
    if not records:
        return None
    interpretation = _object(image.get('channel_interpretation'), 'Channel interpretation').get('mode')
    if interpretation in {'rgb_composite', 'auto_single_signal'}:
        if interpretation == 'auto_single_signal':
            builder.warn('旧 RGB 单信号使用合成强度投影；已恢复原色原件，未把某个 RGB 分量冒充旧合成强度。显示与分析设置需复核。')
        return None
    if len(records) > 32:
        builder.warn('旧图超过 32 个显示通道，全部原件已保留；本次迁移前 32 个，请重新选择需要组合的通道。')
        records = records[:32]
    asset = builder.assets[identity]
    series = (asset.get('metadata', {}).get('series') or [{}])[0]
    scalar_step = int(series.get('samples', 1)) if int(series.get('logical_channels', 1)) > 1 else 1
    mapped, seen = [], set()
    for ordinal, old in enumerate(records):
        index = old.get('index', ordinal)
        if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index in seen:
            builder.warn('旧通道索引重复或无效，相关显示配置未转换；原件及旧配置均保留。')
            continue
        seen.add(index)
        black = _number(old.get('display_min'), 0)
        white = _number(old.get('display_max'), 1)
        processing = _object(old.get('display_processing'), 'Channel display processing')
        if processing.get('enabled', True):
            span = white - black
            low = black + _number(processing.get('black_point'), 0) * span
            high = black + _number(processing.get('white_point'), 1) * span
            black, white = low, high
            if any(processing.get(key) for key in ('denoise', 'strength', 'background_radius')):
                builder.warn('旧通道的降噪、非线性增强或显示去背景效果已保留为历史参数；新显示恢复可审计色阶，需重新确认处理。')
        source_index = index * scalar_step
        color = str(old.get('pseudocolor') or '#FFFFFF')
        if not re.fullmatch(r'#[0-9a-fA-F]{6}', color) or white <= black:
            builder.warn('旧通道颜色或范围无效，该通道恢复默认白色与自动范围。')
            color, black, white = '#FFFFFF', None, None
        mapped.append({'id': f'c{index}', 'name': str(old.get('name') or f'Channel {index + 1}')[:120],
                       'color': color, 'black': black, 'white': white, 'gamma': 1,
                       'visible': bool(old.get('visible', True)),
                       'source': {'asset_id': identity, 'series': 0, 'frame': 0, 'channel': source_index}})
    if not mapped:
        return None
    mode = str(image.get('view_mode') or 'merge')
    if mode not in {'merge', 'single', 'split'}:
        mode = 'merge'
    if mode == 'split' and not any(channel['visible'] for channel in mapped):
        mode = 'merge'
        builder.warn('旧分屏没有可见通道，已保留显隐状态并改为黑色合并预览。')
    active = next((channel['id'] for channel in mapped if channel['visible']), mapped[0]['id'])
    view = {'version': 1, 'mode': mode, 'active_channel': active,
            'split': {'columns': 0, 'gap_px': 8, 'include_merge': True}, 'channels': mapped}
    try:
        view = validate_view(view, builder.assets)['view']
    except ValueError:
        builder.warn('旧通道与当前原件的实际通道轴不能可靠对应；已保留原件与历史显示配置，需重新配置。')
        return None
    if mode == 'single':
        builder.warn('旧单通道画布的临时选中项未随工程保存；恢复首个可见通道，请复核选择。')
    return view


def _sciefig_panel(builder: _Builder, record: dict) -> list[dict]:
    kind = str(record.get("panel_type", ""))
    data = _object(record.get("data"), "ScieFig panel data")
    old_id = str(record["panel_uuid"])
    title = str(record.get("panel_id") or kind)
    result = []
    if kind == "western_blot":
        strips = _records(data.get("band_strips"), "WB strip", 1000)
        if not strips:
            strips = [{**data, "crop_roi": crop.get("roi"), "protein_name": crop.get("protein", ""),
                       "image_path": crop.get("source_image") or data.get("image_path")}
                      for crop in _records(data.get("crops"), "WB crop", 1000)] or [data]
        for strip in strips:
            identity = builder.resolve(strip)
            if not identity:
                continue
            if builder.assets[identity]["kind"] == "table":
                builder.warn("一个 WB 子图指向表格，已保留原件但未生成图像子图。")
                continue
            panel = builder.panel(identity, strip.get("protein_name", title),
                                  builder.crop(identity, strip.get("crop_roi"), "WB"))
            adjustments = _object(strip.get("adjustments"), "WB adjustments")
            panel["flip_h"] = bool(adjustments.get("flip_horizontal", False))
            panel["flip_v"] = bool(adjustments.get("flip_vertical", False))
            if _number(adjustments.get("rotation")):
                builder.warn("旧 WB 旋转会扩展裁切画布，新画布旋转的边界规则不同；已恢复原始裁切与翻转，旋转参数保留待复核。")
            if adjustments.get("brightness") or adjustments.get("contrast"):
                builder.warn("旧 WB 亮度/对比度作用于 8-bit 显示；已保留参数并恢复原始位深显示，请重新确认色阶。")
            if data.get("annotations") or data.get("wb_layout"):
                builder.warn("WB 条带原件和裁切已恢复；旧分组、泳道文字及复杂条带布局保留为历史状态，请在新画布复核。")
            result.append(panel)
    elif kind in {"fluorescence", "ihc", "brightfield"}:
        records = _records(data.get(kind + "_images"), f"{kind} image", 1000)
        for index, image in enumerate(records):
            safe_uid = re.sub(r"[^A-Za-z0-9_-]", "_", str(image.get("uid", "")).strip()) or "record"
            name = _basename(image.get("source_name") or image.get("source_path"))
            identity = builder.resolve(image, (f"data/{kind}/{old_id}/{index:04d}_{safe_uid}/{name}",
                                                f"data/{kind}/{old_id}/{name}"))
            if not identity or builder.assets[identity]["kind"] == "table":
                continue
            panel = builder.panel(identity, name)
            scale = _object(image.get("measurement_scale_bar"), "Microscopy calibration")
            if scale:
                panel["calibration"] = {key: value for key, value in scale.items()
                                        if key in {"um_per_px", "um_per_px_x", "um_per_px_y", "calibration_source"}}
            scale_bar = _object(image.get("scale_bar"), "Microscopy scale bar")
            um_per_pixel = scale_bar.get("manual_um_per_px") or scale.get("um_per_px_x") or scale.get("um_per_px")
            if scale_bar.get("enabled", scale_bar.get("visible", False)) and um_per_pixel and scale_bar.get("length_um"):
                panel["scale_bar"] = {"um_per_px": _number(um_per_pixel), "length_um": _number(scale_bar["length_um"]),
                                      "color": str(scale_bar.get("color", "#ffffff"))}
                builder.warn("显微图比例尺长度与标定已恢复；显示位置采用新画布右下角，请核对。")
            if image.get('channels') and kind == 'fluorescence':
                view = _migrate_channel_view(builder, identity, image)
                if view:
                    panel['channel_view'] = view
                    builder.project['settings'].setdefault('asset_edits', {}).setdefault(identity, {})['channel_view'] = copy.deepcopy(view)
                    if view['mode'] == 'split':
                        from .channels import tile_layout
                        layout = tile_layout(view, builder.assets[identity]['width_px'], builder.assets[identity]['height_px'])
                        panel['height_mm'] = min(600.0, panel['width_mm'] * layout['height_px'] / layout['width_px'])
            if image.get('thresholds') or image.get('measurements'):
                builder.warn('旧显微阈值与测量结果保留在历史记录中；新原始像素分析需重新运行，不自动确认旧结果。')
            result.append(panel)
    elif kind == "external_image":
        identity = builder.resolve(data)
        if identity and builder.assets[identity]["kind"] != "table":
            result.append(builder.panel(identity, title))
    else:
        if kind in {"qpcr", "derived_plot"}:
            builder.warn(f"{title} 的旧统计图未转为新的计算结果；已导入可用原始表格，需重新分析并生成图表。")
        else:
            builder.warn(f"旧子图类型 {kind or '未知'} 尚无布局映射；已保留支持的原件和旧状态。")
    for panel in result:
        panel["legacy"] = {"format": "sfig", "panel_uuid": old_id, "panel_type": kind}
    return result


def _place_default(builder: _Builder, panels: list[dict]):
    if len(panels) > 1000:
        raise ValueError("Too many migrated panels.")
    for index, panel in enumerate(panels):
        fit_scale = min(45/panel["width_mm"], 32/panel["height_mm"])
        panel.update(x_mm=10 + (index % 3)*55, y_mm=12 + (index // 3)*45,
                     width_mm=panel["width_mm"]*fit_scale, height_mm=panel["height_mm"]*fit_scale)
        builder.project["panels"].append(panel)
    height = max(120, 20 + math.ceil(len(panels)/3)*45)
    if height > 2000:
        builder.warn("未排版子图过多，部分位于画布范围外；原件均已保留，请分拆图稿。")
    builder.project["figure"]["height_mm"] = min(2000, height)
    if panels:
        builder.warn("旧工程没有 Composer 布局，已将可恢复子图放入默认网格；请重新排版。")


def _place_composer(builder: _Builder, composer: dict, by_panel: dict[str, list[dict]]):
    canvas = _object(composer.get("canvas"), "Composer canvas")
    builder.project["figure"] = {"width_mm": _number(canvas.get("width_mm"), 183),
                                  "height_mm": _number(canvas.get("height_mm"), 230),
                                  "background": str(canvas.get("background_color", "#ffffff"))}
    for reference in sorted(_records(composer.get("panel_refs"), "Composer panel reference", 1000),
                            key=lambda item: _number(item.get("layer_index"))):
        templates = by_panel.get(str(reference.get("panel_uid")), [])
        if not templates:
            builder.warn("一个 Composer 引用无法恢复为子图；相关原件与旧状态已保留。")
            continue
        if len(builder.project["panels"]) + len(templates) > 1000:
            raise ValueError("Too many composed migration panels.")
        height = _number(reference.get("height_mm"), 30)
        for index, template in enumerate(templates):
            panel = copy.deepcopy(template)
            panel["id"] = str(uuid.uuid4())
            panel.update(x_mm=_number(reference.get("x_mm")),
                         y_mm=_number(reference.get("y_mm")) + index*height/len(templates),
                         width_mm=_number(reference.get("width_mm"), 40), height_mm=height/len(templates),
                         label=str(reference.get("figure_label", "")) if index == 0 and reference.get("label_visible", True) else "",
                         hidden=not reference.get("visible", True), locked=bool(reference.get("locked", False)))
            builder.project["panels"].append(panel)
        if len(templates) > 1:
            builder.warn("包含多张图像/条带的旧子图已拆为独立可编辑子图，并均分旧引用区域；复杂组合布局需复核。")
    for record in sorted(_records(composer.get("annotations"), "Composer annotation"),
                         key=lambda item: _number(item.get("layer_index"))):
        properties = _object(record.get("properties"), "Composer annotation properties")
        kind = record.get("type")
        shape = properties.get("shape_type", "rectangle")
        if kind == "shape":
            kind = {"rectangle": "rect", "rect": "rect", "line": "line"}.get(shape)
        if kind not in {"text", "line", "rect"}:
            builder.warn(f"Composer 标注 {shape} 暂无编辑映射，参数已保留在旧状态中。")
            continue
        x, y = _number(record.get("x_mm")), _number(record.get("y_mm"))
        width, height = _number(properties.get("width_mm"), 55), _number(properties.get("height_mm"), 20)
        if kind == "line":
            x1 = _number(properties.get("x1"))
            y1 = _number(properties.get("y1"), height/2)
            x += x1
            y += y1
            width = _number(properties.get("x2"), width) - x1
            height = _number(properties.get("y2"), height/2) - y1
        annotation = _annotation(kind, x, y, str(properties.get("content", "")), width_mm=width, height_mm=height,
                                 font_size_pt=_number(properties.get("font_size_pt"), 8),
                                 color=str(properties.get("color" if kind == "text" else "stroke_color", "#222222")),
                                 line_width_pt=_number(properties.get("stroke_width_pt"), 1),
                                 hidden=not record.get("visible", True), locked=bool(record.get("locked", False)),
                                 bold=bool(properties.get("font_bold", False)))
        builder.project["annotations"].append(annotation)
        if properties.get("rotation_deg") or properties.get("text") or properties.get("font_italic"):
            builder.warn("Composer 标注的富文本、斜体或旋转样式尚未逐项恢复；可编辑文字与毫米位置已恢复。")
        if kind == "rect" and properties.get("fill_color") not in (None, "transparent", "none"):
            builder.warn("旧矩形填充样式保留在历史状态；新矩形恢复为轮廓，请复核。")
    if composer.get("legend_text"):
        builder.project["settings"]["figure_legend"] = str(composer["legend_text"])
    builder.warn("Composer 子图位置、尺寸及基础标注已按毫米恢复；跨图层叠放和旧字体排版需复核。")


def migrate_legacy(data: bytes, filename: str) -> MigrationResult:
    """Return a validated new document and immutable originals, without writes.

    Only short-lived validator files are created; this function never changes
    the active project, extracts archive paths, or follows external references.
    The caller can commit the result transactionally after all checks succeed.
    """
    if not isinstance(data, bytes) or not data or len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("Legacy project is empty or exceeds the upload size limit.")
    suffix = Path(_basename(filename)).suffix.lower()
    try:
        if suffix == ".gelfig":
            return _migrate_gelfigure(data, filename)
        if suffix == ".sfig":
            return _migrate_sciefig(data, filename)
    except (BadZipFile, RuntimeError, EOFError, OverflowError, RecursionError) as exc:
        raise ValueError("Legacy project contains damaged or unsupported encoded data.") from exc
    raise ValueError("Expected a .gelfig or .sfig project.")
