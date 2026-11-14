"""Embedded font conversion must keep actual glyphs and reject substitutions."""
import base64
import hashlib
from io import BytesIO

import fitz
from fontTools.pens.boundsPen import BoundsPen
from fontTools.ttLib import TTFont
import pytest
from reportlab.graphics import renderPDF
from svglib.svglib import svg2rlg

from workbench.exporting import _subset_font
from workbench.vector_fonts import cff_to_ttf, prepare_svg_fonts


def svg_text(family, text, font=None, mime="otf", weight="normal"):
    face = ""
    if font is not None:
        face = "<style>@font-face{font-family:'" + family + "';src:url(data:font/" + mime + ";base64," + base64.b64encode(font).decode() + ")}</style>"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="260" height="70">{face}'
            f'<text x="10" y="40" font-family="{family}" font-weight="{weight}" font-size="24">{text}</text></svg>').encode()


def test_embedded_cjk_cff_is_visible_and_retains_unicode_widths_and_outlines():
    text = "定量分析 / β-actin"
    original_font = _subset_font("cjk", text)
    original_sha = hashlib.sha256(original_font).hexdigest()
    derived_font = cff_to_ttf(original_font)
    source = TTFont(BytesIO(original_font))
    converted = TTFont(BytesIO(derived_font))
    try:
        assert "glyf" in converted and "CFF " not in converted
        assert source["head"].unitsPerEm == converted["head"].unitsPerEm
        first_map, second_map = source.getBestCmap(), converted.getBestCmap()
        first_glyphs, second_glyphs = source.getGlyphSet(), converted.getGlyphSet()
        for character in set(text) - {" "}:
            first_name, second_name = first_map[ord(character)], second_map[ord(character)]
            assert source["hmtx"][first_name] == converted["hmtx"][second_name]
            first_bounds, second_bounds = BoundsPen(first_glyphs), BoundsPen(second_glyphs)
            first_glyphs[first_name].draw(first_bounds)
            second_glyphs[second_name].draw(second_bounds)
            assert first_bounds.bounds is not None and second_bounds.bounds is not None
            assert second_bounds.bounds == pytest.approx(first_bounds.bounds, abs=2)
    finally:
        source.close()
        converted.close()
    original_svg = svg_text("FW_cjk", text, original_font)
    svg_sha = hashlib.sha256(original_svg).hexdigest()
    prepared, mapping = prepare_svg_fonts(original_svg)
    drawing = svg2rlg(BytesIO(prepared), font_map=mapping)
    pdf = renderPDF.drawToString(drawing)
    assert mapping.embedded[0]["sha256"] == original_sha
    assert mapping.embedded[0]["cff_to_ttf"]
    assert hashlib.sha256(original_font).hexdigest() == original_sha
    assert hashlib.sha256(original_svg).hexdigest() == svg_sha
    with fitz.open(stream=pdf, filetype="pdf") as document:
        assert text in document[0].get_text()
        assert any(font[1] == "ttf" for font in document[0].get_fonts())
        assert min(document[0].get_pixmap().samples) == 0


@pytest.mark.parametrize("family", ["sans-serif", "Helvetica", "DejaVu Sans"])
def test_reproducible_default_family_uses_bundled_font(family):
    prepared, mapping = prepare_svg_fonts(svg_text(family, "Measured β-actin", weight="bold"))
    registered, exact = mapping.find_font(family, "bold")
    assert exact and registered.startswith("FWFont_")
    drawing = svg2rlg(BytesIO(prepared), font_map=mapping)
    assert renderPDF.drawToString(drawing).startswith(b"%PDF")


def test_unknown_explicit_font_and_unsupported_glyph_are_errors():
    with pytest.raises(ValueError, match="字体不可用"):
        prepare_svg_fonts(svg_text("NotInstalledOnAnyComputer", "sample"))
    with pytest.raises(ValueError, match="字体缺少字符"):
        prepare_svg_fonts(svg_text("sans-serif", "中文"))
    with pytest.raises(ValueError, match="字体不可用"):
        prepare_svg_fonts(svg_text("NotInstalledOnAnyComputer,sans-serif", "sample"))


def test_same_embedded_family_name_with_different_bytes_is_rejected():
    first = _subset_font("regular", "sample")
    second = _subset_font("bold", "sample")
    left = svg_text("FW_regular", "sample", first, "ttf")
    other_style = ("<style>@font-face{font-family:FW_regular;src:url(data:font/ttf;base64," + base64.b64encode(second).decode() + ")}</style>").encode()
    conflict = left.replace(b"</svg>", other_style + b"</svg>")
    with pytest.raises(ValueError, match="冲突"):
        prepare_svg_fonts(conflict)


def test_inherited_tspan_font_checks_the_correct_text_run():
    font = _subset_font("cjk", "中文")
    svg = svg_text("sans-serif", "English ", None).replace(b"</text>", b'<tspan font-family="FW_cjk">' + "中文".encode() + b'</tspan></text>')
    style = ("<style>@font-face{font-family:FW_cjk;src:url(data:font/otf;base64," + base64.b64encode(font).decode() + ")}</style>").encode()
    prepared, mapping = prepare_svg_fonts(svg.replace(b"</svg>", style + b"</svg>"))
    pdf = renderPDF.drawToString(svg2rlg(BytesIO(prepared), font_map=mapping))
    with fitz.open(stream=pdf, filetype="pdf") as document:
        assert "中文" in document[0].get_text()


def test_font_registry_budget_refuses_new_fonts_but_keeps_existing(monkeypatch):
    import workbench.vector_fonts as module
    known = svg_text("sans-serif", "known")
    module.prepare_svg_fonts(known)
    monkeypatch.setattr(module, "_MAX_REGISTERED_FONT_BYTES", 0)
    module.prepare_svg_fonts(known)
    never_registered = _subset_font("regular", "budget-boundary-ΩЖ")
    with pytest.raises(ValueError, match="64 MiB"):
        module.prepare_svg_fonts(svg_text("Limited", "budget-boundary", never_registered, "ttf"))


def test_external_font_urls_and_entities_are_not_resolved():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><style>@font-face{font-family:X;src:url(https://example.invalid/font.ttf)}</style><text font-family="X">text</text></svg>'
    with pytest.raises(ValueError, match="data:font"):
        prepare_svg_fonts(svg)
    with pytest.raises(ValueError, match="DTD"):
        prepare_svg_fonts(b'<!DOCTYPE svg><svg xmlns="http://www.w3.org/2000/svg"/>')
