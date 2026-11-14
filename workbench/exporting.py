"""One physical-unit scene, multiple publication outputs.

Source pixels are cropped once at native resolution. PDF source pages remain
vector objects. Application text uses embedded, subsetted fonts, including CJK.
PPTX text/lines/rectangles remain native; imported plots/images remain pictures.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Callable, Any
import base64
import copy
import html
import json
import math
import re
import xml.etree.ElementTree as ET

import fitz
from PIL import Image

from .imaging import load_image_array, crop_display_image, png_bytes, retained_segments

MM_TO_PT = 72 / 25.4
RESOURCE_ROOT = Path(__file__).resolve().parents[1] / "resources/fonts"
FONT_FILES = {
    "regular": RESOURCE_ROOT / "DejaVuSans.ttf",
    "bold": RESOURCE_ROOT / "DejaVuSans-Bold.ttf",
    "cjk": RESOURCE_ROOT / "NotoSansCJK-Regular.ttc",
}
MAX_RASTER_PIXELS = 100_000_000


@dataclass(slots=True)
class Scene:
    width_mm: float
    height_mm: float
    background: str
    primitives: list[dict[str, Any]] = field(default_factory=list)
    title: str = "Figure"


def _color(value: str | None) -> tuple[float, float, float]:
    value = str(value or "#222222")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("颜色需要使用 #RRGGBB 格式")
    return tuple(int(value[index:index + 2], 16) / 255 for index in (1, 3, 5))


def _rotate_point(x: float, y: float, center: tuple[float, float], degrees: float):
    angle = math.radians(degrees)
    delta_x, delta_y = x - center[0], y - center[1]
    return (center[0] + math.cos(angle) * delta_x - math.sin(angle) * delta_y,
            center[1] + math.sin(angle) * delta_x + math.cos(angle) * delta_y)


def _rotated_bounds(x, y, width, height, degrees):
    center = x + width / 2, y + height / 2
    corners = [_rotate_point(px, py, center, degrees) for px, py in
               [(x, y), (x + width, y), (x, y + height), (x + width, y + height)]]
    xs, ys = zip(*corners)
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


def _map_source_x(panel, source_x, source_width, source_height):
    for rectangle, offset, fraction in retained_segments(panel, source_width, source_height):
        if rectangle.x <= source_x <= rectangle.x + rectangle.width:
            relative = offset + (source_x - rectangle.x) / rectangle.width * fraction
            return 1 - relative if panel.get("flip_h") else relative
    return None


def build_scene(project: dict, resolver: Callable[[str], Path]) -> Scene:
    figure = project.get("figure", {})
    scene = Scene(float(figure.get("width_mm", 180)), float(figure.get("height_mm", 120)),
                  figure.get("background", "#ffffff"), title=str(project.get("title") or "Figure"))
    if not 1 <= scene.width_mm <= 2000 or not 1 <= scene.height_mm <= 2000:
        raise ValueError("图版物理尺寸必须为 1–2000 mm")
    assets = {asset["id"]: asset for asset in project.get("assets", [])}
    from .channels import FilePlaneReader, render_view, tile_layout, validate_view
    channel_reader = FilePlaneReader(project.get('assets', []), resolver)
    decoded: dict[tuple, Any] = {}
    for panel in project.get("panels", []):
        if panel.get("hidden"):
            continue
        asset = assets.get(panel.get("asset_id"))
        if not asset:
            raise ValueError(f"子图 {panel.get('label', '')} 缺少原始素材")
        source = Path(resolver(asset["id"]))
        if not source.exists():
            raise ValueError(f"原始素材丢失：{asset.get('name', asset['id'])}")
        x, y = float(panel["x_mm"]), float(panel["y_mm"])
        width, height = float(panel["width_mm"]), float(panel["height_mm"])
        rotation = float(panel.get("rotation", 0))
        if width <= 0 or height <= 0 or not all(math.isfinite(value) for value in (x, y, width, height, rotation)):
            raise ValueError("子图尺寸或位置无效")
        source_width = int(asset.get("width_px") or 1)
        source_height = int(asset.get("height_px") or 1)
        scale_tiles = None
        content_start = len(scene.primitives)
        if asset.get("kind") in {"pdf", "svg"}:
            scene.primitives.append({"type": "vector", "path": source, "kind": asset["kind"], "panel": panel,
                                     "asset": asset, "x": x, "y": y, "width": width, "height": height,
                                     "rotation": rotation})
        elif asset.get("kind") == "table":
            raise ValueError("数据表不能直接作为图像导出，请先生成图表")
        elif panel.get('channel_view'):
            validated = validate_view(panel['channel_view'], channel_reader)
            view = validated['view']
            source_width, source_height = validated['width_px'], validated['height_px']
            if view['mode'] == 'split':
                if (panel.get('gel') or {}).get('hidden_lanes'):
                    raise ValueError('分屏面板不能同时拼接隐藏泳道；请先拆为独立通道面板。')
                layout = tile_layout(view, source_width, source_height, crop=panel.get('crop'))
                scale_tiles = []
                for tile in layout['tiles']:
                    tile_view = {**view, 'mode': tile['mode']}
                    if tile['mode'] == 'single':
                        tile_view['active_channel'] = tile['channel_id']
                    image, _ = render_view(channel_reader, tile_view, crop=panel.get('crop'))
                    tile_x = tile['x_px'] / layout['width_px']
                    tile_y = tile['y_px'] / layout['height_px']
                    tile_w = tile['width_px'] / layout['width_px']
                    tile_h = tile['height_px'] / layout['height_px']
                    if panel.get('flip_h'):
                        image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                        tile_x = 1 - tile_x - tile_w
                    if panel.get('flip_v'):
                        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
                        tile_y = 1 - tile_y - tile_h
                    scene.primitives.append({'type': 'image', 'image': image, 'panel_id': panel['id'],
                                             'channel_tile_id': tile['id'], 'x': x + tile_x * width,
                                             'y': y + tile_y * height, 'width': tile_w * width,
                                             'height': tile_h * height})
                    if panel.get('show_channel_labels', True):
                        caption = str(tile.get('name') or ('Merge' if tile['mode'] == 'merge' else tile['channel_id'])).replace('\n', ' ')
                        font_size, padding = 7.0, 1.2
                        font_key = _font_key(caption, False)
                        font = fitz.Font(fontbuffer=_subset_font(font_key, ''.join(sorted(set(caption + '…')))))
                        available = max(0, tile_w * width - 2 * padding)
                        while len(caption) > 1 and font.text_length(caption, fontsize=font_size) / MM_TO_PT > available:
                            caption = caption[:-2] + '…'
                        text_width = font.text_length(caption, fontsize=font_size) / MM_TO_PT
                        scene.primitives.append({'type': 'rect', 'x': x + tile_x * width, 'y': y + tile_y * height,
                                                 'width': min(tile_w * width, text_width + 2 * padding),
                                                 'height': font_size / MM_TO_PT + 2 * padding,
                                                 'color': '#202723', 'fill': '#202723', 'line_width_pt': 0})
                        scene.primitives.append({'type': 'text', 'x': x + tile_x * width + padding,
                                                 'y': y + tile_y * height + padding + font_size / MM_TO_PT,
                                                 'text': caption, 'font_size_pt': font_size, 'color': '#ffffff'})
                    scale_tiles.append((x + tile_x * width, y + tile_y * height, tile_w * width,
                                        tile_h * height, tile['width_px']))
            else:
                segments = retained_segments(panel, source_width, source_height)
                for rectangle, offset, fraction in segments:
                    crop = {'x': rectangle.x, 'y': rectangle.y, 'width': rectangle.width, 'height': rectangle.height}
                    image, _ = render_view(channel_reader, view, crop=crop)
                    if panel.get('flip_h'):
                        image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                        offset = 1 - offset - fraction
                    if panel.get('flip_v'):
                        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
                    scene.primitives.append({'type': 'image', 'image': image, 'panel_id': panel['id'],
                                             'x': x + offset * width, 'y': y, 'width': fraction * width,
                                             'height': height})
                if len(segments) > 1:
                    for (_, left, size), (_, next_left, _) in zip(segments, segments[1:]):
                        gap = (left + size + next_left) / 2
                        gap_x = x + width * (1 - gap if panel.get('flip_h') else gap)
                        scene.primitives.append({'type': 'line', 'x': gap_x, 'y': y, 'x2': gap_x, 'y2': y + height,
                                                 'color': '#6b7280', 'line_width_pt': .45, 'dashed': True})
        else:
            frame = panel.get('frame') if panel.get('frame') is not None else panel.get('page', 0)
            key = asset["id"], int(frame), panel.get("channel"), int(panel.get('series', 0))
            if key not in decoded:
                decoded[key] = load_image_array(source, page=key[1], channel=key[2], series=key[3])
            array = decoded[key]
            source_height, source_width = array.shape[:2]
            segments = retained_segments(panel, source_width, source_height)
            for rectangle, offset, fraction in segments:
                slice_panel = copy.deepcopy(panel)
                slice_panel["crop"] = {"x": rectangle.x, "y": rectangle.y, "width": rectangle.width, "height": rectangle.height}
                image = crop_display_image(array, slice_panel)
                if panel.get("flip_h"):
                    offset = 1 - offset - fraction
                primitive = {"type": "image", "image": image, "x": x + offset * width, "y": y,
                             "width": width * fraction, "height": height, "rotation": rotation,
                             "rotation_center": (x + width / 2, y + height / 2), "panel_id": panel["id"]}
                scene.primitives.append(primitive)
            if len(segments) > 1:
                # Visible breaks are never an optional display-only decoration.
                for (_, left, size), (_, next_left, _) in zip(segments, segments[1:]):
                    gap = (left + size + next_left) / 2
                    gap_x = x + width * (1 - gap if panel.get("flip_h") else gap)
                    scene.primitives.append({"type": "line", "x": gap_x, "y": y, "x2": gap_x, "y2": y + height,
                                             "color": "#6b7280", "line_width_pt": .45, "dashed": True})
        label = '' if panel.get('label_mode') == 'none' else str(panel.get("label") or "")
        if label:
            scene.primitives.append({"type": "text", "x": x, "y": y - 1.8, "text": label,
                                     "font_size_pt": float(panel.get("label_font_size_pt", 10)), "bold": True, "color": "#202725", "upright": True})
        title = str(panel.get("title") or "")
        if title:
            scene.primitives.append({"type": "text", "x": x + width / 2, "y": y - 1.8,
                                     "text": title, "font_size_pt": float(panel.get("title_font_size_pt", 8)),
                                     "anchor": "middle", "color": "#222222", "upright": True})
        gel = panel.get("gel") or {}
        crop = panel.get("crop") or {"x": 0, "y": 0, "width": source_width, "height": source_height}
        for index, center_x in enumerate(gel.get("lane_centers", [])):
            if index in gel.get("hidden_lanes", []):
                continue
            relative_x = _map_source_x(panel, float(center_x), source_width, source_height)
            names = gel.get("lane_names", [])
            name = str(names[index]) if index < len(names) else str(index + 1)
            if relative_x is not None and name:
                scene.primitives.append({"type": "text", "x": x + relative_x * width, "y": y + height + 3,
                                         "text": name, "font_size_pt": float(gel.get("font_size_pt", 7)), "anchor": "middle", "color": "#333333"})
        for marker in gel.get("marker_points", []):
            relative_y = (float(marker["y"]) - float(crop.get("y", 0))) / float(crop.get("height", source_height))
            if panel.get("flip_v"):
                relative_y = 1 - relative_y
            if not 0 <= relative_y <= 1:
                continue
            position_y = y + relative_y * height
            scene.primitives.append({"type": "line", "x": x + width, "y": position_y, "x2": x + width + 1.2, "y2": position_y, "line_width_pt": .5, "color": "#333333"})
            scene.primitives.append({"type": "text", "x": x + width + 1.8, "y": position_y + .8,
                                     "text": str(marker.get("size", marker.get("value", ""))), "font_size_pt": 7, "color": "#333333"})
        if gel.get("marker_points"):
            scene.primitives.append({"type": "text", "x": x + width + 1.8, "y": y - 1.8,
                                     "text": str(gel.get("marker_unit", "kDa")), "font_size_pt": 7, "color": "#333333"})
        scale_bar = panel.get("scale_bar") or {}
        if scale_bar.get("length_um") and scale_bar.get("um_per_px"):
            for tile_x, tile_y, tile_w, tile_h, crop_w in scale_tiles or [(x, y, width, height, float(crop['width']))]:
                length_mm = float(scale_bar['length_um']) / float(scale_bar['um_per_px']) / crop_w * tile_w
                if not math.isfinite(length_mm) or length_mm <= 0 or length_mm > tile_w * .9:
                    raise ValueError('比例尺长度超出通道子图，请修改标定或长度')
                color = scale_bar.get('color', '#ffffff')
                margin = min(2, tile_w * .03, tile_h * .07)
                scene.primitives.append({'type': 'line', 'x': tile_x + tile_w - length_mm - margin, 'y': tile_y + tile_h - margin,
                                         'x2': tile_x + tile_w - margin, 'y2': tile_y + tile_h - margin,
                                         'line_width_pt': 1.5, 'color': color})
                scene.primitives.append({'type': 'text', 'x': tile_x + tile_w - length_mm / 2 - margin,
                                         'y': tile_y + tile_h - margin - 1.5,
                                         'text': f"{float(scale_bar['length_um']):g} µm", 'font_size_pt': 6.5,
                                         'anchor': 'middle', 'color': color})
        for primitive in scene.primitives[content_start:]:
            if not primitive.get("upright"):
                primitive["rotation"] = rotation
                primitive["rotation_center"] = (x + width / 2, y + height / 2)
    for annotation in project.get("annotations", []):
        if annotation.get("hidden"):
            continue
        primitive = {**annotation, "x": float(annotation.get("x_mm", 0)), "y": float(annotation.get("y_mm", 0)),
                     "width": float(annotation.get("width_mm", 10)), "height": float(annotation.get("height_mm", 5)),
                     "color": annotation.get("color", "#222222")}
        if primitive["type"] == "text":
            primitive["y"] += float(primitive.get("font_size_pt", 8)) / MM_TO_PT
        if primitive["type"] == "line":
            primitive["x2"] = primitive["x"] + primitive["width"]
            primitive["y2"] = primitive["y"] + primitive["height"]
        scene.primitives.append(primitive)
    return scene


@lru_cache(maxsize=3)
def font_character_map(key: str) -> frozenset[int]:
    from fontTools.ttLib import TTFont
    font = TTFont(FONT_FILES[key], fontNumber=2 if key == "cjk" else -1, lazy=True)
    result = frozenset(font.getBestCmap())
    font.close()
    return result


def _font_key(text, bold=False):
    characters = {ord(character) for character in text if not character.isspace()}
    primary = "bold" if bold else "regular"
    if characters <= font_character_map(primary):
        return primary
    if characters <= font_character_map("cjk"):
        return "cjk"
    missing = characters - font_character_map(primary) - font_character_map("cjk")
    if missing:
        raise ValueError("字体无法表示字符：" + " ".join(chr(code) for code in sorted(missing)))
    # Mixed-script uncommon glyphs require separate font runs, not silent tofu.
    raise ValueError("此文本含无法由同一字体覆盖的字符，请拆分为不同文字对象")


@lru_cache(maxsize=24)
def _subset_font(key: str, text: str) -> bytes:
    from fontTools import subset
    from fontTools.ttLib import TTFont
    font = TTFont(FONT_FILES[key], fontNumber=2 if key == "cjk" else -1)
    options = subset.Options()
    options.notdef_glyph = True
    options.notdef_outline = True
    options.recommended_glyphs = True
    options.drop_tables += ["FFTM"]
    # CID-keyed CFF fonts keep character-to-glyph identities in MuPDF's PDF
    # embedding path. Renumbering them produces extractable but invisible text.
    options.retain_gids = key == "cjk"
    subsetter = subset.Subsetter(options=options)
    subsetter.populate(text=text)
    subsetter.subset(font)
    buffer = BytesIO()
    font.save(buffer)
    font.close()
    return buffer.getvalue()


def _font_resources(scene):
    texts: dict[str, str] = {}
    for primitive in scene.primitives:
        if primitive["type"] == "text":
            text = str(primitive.get("text", ""))
            key = _font_key(text, primitive.get("bold", False))
            texts[key] = texts.get(key, "") + text
            primitive["font_key"] = key
    return {key: _subset_font(key, "".join(sorted(set(text)))) for key, text in texts.items()}


def _source_document(primitive):
    if primitive["kind"] == "pdf":
        document = fitz.open(primitive["path"])
        page_index = int(primitive["panel"].get("page", 0))
        if not 0 <= page_index < len(document):
            document.close()
            raise ValueError("PDF 页码超出范围")
        return document, page_index
    from .vectors import open_svg
    return open_svg(primitive["path"].read_bytes()), 0


def _clip_rect(primitive, page):
    crop = primitive["panel"].get("crop")
    if not crop:
        return page.rect
    source_width = max(1, float(primitive["asset"].get("width_px", page.rect.width)))
    source_height = max(1, float(primitive["asset"].get("height_px", page.rect.height)))
    return fitz.Rect(float(crop["x"]) / source_width * page.rect.width,
                     float(crop["y"]) / source_height * page.rect.height,
                     (float(crop["x"]) + float(crop["width"])) / source_width * page.rect.width,
                     (float(crop["y"]) + float(crop["height"])) / source_height * page.rect.height) & page.rect


def _scene_point(primitive, x, y):
    """Apply panel rotation in y-down physical coordinates, without mirroring text."""
    if not primitive.get("rotation"):
        return x, y
    return _rotate_point(x, y, primitive["rotation_center"], primitive["rotation"])


def _place_pdf(page, source, page_index, rectangle, primitive, clip=None):
    """Place a PDF form, then apply one affine matrix; source vectors stay vectors.

    show_pdf_page creates a small wrapper Form XObject. Altering that wrapper's
    documented PDF Matrix avoids rasterization and avoids rotate-then-stretch
    distortion. Only newly created top-level forms are touched.
    """
    before = {item[0] for item in page.get_xobjects()}
    page.show_pdf_page(rectangle, source, page_index, clip=clip, keep_proportion=False)
    panel = primitive.get("panel", {}) if primitive["type"] == "vector" else {}
    angle = float(primitive.get("rotation", 0))
    flip_x, flip_y = -1 if panel.get("flip_h") else 1, -1 if panel.get("flip_v") else 1
    if not angle and flip_x == flip_y == 1:
        return
    wrappers = [item for item in page.get_xobjects() if item[0] not in before and item[2] == 0]
    if len(wrappers) != 1:
        raise RuntimeError("无法确定矢量变换对象")
    xref = wrappers[0][0]
    kind, value = page.parent.xref_get_key(xref, "Matrix")
    if kind != "array":
        raise RuntimeError("PDF 矢量变换矩阵缺失")
    numbers = [float(number) for number in re.findall(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", value)]
    base_matrix = fitz.Matrix(*numbers)
    # Reflection is about this panel fragment; rotation may share a wider group.
    center = (rectangle.tl + rectangle.br) / 2
    center_pdf = center * ~page.transformation_matrix
    transform = fitz.Matrix(1, 0, 0, 1, -center_pdf.x, -center_pdf.y)
    transform *= fitz.Matrix(flip_x, flip_y)
    transform *= fitz.Matrix(1, 0, 0, 1, center_pdf.x, center_pdf.y)
    rotation_center = primitive.get("rotation_center", (center.x / MM_TO_PT, center.y / MM_TO_PT))
    origin = fitz.Point(*(value * MM_TO_PT for value in rotation_center)) * ~page.transformation_matrix
    transform *= fitz.Matrix(1, 0, 0, 1, -origin.x, -origin.y)
    transform *= fitz.Matrix(-angle)
    transform *= fitz.Matrix(1, 0, 0, 1, origin.x, origin.y)
    composed = base_matrix * transform
    page.parent.xref_set_key(xref, "Matrix", "[" + " ".join(f"{value:.10g}" for value in composed) + "]")


def _pdf_bytes(scene: Scene, outlined_text: bool = False) -> bytes:
    document = fitz.open()
    page = document.new_page(width=scene.width_mm * MM_TO_PT, height=scene.height_mm * MM_TO_PT)
    page.draw_rect(page.rect, color=None, fill=_color(scene.background))
    fonts = _font_resources(scene)
    font_objects = {}
    for key, buffer in fonts.items():
        page.insert_font(fontname=f"FW_{key}", fontbuffer=buffer)
        font_objects[key] = fitz.Font(fontbuffer=buffer)
    for primitive in scene.primitives:
        kind = primitive["type"]
        x, y = primitive["x"] * MM_TO_PT, primitive["y"] * MM_TO_PT
        if kind in {"image", "vector"}:
            width, height = primitive["width"] * MM_TO_PT, primitive["height"] * MM_TO_PT
            angle = float(primitive.get("rotation", 0))
            rectangle = fitz.Rect(x, y, x + width, y + height)
            if kind == "image":
                image = primitive["image"]
                if angle % 360:
                    source = fitz.open()
                    image_page = source.new_page(width=width, height=height)
                    image_page.insert_image(image_page.rect, stream=png_bytes(image), keep_proportion=False)
                    try:
                        _place_pdf(page, source, 0, rectangle, primitive)
                    finally:
                        source.close()
                else:
                    page.insert_image(rectangle, stream=png_bytes(image), keep_proportion=False)
            else:
                source, page_index = _source_document(primitive)
                try:
                    source_page = source[page_index]
                    clip = _clip_rect(primitive, source_page)
                    if clip.is_empty:
                        raise ValueError("PDF/SVG 裁切区域为空")
                    _place_pdf(page, source, page_index, rectangle, primitive, clip)
                finally:
                    source.close()
        elif kind == "text":
            font_key = primitive.get("font_key", "regular")
            size = float(primitive.get("font_size_pt", 8))
            text = str(primitive.get("text", ""))
            if size <= 0:
                raise ValueError("文字字号必须大于零")
            for line_index, line in enumerate(text.splitlines() or [""]):
                length = font_objects[font_key].text_length(line, fontsize=size)
                anchor = primitive.get("anchor", "start")
                offset = length / 2 if anchor == "middle" else length if anchor == "end" else 0
                position = _scene_point(primitive, (x - offset) / MM_TO_PT, (y + line_index * size * 1.25) / MM_TO_PT)
                point = fitz.Point(*(value * MM_TO_PT for value in position))
                morph = (point, fitz.Matrix(-float(primitive["rotation"]))) if primitive.get("rotation") else None
                page.insert_text(point, line, fontname=f"FW_{font_key}",
                                 fontsize=size, color=_color(primitive.get("color")), morph=morph)
        elif kind == "line":
            start = _scene_point(primitive, primitive["x"], primitive["y"])
            end = _scene_point(primitive, primitive["x2"], primitive["y2"])
            page.draw_line(tuple(value * MM_TO_PT for value in start), tuple(value * MM_TO_PT for value in end),
                           color=_color(primitive.get("color")), width=float(primitive.get("line_width_pt", .8)),
                           dashes="[2 2] 0" if primitive.get("dashed") else None)
        elif kind == "rect":
            px, py = primitive['x'], primitive['y']
            points = [_scene_point(primitive, vx, vy) for vx, vy in
                      [(px, py), (px + primitive['width'], py),
                       (px + primitive['width'], py + primitive['height']), (px, py + primitive['height'])]]
            fill = primitive.get("fill")
            page.draw_polyline([tuple(value * MM_TO_PT for value in point) for point in points],
                               closePath=True, color=_color(primitive.get('color')),
                               fill=_color(fill) if fill and fill != 'transparent' else None,
                               width=float(primitive.get('line_width_pt', .8)))
    document.set_metadata({"title": scene.title, "creator": "Figure Workbench", "producer": "Figure Workbench / PyMuPDF"})
    if outlined_text:
        svg_root = ET.fromstring(page.get_svg_image(text_as_path=True))
        # MuPDF emits unitless SVG width/height in PDF points. SVG unitless
        # lengths are CSS pixels, so specify physical units before conversion.
        svg_root.set('width', f'{scene.width_mm:g}mm')
        svg_root.set('height', f'{scene.height_mm:g}mm')
        svg = ET.tostring(svg_root, encoding='utf-8')
        # MuPDF writes valid clip paths, but its own SVG reader ignores them.
        # The shared vector converter keeps crops intact when outlining text.
        from .vectors import svg_to_pdf
        result = svg_to_pdf(svg)
    else:
        result = document.tobytes(garbage=4, deflate=True)
    document.close()
    return result


def _svg_bytes(scene: Scene, outlined_text=False) -> bytes:
    if outlined_text:
        document = fitz.open(stream=_pdf_bytes(scene), filetype="pdf")
        svg_root = ET.fromstring(document[0].get_svg_image(text_as_path=True))
        svg_root.set('width', f'{scene.width_mm:g}mm')
        svg_root.set('height', f'{scene.height_mm:g}mm')
        svg = ET.tostring(svg_root, encoding='utf-8')
        document.close()
        return svg
    fonts = _font_resources(scene)
    width, height = scene.width_mm * MM_TO_PT, scene.height_mm * MM_TO_PT
    elements = [f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{scene.width_mm:g}mm" height="{scene.height_mm:g}mm" viewBox="0 0 {width:g} {height:g}">']
    elements.append('<metadata>' + html.escape(json.dumps({"application": "Figure Workbench", "units": "pt", "fonts_embedded": True})) + '</metadata>')
    rules = []
    for key, buffer in fonts.items():
        font_type = "opentype" if buffer[:4] == b"OTTO" else "truetype"
        rules.append(f"@font-face{{font-family:'FW_{key}';src:url(data:font/{'otf' if font_type == 'opentype' else 'ttf'};base64,{base64.b64encode(buffer).decode()}) format('{font_type}');}}")
    elements.append('<defs><style>' + ''.join(rules) + '</style></defs>')
    elements.append(f'<rect width="100%" height="100%" fill="{scene.background}"/>')
    for index, primitive in enumerate(scene.primitives):
        kind = primitive["type"]
        x, y = primitive["x"] * MM_TO_PT, primitive["y"] * MM_TO_PT
        color = primitive.get("color", "#222222")
        _color(color)
        rotation = primitive.get("rotation", 0)
        center = primitive.get("rotation_center", (primitive["x"], primitive["y"]))
        transform = f' transform="rotate({rotation:g} {center[0]*MM_TO_PT:g} {center[1]*MM_TO_PT:g})"' if rotation else ''
        if kind in {"image", "vector"}:
            panel_width, panel_height = primitive["width"] * MM_TO_PT, primitive["height"] * MM_TO_PT
            if kind == "image":
                payload = base64.b64encode(png_bytes(primitive["image"])).decode()
                elements.append(f'<image x="{x:g}" y="{y:g}" width="{panel_width:g}" height="{panel_height:g}" preserveAspectRatio="none" href="data:image/png;base64,{payload}"{transform}/>')
            else:
                source, page_index = _source_document(primitive)
                try:
                    page = source[page_index]
                    clip = _clip_rect(primitive, page)
                    root = ET.fromstring(page.get_svg_image(text_as_path=True))
                    # Prefix IDs and local references to prevent cross-panel collisions.
                    fragment = ''.join(ET.tostring(child, encoding="unicode") for child in root)
                    identifiers = re.findall(r'\bid="([^"]+)"', fragment)
                    for identifier in sorted(set(identifiers), key=len, reverse=True):
                        replacement = f"p{index}_{identifier}"
                        fragment = fragment.replace(f'id="{identifier}"', f'id="{replacement}"').replace(f'#{identifier}"', f'#{replacement}"').replace(f'#{identifier})', f'#{replacement})')
                    panel = primitive["panel"]
                    flip_x, flip_y = -1 if panel.get("flip_h") else 1, -1 if panel.get("flip_v") else 1
                    cx, cy = x + panel_width / 2, y + panel_height / 2
                    reflection = f'translate({cx:g} {cy:g}) scale({flip_x} {flip_y}) translate({-cx:g} {-cy:g})'
                    # Explicit user-space clips work in standards-compliant SVG
                    # viewers and in our shared SVG→PDF converter. MuPDF's SVG
                    # reader ignores clipPath entirely and must not be used.
                    placement = f'translate({x:g} {y:g}) scale({panel_width/clip.width:g} {panel_height/clip.height:g}) translate({-clip.x0:g} {-clip.y0:g})'
                    clip_id = f'fw-panel-clip-{index}'
                    elements.append(f'<defs><clipPath id="{clip_id}" clipPathUnits="userSpaceOnUse"><rect x="{x:g}" y="{y:g}" width="{panel_width:g}" height="{panel_height:g}"/></clipPath></defs>')
                    elements.append(f'<g{transform}><g transform="{reflection}"><g clip-path="url(#{clip_id})"><g transform="{placement}">{fragment}</g></g></g></g>')
                finally:
                    source.close()
        elif kind == "text":
            size = float(primitive.get("font_size_pt", 8))
            text = str(primitive.get("text", ""))
            anchor = primitive.get("anchor", "start")
            key = primitive.get("font_key", "regular")
            lines = ''.join(f'<tspan x="{x:g}" dy="{0 if number == 0 else size*1.25:g}">{html.escape(line)}</tspan>' for number, line in enumerate(text.splitlines() or [""]))
            elements.append(f'<text x="{x:g}" y="{y:g}" fill="{color}" font-family="FW_{key}" font-size="{size:g}" text-anchor="{anchor}"{transform}>{lines}</text>')
        elif kind == "line":
            dash = ' stroke-dasharray="2 2"' if primitive.get("dashed") else ''
            elements.append(f'<line x1="{x:g}" y1="{y:g}" x2="{primitive["x2"]*MM_TO_PT:g}" y2="{primitive["y2"]*MM_TO_PT:g}" stroke="{color}" stroke-width="{primitive.get("line_width_pt",.8):g}"{dash}{transform}/>')
        elif kind == "rect":
            fill = primitive.get("fill", "none")
            if fill == "transparent":
                fill = "none"
            elements.append(f'<rect x="{x:g}" y="{y:g}" width="{primitive["width"]*MM_TO_PT:g}" height="{primitive["height"]*MM_TO_PT:g}" fill="{fill}" stroke="{color}" stroke-width="{primitive.get("line_width_pt",.8):g}"{transform}/>')
    elements.append('</svg>')
    return '\n'.join(elements).encode()


def _pptx_bytes(scene: Scene) -> bytes:
    if not (25.4 <= scene.width_mm <= 1422.4 and 25.4 <= scene.height_mm <= 1422.4):
        raise ValueError('PPTX 幻灯片的宽度和高度均须为 25.4–1422.4 mm。请调整版面尺寸，或使用 PDF / SVG 保留当前物理尺寸。')
    from pptx import Presentation
    from pptx.util import Mm, Pt
    from pptx.enum.shapes import MSO_SHAPE, MSO_CONNECTOR
    from pptx.enum.dml import MSO_LINE_DASH_STYLE
    from pptx.enum.text import PP_ALIGN
    from pptx.dml.color import RGBColor
    presentation = Presentation()
    presentation.slide_width = Mm(scene.width_mm)
    presentation.slide_height = Mm(scene.height_mm)
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = RGBColor.from_string(scene.background.lstrip('#'))
    for primitive in scene.primitives:
        kind = primitive['type']
        x, y = primitive['x'], primitive['y']
        color = RGBColor.from_string(primitive.get('color', '#222222').lstrip('#'))
        if kind in {'image', 'vector'}:
            if kind == 'image':
                image = primitive['image']
            else:
                source, page_index = _source_document(primitive)
                try:
                    page = source[page_index]
                    target_scale = min(4, max(1, primitive['width'] / 25.4 * 300 / max(page.rect.width, 1)))
                    raster = page.get_pixmap(matrix=fitz.Matrix(target_scale, target_scale), clip=_clip_rect(primitive, page), alpha=True)
                    image = Image.open(BytesIO(raster.tobytes('png'))).copy()
                    if primitive['panel'].get('flip_h'):
                        image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                    if primitive['panel'].get('flip_v'):
                        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
                finally:
                    source.close()
            center = _scene_point(primitive, x + primitive['width'] / 2, y + primitive['height'] / 2)
            picture = slide.shapes.add_picture(BytesIO(png_bytes(image)), Mm(center[0] - primitive['width']/2), Mm(center[1] - primitive['height']/2), width=Mm(primitive['width']), height=Mm(primitive['height']))
            picture.rotation = float(primitive.get('rotation', 0))
        elif kind == 'text':
            text = str(primitive.get('text', ''))
            size = float(primitive.get('font_size_pt', 8))
            key = _font_key(text, primitive.get('bold', False))
            font = fitz.Font(fontbuffer=_subset_font(key, ''.join(sorted(set(text)))))
            width = max(3, max((font.text_length(line, fontsize=size) for line in text.splitlines()), default=1) / MM_TO_PT + 2)
            height = max(3, size * 1.5 * max(1, len(text.splitlines())) / MM_TO_PT)
            anchor = primitive.get('anchor', 'start')
            left = x - (width / 2 if anchor == 'middle' else width if anchor == 'end' else 0)
            top = y - size / MM_TO_PT
            center = _scene_point(primitive, left + width / 2, top + height / 2)
            box = slide.shapes.add_textbox(Mm(center[0] - width / 2), Mm(center[1] - height / 2), Mm(width), Mm(height))
            box.rotation = float(primitive.get('rotation', 0))
            frame = box.text_frame
            frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = 0
            frame.word_wrap = False
            for number, line in enumerate(text.splitlines() or ['']):
                paragraph = frame.paragraphs[0] if number == 0 else frame.add_paragraph()
                paragraph.text = line
                paragraph.alignment = PP_ALIGN.CENTER if anchor == 'middle' else PP_ALIGN.RIGHT if anchor == 'end' else PP_ALIGN.LEFT
                paragraph.font.name = 'Noto Sans CJK SC' if key == 'cjk' else 'DejaVu Sans'
                paragraph.font.size = Pt(size)
                paragraph.font.bold = bool(primitive.get('bold'))
                paragraph.font.color.rgb = color
        elif kind == 'line':
            start = _scene_point(primitive, x, y)
            end = _scene_point(primitive, primitive['x2'], primitive['y2'])
            shape = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Mm(start[0]), Mm(start[1]), Mm(end[0]), Mm(end[1]))
            shape.line.color.rgb = color
            shape.line.width = Pt(float(primitive.get('line_width_pt', .8)))
            if primitive.get('dashed'):
                shape.line.dash_style = MSO_LINE_DASH_STYLE.DASH
        elif kind == 'rect':
            center = _scene_point(primitive, x + primitive['width'] / 2, y + primitive['height'] / 2)
            shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Mm(center[0] - primitive['width'] / 2),
                                          Mm(center[1] - primitive['height'] / 2), Mm(primitive['width']), Mm(primitive['height']))
            shape.rotation = float(primitive.get('rotation', 0))
            if primitive.get('fill') and primitive['fill'] not in {'transparent', 'none'}:
                shape.fill.solid()
                shape.fill.fore_color.rgb = RGBColor.from_string(primitive['fill'].lstrip('#'))
            else:
                shape.fill.background()
            shape.line.color.rgb = color
            shape.line.width = Pt(float(primitive.get('line_width_pt', .8)))
    presentation.core_properties.title = scene.title
    presentation.core_properties.author = ''
    presentation.core_properties.last_modified_by = ''
    presentation.core_properties.comments = 'Native application text/shapes. Source pixels preserved in the .figwork project. PPTX references font names; use the embedded-font PDF for portable appearance.'
    buffer = BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def export_project(project: dict, asset_resolver: Callable[[str], Path], format: str, dpi: int = 300,
                   outlined_text: bool = False) -> tuple[bytes, str, str]:
    output_format = str(format).lower().lstrip('.')
    if output_format not in {'pdf', 'svg', 'png', 'tif', 'tiff', 'pptx'}:
        raise ValueError('不支持的成图格式')
    if not 72 <= int(dpi) <= 1200:
        raise ValueError('导出 DPI 必须为 72–1200')
    scene = build_scene(project, asset_resolver)
    filename = re.sub(r'[^\w\-\u4e00-\u9fff]+', '_', scene.title).strip('_')[:80] or 'Figure'
    if output_format == 'pdf':
        return _pdf_bytes(scene, outlined_text), 'application/pdf', filename + '.pdf'
    if output_format == 'svg':
        return _svg_bytes(scene, outlined_text), 'image/svg+xml', filename + '.svg'
    if output_format == 'pptx':
        return _pptx_bytes(scene), 'application/vnd.openxmlformats-officedocument.presentationml.presentation', filename + '.pptx'
    pixel_count = (scene.width_mm / 25.4 * int(dpi)) * (scene.height_mm / 25.4 * int(dpi))
    if pixel_count > MAX_RASTER_PIXELS:
        raise ValueError('成图超过 1 亿像素，请降低 DPI 或缩小版面尺寸')
    document = fitz.open(stream=_pdf_bytes(scene), filetype='pdf')
    try:
        raster = document[0].get_pixmap(dpi=int(dpi), alpha=False)
        image = Image.frombytes('RGB', (raster.width, raster.height), raster.samples)
    finally:
        document.close()
    buffer = BytesIO()
    if output_format == 'png':
        from PIL.PngImagePlugin import PngInfo
        info = PngInfo()
        info.add_itxt('Software', 'Figure Workbench')
        info.add_itxt('SourceNote', 'Export DPI is output sampling. Original source resolution is evaluated per panel; no new scientific detail is generated.')
        image.save(buffer, 'PNG', dpi=(int(dpi), int(dpi)), pnginfo=info)
        return buffer.getvalue(), 'image/png', filename + '.png'
    image.save(buffer, 'TIFF', compression='tiff_lzw', dpi=(int(dpi), int(dpi)))
    return buffer.getvalue(), 'image/tiff', filename + '.tiff'
