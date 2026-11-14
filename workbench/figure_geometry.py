"""Display bounds in mm, using the same placement and fonts as native export.

The browser planner reserves conservative em-sized envelopes without loading
fonts. Preflight can measure the bundled fonts, so long narrow-letter titles do
not produce false clipping reports. Bounds do not crop or modify any source.
"""
from __future__ import annotations

from functools import lru_cache
import math
from fontTools.ttLib import TTLibError

from .exporting import FONT_FILES, MM_TO_PT, _font_key, _rotated_bounds


def _number(value, default=0):
    result = float(default if value is None else value)
    if not math.isfinite(result):
        raise ValueError('Display bounds require finite numbers.')
    return result


def union_bounds(*rectangles):
    """Return (x, y, width, height) enclosing nonempty physical rectangles."""
    rectangles = [rect for rect in rectangles if rect is not None]
    if not rectangles:
        return 0., 0., 0., 0.
    left = min(rect[0] for rect in rectangles)
    top = min(rect[1] for rect in rectangles)
    right = max(rect[0] + rect[2] for rect in rectangles)
    bottom = max(rect[1] + rect[3] for rect in rectangles)
    return left, top, right - left, bottom - top


@lru_cache(maxsize=3)
def _measurement_font(key):
    from fontTools.ttLib import TTFont
    font = TTFont(FONT_FILES[key], fontNumber=2 if key == 'cjk' else -1,
                  lazy=False)
    # Keep the font alive with the glyph set; both are read-only after loading.
    return font, font.getBestCmap(), font.getGlyphSet(), font['head'].unitsPerEm


@lru_cache(maxsize=8192)
def _glyph_metrics(key, codepoint):
    from fontTools.pens.boundsPen import BoundsPen
    _, cmap, glyphs, units = _measurement_font(key)
    glyph = glyphs[cmap[codepoint]]
    pen = BoundsPen(glyphs)
    glyph.draw(pen)
    bounds = tuple(value / units for value in pen.bounds) if pen.bounds else None
    return glyph.width / units, bounds


def text_bounds(text, x, baseline, font_size_pt, *, bold=False, anchor='start'):
    """Measure the ink of an export text primitive, including multiple lines.

    PDF/SVG export uses glyph advances without browser shaping, with 1.25 em
    line spacing. Missing font/glyphs keep a conservative estimate here; the
    separate preflight font checks report that inspection failure explicitly.
    """
    text = str(text or '')
    size_mm = _number(font_size_pt) / MM_TO_PT
    if size_mm <= 0:
        raise ValueError('Text size must be positive.')
    lines = text.splitlines() or ['']
    rectangles = []
    try:
        key = _font_key(text, bold)
        for row, line in enumerate(lines):
            metrics = [_glyph_metrics(key, ord(char)) for char in line]
            advance = sum(item[0] for item in metrics)
            offset = advance / 2 if anchor == 'middle' else advance if anchor == 'end' else 0
            cursor = -offset
            line_baseline = baseline + row * size_mm * 1.25
            for width, bounds in metrics:
                if bounds is not None:
                    left, bottom, right, top = bounds  # font coordinates are y-up
                    rectangles.append((x + (cursor + left) * size_mm,
                                       line_baseline - top * size_mm,
                                       (right - left) * size_mm,
                                       (top - bottom) * size_mm))
                cursor += width
    except (OSError, ValueError, KeyError, TTLibError):
        for row, line in enumerate(lines):
            width = sum(.6 if char.isspace() else 1.05 for char in line) * size_mm
            offset = width / 2 if anchor == 'middle' else width if anchor == 'end' else 0
            rectangles.append((x - offset, baseline + row * size_mm * 1.25 - size_mm * 1.15,
                               width, size_mm * 1.45))
    return union_bounds(*rectangles) if rectangles else (x, baseline, 0., 0.)


def panel_display_bounds(panel):
    """Separate bounds for rotated source content and upright panel captions."""
    x, y, width, height = (_number(panel.get(key)) for key in
                           ('x_mm', 'y_mm', 'width_mm', 'height_mm'))
    if min(width, height) <= 0:
        raise ValueError('Panel size must be positive.')
    rotation = _number(panel.get('rotation'))
    bounds = {'image': _rotated_bounds(x, y, width, height, rotation)}
    label = '' if panel.get('label_mode') == 'none' else str(panel.get('label') or '')
    if label:
        bounds['label'] = text_bounds(label, x, y - 1.8,
                                      _number(panel.get('label_font_size_pt'), 10), bold=True)
    title = str(panel.get('title') or '')
    if title:
        bounds['title'] = text_bounds(title, x + width / 2, y - 1.8,
                                      _number(panel.get('title_font_size_pt'), 8), anchor='middle')
    return bounds


def annotation_display_bounds(annotation):
    """Include overflowing text, line endpoints and physical stroke width."""
    x, y, width, height = (_number(annotation.get(key)) for key in
                           ('x_mm', 'y_mm', 'width_mm', 'height_mm'))
    box = min(x, x + width), min(y, y + height), abs(width), abs(height)
    if annotation.get('type') == 'text':
        size = _number(annotation.get('font_size_pt'), 8)
        ink = text_bounds(annotation.get('text'), x, y + size / MM_TO_PT, size,
                          bold=bool(annotation.get('bold')), anchor=annotation.get('anchor', 'start'))
        return union_bounds(box, ink)
    half_stroke = max(0, _number(annotation.get('line_width_pt'), .8)) / MM_TO_PT / 2
    return box[0] - half_stroke, box[1] - half_stroke, box[2] + 2 * half_stroke, box[3] + 2 * half_stroke
