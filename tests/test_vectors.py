"""SVG clips must survive actual import, rather than only look valid in XML."""
from io import BytesIO
import hashlib

import fitz
import numpy as np
from PIL import Image
import pytest

from workbench.storage import ProjectStore
from workbench.vectors import svg_to_pdf


def svg_document(body, prefix=False):
    data = ('<svg xmlns="http://www.w3.org/2000/svg" width="100pt" height="100pt" '
            'viewBox="0 0 100 100">' + body + '</svg>').encode()
    if prefix:
        import xml.etree.ElementTree as ET
        # A prefixed namespace is semantically identical to a default namespace.
        text = data.decode().replace('xmlns=', 'xmlns:q=')
        import re
        text = re.sub(r'<(/?)(svg|defs|clipPath|rect|g)(?=[\s>])', r'<\1q:\2', text)
        data = text.encode()
    return data


def red_mask(data):
    with fitz.open(stream=data, filetype='pdf') as document:
        page = document[0]
        assert page.rect.width == pytest.approx(100)
        assert not page.get_images()
        pixmap = page.get_pixmap(alpha=False)
        pixels = np.frombuffer(pixmap.samples, np.uint8).reshape(pixmap.height, pixmap.width, 3)
        return (pixels[:, :, 0] > 240) & (pixels[:, :, 1] < 20)


@pytest.mark.parametrize('kind', ['attribute', 'inline_css', 'stylesheet', 'prefixed', 'transformed_clip'])
def test_real_clip_geometry_not_ignored(kind):
    shape = '<rect x="25" y="25" width="50" height="50"/>'
    if kind == 'transformed_clip':
        shape = '<g transform="translate(25 25)"><rect width="50" height="50"/></g>'
    definitions = '<defs><clipPath id="crop">' + shape + '</clipPath></defs>'
    attributes = 'clip-path="url(#crop)"'
    if kind == 'inline_css':
        attributes = 'style="clip-path: url(\'\u0023crop\')"'
    elif kind == 'stylesheet':
        definitions += '<style>.cropped{clip-path:url(#crop);fill:red}</style>'
        attributes = 'class="cropped"'
    data = svg_document(definitions + '<g ' + attributes + '><rect width="100" height="100" fill="red"/></g>',
                        prefix=kind == 'prefixed')
    mask = red_mask(svg_to_pdf(data))
    expected = np.zeros((100, 100), bool)
    expected[25:75, 25:75] = True
    np.testing.assert_array_equal(mask, expected)


def test_source_svg_preview_uses_same_clip_and_preserves_original_bytes(tmp_path):
    data = svg_document('<defs><clipPath id="clip"><rect x="25" y="25" width="50" height="50"/></clipPath></defs>'
                        '<g clip-path="url(#clip)"><rect width="100" height="100" fill="red"/></g>')
    store = ProjectStore(tmp_path)
    asset = store.add_files([('original.svg', data)])[0]
    assert asset['sha256'] == hashlib.sha256(data).hexdigest()
    assert store.resolve(asset['id']).read_bytes() == data
    image = np.asarray(Image.open(BytesIO(store.preview(asset['id'], max_size=100))).convert('RGB'))
    mask = (image[:, :, 0] > 240) & (image[:, :, 1] < 20)
    assert mask.sum() == 2500
    assert mask[25:75, 25:75].all()
    reopened = ProjectStore(tmp_path / 'reopened')
    project = reopened.open_archive(store.export_archive())
    assert reopened.resolve(project['assets'][0]['id']).read_bytes() == data


@pytest.mark.parametrize('body,reason', [
    ('<mask id="m"><rect width="50" height="50"/></mask>', 'mask'),
    ('<svg width="50" height="50"><rect width="100" height="100"/></svg>', '嵌套'),
    ('<filter id="f"><feGaussianBlur stdDeviation="3"/></filter>', 'filter'),
    ('<defs><clipPath id="c" clipPathUnits="objectBoundingBox"><rect width="1" height="1"/></clipPath></defs>', 'objectBoundingBox'),
    ('<text x="5" y="50" font-family="A Font That Does Not Exist">label</text>', '字体不可用'),
    ('<g clip-path="url(#missing)"><rect width="100" height="100"/></g>', '引用不存在'),
])
def test_unsupported_svg_cannot_silently_change_scientific_figure(tmp_path, body, reason):
    store = ProjectStore(tmp_path)
    before = store.get_project()
    with pytest.raises(ValueError, match=reason):
        store.add_files([('unsupported.svg', svg_document(body))])
    assert store.get_project() == before
