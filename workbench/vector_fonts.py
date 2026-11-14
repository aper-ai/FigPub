"""Strict embedded-font preparation for the SVG -> ReportLab renderer.

The uploaded SVG/font bytes are immutable. CFF outlines are converted only for
the derived rendering path; cubic-to-quadratic approximation uses a 1/1000 em
tolerance, in addition to TrueType's integer-coordinate quantization. Unknown
fonts and missing Unicode glyphs are errors, never silent system fallbacks.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
from io import BytesIO
from pathlib import Path
import re
import threading
import xml.etree.ElementTree as ET

_REGISTRATION_LOCK = threading.RLock()
_REGISTERED: dict[str, "RegisteredFont"] = {}
_FONT_GLYPHS: dict[str, frozenset[int]] = {}
_FONT_ROOT = Path(__file__).resolve().parents[1] / "resources" / "fonts"
_MAX_FONT_BYTES = 32 * 1024 * 1024
_MAX_EMBEDDED_FONTS = 32
_MAX_REGISTERED_FONTS = 256
_MAX_REGISTERED_FONT_BYTES = 64 * 1024 * 1024
_REGISTERED_FONT_BYTES = 0
_SVG = "{http://www.w3.org/2000/svg}"


@dataclass(frozen=True)
class RegisteredFont:
    name: str
    digest: str
    codepoints: frozenset[int]
    converted_cff: bool


def _font_weight(value: str | int = "normal") -> str:
    value = str(value).strip().lower()
    if value in {"normal", "400", "regular", ""}:
        return "normal"
    if value in {"bold", "700"}:
        return "bold"
    return value


def _family(value: str) -> str:
    return str(value).strip().strip("\"'").strip().casefold()


def cff_to_ttf(font_data: bytes) -> bytes:
    """Convert static CFF OTF to a derived TrueType font, retaining Unicode/metrics.

    Already-TrueType input is returned unchanged. This function has no file or
    network side effects and never changes the supplied font bytes.
    """
    from fontTools import subset
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.cu2quPen import Cu2QuPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.ttLib import TTFont, TTLibError

    if not font_data or len(font_data) > _MAX_FONT_BYTES:
        raise ValueError("SVG 字体为空或超过 32 MiB。")
    if font_data[:4] not in {b"OTTO", b"\x00\x01\x00\x00", b"true"}:
        raise ValueError("SVG 字体数据不是未压缩的 TTF/OTF。")
    try:
        font = TTFont(BytesIO(font_data), recalcTimestamp=False)
    except (TTLibError, ValueError, OSError) as exc:
        raise ValueError("SVG 内嵌字体无法解析。") from exc
    try:
        if "glyf" in font:
            return font_data
        if "CFF " not in font or "CFF2" in font or "fvar" in font:
            raise ValueError("SVG 字体仅支持静态 TrueType 或 CFF OpenType。")
        characters = font.getBestCmap() or {}
        if not characters:
            raise ValueError("SVG 字体缺少 Unicode 字符映射。")
        # Our CJK export keeps original CIDs. Remove unreachable placeholders
        # before converting, without removing any mapped Unicode character.
        options = subset.Options()
        options.retain_gids = False
        options.notdef_glyph = True
        options.notdef_outline = True
        options.recommended_glyphs = True
        options.layout_features = []
        subsetter = subset.Subsetter(options=options)
        subsetter.populate(unicodes=characters.keys())
        subsetter.subset(font)
        glyph_order = font.getGlyphOrder()
        if len(glyph_order) > 65535:
            raise ValueError("SVG 字体字形数量超过 TrueType 上限。")
        units = int(font["head"].unitsPerEm)
        glyph_set = font.getGlyphSet()
        glyphs = {}
        for name in glyph_order:
            pen = TTGlyphPen(glyph_set)
            conversion = Cu2QuPen(pen, max_err=units / 1000.0, reverse_direction=True)
            glyph_set[name].draw(conversion)
            glyphs[name] = pen.glyph()
        builder = FontBuilder(units, isTTF=True)
        builder.setupGlyphOrder(glyph_order)
        builder.setupCharacterMap(font.getBestCmap())
        builder.setupGlyf(glyphs)
        builder.setupHorizontalMetrics({name: tuple(font["hmtx"][name]) for name in glyph_order})
        horizontal = font["hhea"]
        builder.setupHorizontalHeader(ascent=horizontal.ascent, descent=horizontal.descent,
                                      lineGap=horizontal.lineGap)
        identity = hashlib.sha256(font_data).hexdigest()[:24]
        name = "FWEmbedded-" + identity
        builder.setupNameTable({"familyName": name, "styleName": "Regular", "uniqueFontIdentifier": name,
                                "fullName": name, "psName": name, "version": "Version 1.0"})
        os2 = font.get("OS/2")
        builder.setupOS2(sTypoAscender=getattr(os2, "sTypoAscender", horizontal.ascent),
                         sTypoDescender=getattr(os2, "sTypoDescender", horizontal.descent),
                         sTypoLineGap=getattr(os2, "sTypoLineGap", horizontal.lineGap),
                         usWinAscent=max(0, getattr(os2, "usWinAscent", horizontal.ascent)),
                         usWinDescent=max(0, getattr(os2, "usWinDescent", -horizontal.descent)))
        builder.setupPost(keepGlyphNames=True)
        builder.setupMaxp()
        output = BytesIO()
        builder.save(output)
        return output.getvalue()
    except (TTLibError, KeyError, OverflowError, AssertionError) as exc:
        raise ValueError("SVG CFF 字体无法安全转换为渲染字体。") from exc
    finally:
        font.close()


def _register(font_data: bytes) -> RegisteredFont:
    from fontTools.ttLib import TTFont, TTLibError
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont as ReportLabFont, TTFError

    global _REGISTERED_FONT_BYTES
    digest = hashlib.sha256(font_data).hexdigest()
    with _REGISTRATION_LOCK:
        if digest in _REGISTERED:
            return _REGISTERED[digest]
        if len(_REGISTERED) >= _MAX_REGISTERED_FONTS:
            raise ValueError("本次会话的 SVG 字体种类已达 256 个，请保存工程并重启应用后继续。")
        if len(font_data) > _MAX_FONT_BYTES:
            raise ValueError("SVG 内嵌字体超过 32 MiB。")
        try:
            source = TTFont(BytesIO(font_data), recalcTimestamp=False)
            try:
                codepoints = frozenset(source.getBestCmap() or {})
                if len(codepoints) > 100_000:
                    raise ValueError("SVG 字体字符映射数量超过支持上限。")
                is_cff = "CFF " in source
                if "fvar" in source:
                    raise ValueError("SVG 暂不支持可变字体，请先生成静态实例或将文字转路径。")
            finally:
                source.close()
            prepared = cff_to_ttf(font_data)
            if _REGISTERED_FONT_BYTES + len(prepared) > _MAX_REGISTERED_FONT_BYTES:
                raise ValueError("本次会话的 SVG 渲染字体已达 64 MiB，请保存工程并重启应用后继续。")
            name = "FWFont_" + digest
            pdfmetrics.registerFont(ReportLabFont(name, BytesIO(prepared)))
        except (TTLibError, TTFError, OSError, RuntimeError, KeyError) as exc:
            raise ValueError("SVG 内嵌字体注册失败。") from exc
        record = RegisteredFont(name, digest, codepoints, is_cff)
        _REGISTERED[digest] = record
        _REGISTERED_FONT_BYTES += len(prepared)
        _FONT_GLYPHS[name] = codepoints
        return record


def font_cmap_check(text: str, registered_name: str) -> None:
    """Raise if the exact registered font cannot represent displayed characters."""
    with _REGISTRATION_LOCK:
        codepoints = _FONT_GLYPHS.get(registered_name)
    if codepoints is None:
        raise ValueError("SVG 使用了未经验证的字体。")
    missing = {ord(character) for character in text if character not in "\n\r\t"} - codepoints
    if missing:
        examples = " ".join(f"{chr(code)} (U+{code:04X})" for code in sorted(missing)[:10])
        raise ValueError("SVG 字体缺少字符：" + examples)


class StrictFontMap:
    """svglib FontMap protocol without its fontconfig/file/default fallback."""

    def __init__(self):
        self.fonts: dict[tuple[str, str, str], RegisteredFont] = {}
        self.embedded: list[dict] = []
        self.default_substitutions = {"sans-serif": "DejaVu Sans", "Helvetica": "DejaVu Sans"}

    def add(self, family: str, record: RegisteredFont, weight="normal", style="normal"):
        key = (_family(family), _font_weight(weight), str(style).strip().lower() or "normal")
        previous = self.fonts.get(key)
        if previous and previous.digest != record.digest:
            raise ValueError(f"SVG 对字体 {family} 声明了冲突的字节内容。")
        self.fonts[key] = record

    def find_font(self, font_name: str, weight="normal", style="normal") -> tuple[str, bool]:
        key = (_family(font_name), _font_weight(weight), str(style).strip().lower() or "normal")
        if key not in self.fonts:
            raise ValueError(f"SVG 字体不可用：{font_name}，weight={weight}，style={style}；请嵌入字体或将文字转路径。")
        return self.fonts[key].name, True


def _first_family(value: str) -> str:
    # CSS fallback is deliberately disabled: a missing explicit first choice
    # must not silently change glyph widths or scientific labels.
    match = re.match(r"\s*(?:'([^']+)'|\"([^\"]+)\"|([^,]+))", str(value))
    return next((part for part in match.groups() if part is not None), "sans-serif").strip() if match else "sans-serif"


def _extract_font_faces(root: ET.Element, font_map: StrictFontMap):
    import tinycss2
    face_count = 0
    for style in (element for element in root.iter() if isinstance(element.tag, str) and element.tag.rsplit("}", 1)[-1] == "style"):
        rules = tinycss2.parse_stylesheet(style.text or "", skip_comments=True, skip_whitespace=True)
        retained = []
        for rule in rules:
            if rule.type != "at-rule" or rule.lower_at_keyword != "font-face":
                retained.append(rule)
                continue
            face_count += 1
            if face_count > _MAX_EMBEDDED_FONTS:
                raise ValueError("SVG 内嵌字体超过 32 个。")
            if rule.content is None:
                raise ValueError("SVG @font-face 声明缺少内容。")
            declarations = {item.lower_name: item for item in tinycss2.parse_declaration_list(rule.content, skip_comments=True, skip_whitespace=True)
                            if item.type == "declaration"}
            if "font-family" not in declarations or "src" not in declarations:
                raise ValueError("SVG @font-face 必须包含 font-family 和内嵌 src。")
            family = tinycss2.serialize(declarations["font-family"].value).strip().strip("\"'")
            sources = []
            for token in declarations["src"].value:
                if token.type == "url":
                    sources.append(token.value)
                elif token.type == "function" and token.lower_name == "url":
                    sources.append(tinycss2.serialize(token.arguments).strip().strip("\"'"))
            if len(sources) != 1:
                raise ValueError("SVG 字体 src 必须为一个内嵌 data 字体，不能依赖本机或网络字体。")
            matched = re.fullmatch(r"data:font/(ttf|otf);base64,([A-Za-z0-9+/=]+)", sources[0], flags=re.I)
            if not matched:
                raise ValueError("SVG 字体仅接受 data:font/ttf 或 data:font/otf 内嵌数据。")
            if len(matched.group(2)) > 4 * ((_MAX_FONT_BYTES + 2)//3):
                raise ValueError("SVG 内嵌字体超过 32 MiB。")
            try:
                payload = base64.b64decode(matched.group(2), validate=True)
            except binascii.Error as exc:
                raise ValueError("SVG 字体 base64 数据损坏。") from exc
            record = _register(payload)
            weight = tinycss2.serialize(declarations["font-weight"].value).strip() if "font-weight" in declarations else "normal"
            face_style = tinycss2.serialize(declarations["font-style"].value).strip() if "font-style" in declarations else "normal"
            font_map.add(family, record, weight, face_style)
            font_map.embedded.append({"family": family, "sha256": record.digest, "cff_to_ttf": record.converted_cff,
                                       "curve_tolerance_em": .001 if record.converted_cff else None})
        style.text = tinycss2.serialize(retained)


def _validate_text_fonts(root: ET.Element, font_map: StrictFontMap):
    def walk(element: ET.Element, inherited: dict[str, str], in_text: bool):
        properties = dict(inherited)
        for key in ("font-family", "font-weight", "font-style"):
            value = element.get(key)
            if value and value != "inherit":
                properties[key] = value
        tag = element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""
        is_text = in_text or tag in {"text", "tspan", "textPath"}
        name = None
        if is_text:
            name, _ = font_map.find_font(_first_family(properties["font-family"]),
                                         properties.get("font-weight", "normal"), properties.get("font-style", "normal"))
            if element.text:
                font_cmap_check(element.text, name)
        for child in element:
            walk(child, properties, is_text)
            if is_text and child.tail:
                font_cmap_check(child.tail, name)
    walk(root, {"font-family": "sans-serif", "font-weight": "normal", "font-style": "normal"}, False)


def prepare_svg_fonts(svg_bytes: bytes) -> tuple[bytes, StrictFontMap]:
    """Register safe embedded fonts and return derived SVG plus strict svglib map.

    The caller must first sanitize SVG and resolve CSS declarations onto element
    presentation attributes. This function handles @font-face, inherited text
    attributes, font registration and glyph validation; it does not fetch CSS.
    """
    if not isinstance(svg_bytes, bytes) or len(svg_bytes) > 128 * 1024 * 1024:
        raise ValueError("SVG 输入为空或过大。")
    if b"<!DOCTYPE" in svg_bytes.upper() or b"<!ENTITY" in svg_bytes.upper():
        raise ValueError("SVG 字体准备不接受 DTD 或实体。")
    try:
        root = ET.fromstring(svg_bytes)
    except ET.ParseError as exc:
        raise ValueError("SVG XML 无法解析。") from exc
    font_map = StrictFontMap()
    for weight, filename in (("normal", "DejaVuSans.ttf"), ("bold", "DejaVuSans-Bold.ttf")):
        try:
            record = _register((_FONT_ROOT / filename).read_bytes())
        except OSError as exc:
            raise ValueError("SVG 渲染所需的打包字体资源缺失。") from exc
        for family in ("DejaVu Sans", "sans-serif", "Helvetica"):
            font_map.add(family, record, weight)
    _extract_font_faces(root, font_map)
    if not root.get("font-family"):
        root.set("font-family", "sans-serif")
    _validate_text_fonts(root, font_map)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), font_map
