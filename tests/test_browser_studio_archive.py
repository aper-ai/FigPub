"""Cross-runtime checks for the browser's real ZIP/PNG/PDF encoders; no browser simulation.

These are distinct from the Playwright UI workflow in test_browser_studio_ui.py.
"""
from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess

import fitz
import numpy as np
from PIL import Image
import pytest

from workbench.storage import ProjectStore

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')
pytestmark = pytest.mark.skipif(NODE is None, reason='Node is required for browser/native archive interoperation checks')


def node(script: str, payload: dict) -> dict:
    completed = subprocess.run([NODE, '--input-type=module', '-e', script], cwd=ROOT,
                               input=json.dumps(payload), text=True, capture_output=True, check=True)
    return json.loads(completed.stdout)


PREAMBLE = """
import {readFileSync} from 'node:fs';
import {newProject, panelForAsset, validateBrowserProject} from './web/browser/model.js';
import {encodeProject, decodeProject, sha256} from './web/browser/archive.js';
import {applyPanelLayout} from './web/modules/layout-engine.js';
const input = JSON.parse(readFileSync(0, 'utf8'));
"""


def synthetic_png() -> bytes:
    rows, columns = np.indices((64, 96))
    array = np.stack([columns % 256, rows % 256, (rows + columns) % 256], axis=-1).astype(np.uint8)
    buffer = BytesIO(); Image.fromarray(array).save(buffer, format='PNG')
    return buffer.getvalue()


def browser_project(original: bytes) -> bytes:
    result = node(PREAMBLE + """
const bytes = new Uint8Array(Buffer.from(input.png, 'base64')), id = await sha256(bytes);
const asset = {id, sha256: id, name: 'synthetic.png', kind: 'image', width_px: 96, height_px: 64, bit_depth: 8, dtype: 'uint8', channels: 3, frames: 1, metadata: {browser_display_only: true}, preview_url: 'blob:temporary'};
const project = newProject(); project.title = '浏览器与本机互通'; project.assets.push(asset); project.panels.push(panelForAsset(asset)); project.panels[0].title = '蛋白表达'; project.panels[0].group_id = 'group_test';
applyPanelLayout(project); validateBrowserProject(project);
const archive = await encodeProject(project, new Map([[id, {bytes, extension: '.png'}]]));
process.stdout.write(JSON.stringify({archive: Buffer.from(await archive.arrayBuffer()).toString('base64')}));
""", {'png': base64.b64encode(original).decode()})
    return base64.b64decode(result['archive'])


def test_browser_project_opens_in_native_workbench_without_changing_originals(tmp_path):
    original = synthetic_png(); archive = browser_project(original)
    store = ProjectStore(tmp_path / 'native'); store.open_archive(archive)
    document = store.get_project()
    assert document['title'] == '浏览器与本机互通'
    assert document['panels'][0]['label'] == 'A'
    assert document['panels'][0]['group_id'] == 'group_test'
    assert document['panels'][0]['title'] == '蛋白表达'
    assert store.resolve(document['assets'][0]['id']).read_bytes() == original
    assert document['settings']['browser_studio']['version'] == json.loads((ROOT / 'package.json').read_text('utf-8'))['version']


def test_native_deflated_project_returns_to_browser_with_all_originals(tmp_path):
    original = synthetic_png(); store = ProjectStore(tmp_path / 'native'); store.open_archive(browser_project(original))
    archive = store.export_archive()
    result = node(PREAMBLE + """
const decoded = await decodeProject(new Uint8Array(Buffer.from(input.archive, 'base64')));
validateBrowserProject(decoded.project);
process.stdout.write(JSON.stringify({project: decoded.project, original: Buffer.from(decoded.originals[0].bytes).toString('base64')}));
""", {'archive': base64.b64encode(archive).decode()})
    assert base64.b64decode(result['original']) == original
    assert result['project']['panels'][0]['label_mode'] == 'auto'
    assert result['project']['panels'][0]['group_id'] == 'group_test'
    assert result['project']['title'] == '浏览器与本机互通'


@pytest.mark.parametrize('dpi', [150, 300, 600])
def test_browser_png_metadata_is_read_correctly_by_pillow(dpi):
    original = synthetic_png()
    result = node("""
import {readFileSync} from 'node:fs'; import {pngWithDensity} from './web/browser/encoding.js';
const input = JSON.parse(readFileSync(0, 'utf8')); const blob = pngWithDensity(new Uint8Array(Buffer.from(input.png, 'base64')), input.dpi);
process.stdout.write(JSON.stringify({png: Buffer.from(await blob.arrayBuffer()).toString('base64')}));
""", {'png': base64.b64encode(original).decode(), 'dpi': dpi})
    image = Image.open(BytesIO(base64.b64decode(result['png'])))
    assert image.size == (96, 64)
    assert abs(image.info['dpi'][0] - dpi) < .02
    assert image.tobytes() == Image.open(BytesIO(original)).tobytes()


def test_browser_raster_pdf_has_exact_page_size_and_lossless_embedded_pixels():
    original = Image.open(BytesIO(synthetic_png())); pixels = original.tobytes()
    result = node("""
import {readFileSync} from 'node:fs'; import {encodeRasterPdf} from './web/browser/encoding.js';
const input = JSON.parse(readFileSync(0, 'utf8'));
const pdf = await encodeRasterPdf(new Uint8Array(Buffer.from(input.rgb, 'base64')), 96, 64, 89, 60);
process.stdout.write(JSON.stringify({pdf: Buffer.from(await pdf.arrayBuffer()).toString('base64')}));
""", {'rgb': base64.b64encode(pixels).decode()})
    document = fitz.open(stream=base64.b64decode(result['pdf']), filetype='pdf')
    assert len(document) == 1
    page = document[0]
    assert abs(page.rect.width * 25.4 / 72 - 89) < .0001
    assert abs(page.rect.height * 25.4 / 72 - 60) < .0001
    assert page.get_text() == ''  # Deliberately and explicitly a raster PDF.
    images = page.get_images(full=True); assert len(images) == 1
    assert images[0][2:4] == (96, 64)
    extracted = Image.open(BytesIO(document.extract_image(images[0][0])['image'])).convert('RGB')
    assert extracted.tobytes() == pixels
    rendered = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    assert rendered.width > 500 and rendered.height > 300
    assert np.asarray(Image.frombytes('RGB', (rendered.width, rendered.height), rendered.samples)).std() > 8


def test_build_font_coverage_matches_actual_cmap_for_all_three_fonts():
    from fontTools.ttLib import TTFont

    result = node("""
import {readFileSync} from 'node:fs'; import {fontCoverage} from './scripts/build_browser_studio_fonts.mjs';
process.stdout.write(JSON.stringify(Object.fromEntries([['normal','DejaVuSans.ttf',0],['bold','DejaVuSans-Bold.ttf',0],['cjk','NotoSansCJK-Regular.ttc',2]].map(([key,name,index])=>[key,fontCoverage(readFileSync('resources/fonts/'+name),index)]))));
""", {})
    for key, name, index in [('normal', 'DejaVuSans.ttf', -1), ('bold', 'DejaVuSans-Bold.ttf', -1), ('cjk', 'NotoSansCJK-Regular.ttc', 2)]:
        font = TTFont(ROOT / 'resources/fonts' / name, fontNumber=index, lazy=True)
        actual = {codepoint for codepoint, glyph in font.getBestCmap().items() if font.getGlyphID(glyph)}
        generated = {codepoint for start, end in result[key] for codepoint in range(start, end + 1)}
        assert generated == actual
        font.close()
