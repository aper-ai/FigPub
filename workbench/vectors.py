"""A single, explicit SVG rendering contract for preview, checks and export.

MuPDF's SVG reader ignores clipPath and masks. It remains our PDF reader, but
must never interpret SVG input: a seemingly successful conversion can expose
cropped source content. svglib creates vector ReportLab objects, and ReportLab
applies real PDF clipping paths. Unsupported SVG effects fail visibly instead
of being silently omitted. Original SVG bytes are never rewritten on disk.
"""
from __future__ import annotations

from collections import OrderedDict
from io import BytesIO
import base64
import copy
import hashlib
import logging
import math
import re
import threading
import xml.etree.ElementTree as ET

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
# svglib 2.4 resolves <defs> using the root's default namespace. Canonicalizing
# namespace spelling avoids losing valid prefixed definitions after XML edits.
ET.register_namespace('', SVG_NAMESPACE)
ET.register_namespace('xlink', 'http://www.w3.org/1999/xlink')
MAX_CACHE_BYTES = 32 * 1024 * 1024
MAX_ELEMENTS = 100_000
MAX_DEPTH = 128
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
_conversion_lock = threading.RLock()
_cache: OrderedDict[str, bytes] = OrderedDict()
_cache_size = 0


def _name(node) -> str:
    return node.tag.rsplit('}', 1)[-1]


def _unsupported(feature: str):
    raise ValueError(f'SVG 含当前矢量渲染器无法可靠处理的 {feature}；请在原绘图软件中导出 PDF，或将该效果转换为路径后再导入。')


def _declarations(tokens):
    import tinycss2
    result = []
    for item in tinycss2.parse_declaration_list(tokens, skip_whitespace=True, skip_comments=True):
        if item.type == 'error':
            raise ValueError('SVG CSS 声明无效。')
        if item.type != 'declaration':
            _unsupported('CSS 嵌套规则')
        result.append((item.lower_name, tinycss2.serialize(item.value).strip(), bool(item.important)))
    return result


def _materialize_css(root):
    """Make clip-path obey the same cascade as fill/font properties.

    svglib resolves clip paths before consulting its own CSS matcher. Applying
    the cascade to attributes first prevents style/class clips being dropped.
    Font-face definitions are retained for the strict embedded-font resolver.
    """
    import cssselect2
    import tinycss2
    matcher = cssselect2.Matcher()
    font_rules = []
    style_nodes = []
    for node in root.iter():
        if _name(node) != 'style':
            continue
        style_nodes.append(node)
        for rule in tinycss2.parse_stylesheet(''.join(node.itertext()), skip_whitespace=True, skip_comments=True):
            if rule.type == 'error':
                raise ValueError('SVG CSS 样式表无效。')
            if rule.type == 'at-rule':
                if rule.lower_at_keyword != 'font-face':
                    _unsupported('@' + rule.lower_at_keyword)
                font_rules.append(tinycss2.serialize([rule]))
                continue
            if rule.type != 'qualified-rule':
                _unsupported('CSS 规则')
            declarations = _declarations(rule.content)
            try:
                selectors = cssselect2.compile_selector_list(rule.prelude)
            except cssselect2.SelectorError as exc:
                raise ValueError('SVG CSS 选择器无效。') from exc
            for selector in selectors:
                if selector.pseudo_element is not None:
                    _unsupported('CSS 伪元素')
                matcher.add_selector(selector, declarations)
    for wrapped in cssselect2.ElementWrapper.from_xml_root(root).iter_subtree():
        node = wrapped.etree_element
        cascade = {}
        for specificity, order, pseudo, declarations in matcher.match(wrapped):
            for prop, value, important in declarations:
                priority = (important, 0, *specificity, order)
                if prop not in cascade or priority >= cascade[prop][0]:
                    cascade[prop] = priority, value
        inline = node.attrib.pop('style', '')
        if inline:
            for order, (prop, value, important) in enumerate(_declarations(inline)):
                priority = (important, 1, 0, 0, 0, order)
                if prop not in cascade or priority >= cascade[prop][0]:
                    cascade[prop] = priority, value
        for prop, (_, value) in cascade.items():
            node.set(prop, value)
    for node in style_nodes:
        node.clear()
    if font_rules:
        style = ET.SubElement(root, f'{{{SVG_NAMESPACE}}}style')
        style.text = '\n'.join(font_rules)
    # SVGs that leave the family unspecified use one reproducible bundled font.
    root.attrib.setdefault('font-family', 'sans-serif')


def _normalize_clips(root, identifiers):
    """Flatten clip group transforms; preserve the actual curved vector paths."""
    shapes = {'path', 'rect', 'circle', 'ellipse', 'polygon', 'polyline'}
    for clip in [node for node in root.iter() if _name(node) == 'clipPath']:
        if clip.get('clipPathUnits', 'userSpaceOnUse') != 'userSpaceOnUse':
            _unsupported('objectBoundingBox 裁切坐标')
        flattened = []

        def visit(node, transforms, rule, references=()):
            tag = _name(node)
            transform = node.get('transform', '')
            transforms = transforms + ([transform] if transform else [])
            rule = node.get('clip-rule', rule)
            if tag in shapes:
                shape = copy.deepcopy(node)
                shape.attrib.pop('id', None)
                shape.set('clip-rule', rule)
                if transforms:
                    shape.set('transform', ' '.join(transforms))
                flattened.append(shape)
            elif tag in {'g', 'clipPath'}:
                for child in node:
                    visit(child, transforms, rule, references)
            elif tag == 'use':
                target = node.get('href') or node.get('{http://www.w3.org/1999/xlink}href', '')
                if not target.startswith('#') or target[1:] not in identifiers:
                    raise ValueError('SVG 裁切路径引用不存在。')
                if target in references or len(references) > 32:
                    raise ValueError('SVG 裁切路径包含循环引用。')
                if node.get('x') or node.get('y'):
                    transforms += [f'translate({node.get("x", "0")} {node.get("y", "0")})']
                visit(identifiers[target[1:]], transforms, rule, references + (target,))
            elif tag not in {'title', 'desc'}:
                _unsupported('裁切路径中的 ' + tag)
        visit(clip, [], clip.get('clip-rule', 'nonzero'))
        if not flattened:
            # An empty clip means nothing is visible, not an absent clip.
            _unsupported('空裁切路径')
        clip.attrib.pop('transform', None)
        clip[:] = flattened


def _prepare_svg(data: bytes) -> bytes:
    from .storage import safe_svg
    from PIL import Image
    root = ET.fromstring(safe_svg(data))
    nodes = list(root.iter())
    if len(nodes) > MAX_ELEMENTS:
        raise ValueError('SVG 元素超过 100000 个，无法安全渲染。')
    depths = [(root, 0)]
    while depths:
        node, depth = depths.pop()
        if depth > MAX_DEPTH:
            raise ValueError('SVG 嵌套层级过深。')
        depths.extend((child, depth + 1) for child in node)
    _materialize_css(root)
    allowed = {'svg', 'g', 'defs', 'style', 'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon',
               'text', 'tspan', 'use', 'image', 'clipPath', 'linearGradient', 'radialGradient', 'stop',
               'title', 'desc', 'metadata', 'a'}
    identifiers = {}
    decoded_image_bytes = 0
    for node in root.iter():
        name = _name(node)
        namespace = node.tag.split('}')[0].lstrip('{') if '}' in node.tag else ''
        # RDF and other descriptive metadata are inert; leave them intact.
        if namespace not in {'', SVG_NAMESPACE}:
            continue
        if name not in allowed or (name == 'svg' and node is not root):
            _unsupported('嵌套 SVG 视口' if name == 'svg' else name)
        identifier = node.get('id')
        if identifier:
            if identifier in identifiers:
                raise ValueError('SVG 含重复元素 ID。')
            identifiers[identifier] = node
        for prop in ('filter', 'mask', 'marker', 'marker-start', 'marker-mid', 'marker-end',
                     'mix-blend-mode', 'isolation', 'vector-effect', 'paint-order'):
            if node.get(prop) and node.get(prop) not in {'none', 'normal', 'auto'}:
                _unsupported(prop)
        if name in {'g', 'svg'} and node.get('opacity', '1') not in {'1', '1.0', '100%', 'inherit'}:
            _unsupported('分组透明度合成')
        if node.get('clip-path') not in {None, '', 'none'}:
            match = re.fullmatch(r'url\(\s*[\'"]?#([^\s)\'"]+)[\'"]?\s*\)', node.get('clip-path'))
            if not match:
                _unsupported('裁切引用')
            node.set('clip-path', 'url(#' + match.group(1) + ')')
        if name == 'image':
            href_key = 'href' if 'href' in node.attrib else '{http://www.w3.org/1999/xlink}href'
            href = node.get(href_key, '')
            if href.startswith('data:image/'):
                try:
                    payload = base64.b64decode(href.split(',', 1)[1], validate=False)
                    image = Image.open(BytesIO(payload))
                    if image.width * image.height > 60_000_000:
                        raise ValueError('SVG 内嵌图片超过 6000 万像素。')
                    decoded_image_bytes += image.width * image.height * 4
                    if decoded_image_bytes > 512 * 1024 * 1024:
                        raise ValueError('SVG 内嵌图片的累计解码大小超过 512 MB。')
                    if href.lower().startswith('data:image/webp;'):
                        buffer = BytesIO()
                        image.save(buffer, 'PNG')
                        node.set(href_key, 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode())
                except ValueError:
                    raise
                except Exception as exc:
                    raise ValueError('SVG 内嵌图片无法读取。') from exc
    for node in root.iter():
        clipping = node.get('clip-path', '')
        if clipping.startswith('url(#'):
            target = identifiers.get(clipping[5:-1])
            if target is None or _name(target) != 'clipPath':
                raise ValueError('SVG 裁切路径引用不存在。')
        if _name(node) in {'use', 'image', 'linearGradient', 'radialGradient'}:
            target = node.get('href') or node.get('{http://www.w3.org/1999/xlink}href', '')
            if target.startswith('#') and target[1:] not in identifiers:
                raise ValueError('SVG 图形或渐变引用不存在。')
    _normalize_clips(root, identifiers)
    return ET.tostring(root, encoding='utf-8')


class _RenderProblems(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.problems = []
        self.thread_id = threading.get_ident()

    def emit(self, record):
        if record.thread == self.thread_id:
            self.problems.append(record.getMessage()[:240])


def svg_to_pdf(data: bytes) -> bytes:
    """Convert immutable SVG bytes to a cached vector PDF, or explain failure."""
    global _cache_size
    digest = hashlib.sha256(data).hexdigest()
    with _conversion_lock:
        if digest in _cache:
            _cache.move_to_end(digest)
            return _cache[digest]
        from reportlab.graphics import renderPDF
        from svglib.svglib import svg2rlg
        from .vector_fonts import prepare_svg_fonts
        prepared = _prepare_svg(data)
        prepared, font_map = prepare_svg_fonts(prepared)
        logger = logging.getLogger('svglib')
        problems = _RenderProblems()
        logger.addHandler(problems)
        try:
            drawing = svg2rlg(BytesIO(prepared), resolve_entities=False, font_map=font_map,
                              external_reference_root='/__figure_workbench_no_external_svg_resources__')
            if drawing is None or not all(math.isfinite(float(size)) and 0 < size <= 100_000 for size in (drawing.width, drawing.height)):
                raise ValueError('SVG 的物理尺寸无效或过大。')
            result = renderPDF.drawToString(drawing, invariant=1, pageCompression=1)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f'SVG 矢量转换失败：{str(exc)[:160]}') from exc
        finally:
            logger.removeHandler(problems)
        if problems.problems:
            raise ValueError('SVG 存在无法可靠渲染的内容：' + '; '.join(problems.problems[:3]))
        # ReportLab initializes a default font even for a path-only document.
        # The first cleanup removes empty text setup; the second removes its
        # now-unused font resource. No visible text or paths are discarded.
        import fitz
        with fitz.open(stream=result, filetype='pdf') as document:
            document[0].clean_contents()
            document[0].clean_contents()
            result = document.tobytes(garbage=4, deflate=True)
        if len(result) > MAX_OUTPUT_BYTES:
            raise ValueError('SVG 转换后的绘图对象超过 64 MB。')
        if len(result) <= MAX_CACHE_BYTES:
            _cache[digest] = result
            _cache_size += len(result)
            while _cache_size > MAX_CACHE_BYTES or len(_cache) > 64:
                _, previous = _cache.popitem(last=False)
                _cache_size -= len(previous)
        return result


def open_svg(data: bytes):
    """Return a caller-owned PyMuPDF PDF document with correct SVG clipping."""
    import fitz
    return fitz.open(stream=svg_to_pdf(data), filetype='pdf')
