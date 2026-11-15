"""Mixed-aspect originals keep aligned panel captions in portable native output.

The fixture contains generated color patterns with source lettering. It uses
the real JavaScript auto-layout, then the saved project and Python exporters.
"""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

import fitz
import numpy as np
from PIL import Image, ImageDraw
from pydantic import ValidationError
import pytest

from workbench.exporting import build_scene, export_project, MM_TO_PT
from workbench.figure_geometry import panel_label_position, panel_display_bounds, text_bounds
from workbench.models import Panel
from workbench.storage import ProjectStore

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')


@pytest.fixture
def aligned_project(tmp_path):
    if not NODE:
        pytest.skip('Cross-runtime layout/export regression requires Node.js')
    store = ProjectStore(tmp_path / 'original')
    originals = {}
    for index, (width, height) in enumerate([(32, 80), (96, 32), (120, 24), (48, 64)]):
        image = Image.new('RGB', (width, height), (70 + index * 35, 150, 200))
        ImageDraw.Draw(image).text((2, 2), f'SOURCE {chr(65 + index)}', fill='white')
        buffer = BytesIO()
        image.save(buffer, 'PNG')
        asset = store.add_files([(f'Synthetic_{index}.png', buffer.getvalue())])[0]
        originals[asset['id']] = buffer.getvalue()
    project = store.get_project()
    project['panels'] = []
    for index, asset in enumerate(project['assets']):
        panel = store.default_panel(asset, '', 10, 20, 40, 30)
        panel.update(id=f'p{index}', label_mode='auto', label_font_size_pt=[8, 11, 10, 13][index])
        project['panels'].append(panel)
    crops = deepcopy([panel['crop'] for panel in project['panels']])
    script = """
      import {readFileSync} from 'node:fs';
      import {applyPanelLayout,panelLabelPosition} from './web/modules/layout-engine.js';
      const project=JSON.parse(readFileSync(0,'utf8'));
      const plan=applyPanelLayout(project,{columns:2,rows:2});
      process.stdout.write(JSON.stringify({project,cells:plan.cells,anchors:project.panels.map(panel=>panelLabelPosition(panel))}));
    """
    result = subprocess.run([NODE, '--input-type=module', '-e', script],
                            cwd=ROOT, input=json.dumps(project), capture_output=True,
                            text=True, check=True, timeout=15)
    planned = json.loads(result.stdout)
    project = planned['project']
    assert [panel['crop'] for panel in project['panels']] == crops
    store.update_project(project)
    restored_store = ProjectStore(tmp_path / 'restored')
    restored = restored_store.open_archive(store.export_archive())
    assert restored['panels'] == project['panels']
    for asset_id, data in originals.items():
        assert restored_store.resolve(asset_id).read_bytes() == data
    return restored, restored_store, planned, originals


def test_real_layout_survives_save_reopen_and_native_scene_without_changing_original_lettering(aligned_project):
    project, store, planned, originals = aligned_project
    scene = build_scene(project, store.resolve)
    labels = [item for item in scene.primitives if item['type'] == 'text']
    images = [item for item in scene.primitives if item['type'] == 'image']
    assert [item['text'] for item in labels] == ['A', 'B', 'C', 'D']
    for index, (panel, label, image) in enumerate(zip(project['panels'], labels, images)):
        expected = planned['anchors'][index]
        assert (label['x'], label['y']) == pytest.approx((expected['x_mm'], expected['y_mm']))
        assert label['x'] == pytest.approx(planned['cells'][index]['x_mm'], abs=1e-5)
        assert panel_label_position(panel) == pytest.approx((label['x'], label['y']))
        assert panel_display_bounds(panel)['label'] == pytest.approx(text_bounds(
            panel['label'], label['x'], label['y'], panel['label_font_size_pt'], bold=True))
        source = Image.open(BytesIO(originals[panel['asset_id']])).convert('RGB')
        np.testing.assert_array_equal(np.asarray(image['image'].convert('RGB')), np.asarray(source))
        assert panel['width_mm'] / panel['height_mm'] == pytest.approx(source.width / source.height, abs=1e-5)
    assert labels[0]['x'] == pytest.approx(labels[2]['x'])
    assert labels[1]['x'] == pytest.approx(labels[3]['x'])
    assert labels[0]['y'] == pytest.approx(labels[1]['y'])
    assert labels[2]['y'] == pytest.approx(labels[3]['y'])


@pytest.mark.parametrize('format', ['pdf', 'svg', 'pptx', 'png'])
def test_actual_publication_outputs_use_the_saved_grid_label_positions(aligned_project, format):
    project, store, planned, _ = aligned_project
    data, _, _ = export_project(project, store.resolve, format, dpi=150)
    if format == 'pdf':
        with fitz.open(stream=data, filetype='pdf') as document:
            spans = {span['text']: span for block in document[0].get_text('dict')['blocks']
                     for line in block.get('lines', []) for span in line['spans']}
            for panel, expected in zip(project['panels'], planned['anchors']):
                span = spans[panel['label']]
                assert span['origin'] == pytest.approx((expected['x_mm'] * MM_TO_PT,
                                                       expected['y_mm'] * MM_TO_PT), abs=1e-4)
                assert span['size'] == pytest.approx(panel['label_font_size_pt'])
            # Every original image is embedded at its source resolution.
            assert sorted(image[2:4] for image in document[0].get_images()) == [(32, 80), (48, 64), (96, 32), (120, 24)]
    elif format == 'svg':
        labels = {''.join(node.itertext()): node for node in
                  ET.fromstring(data).iter('{http://www.w3.org/2000/svg}text')}
        for panel, expected in zip(project['panels'], planned['anchors']):
            node = labels[panel['label']]
            # Native SVG writes point coordinates with six significant digits.
            assert float(node.get('x')) == pytest.approx(expected['x_mm'] * MM_TO_PT, abs=5.1e-4)
            assert float(node.get('y')) == pytest.approx(expected['y_mm'] * MM_TO_PT, abs=5.1e-4)
    elif format == 'pptx':
        from pptx import Presentation
        labels = {shape.text: shape for shape in Presentation(BytesIO(data)).slides[0].shapes
                  if shape.has_text_frame}
        for panel, expected in zip(project['panels'], planned['anchors']):
            shape = labels[panel['label']]
            assert shape.left.mm == pytest.approx(expected['x_mm'], abs=1e-4)
            assert shape.top.mm + panel['label_font_size_pt'] / MM_TO_PT == pytest.approx(expected['y_mm'], abs=1e-4)
            assert shape.text_frame.paragraphs[0].font.size.pt == panel['label_font_size_pt']
    else:
        pixels = np.asarray(Image.open(BytesIO(data)).convert('RGB'))
        # Label A is well to the left of its narrow source image. Verify actual
        # exported ink at the grid anchor, where the previous renderer was blank.
        anchor = planned['anchors'][0]
        scale = 150 / 25.4
        patch = pixels[int((anchor['y_mm'] - 4) * scale):int((anchor['y_mm'] + .8) * scale),
                       int(anchor['x_mm'] * scale):int((anchor['x_mm'] + 4) * scale)]
        assert (patch.max(axis=2) < 100).sum() > 10


@pytest.mark.parametrize('anchor', [
    {'x_ratio': float('nan'), 'y_ratio': 0}, {'x_ratio': 0, 'y_ratio': float('inf')},
    {'x_ratio': True, 'y_ratio': 0}, {'x_ratio': '0', 'y_ratio': 0},
    {'x_ratio': 400001, 'y_ratio': 0}, {'x_ratio': 0}, [], 'left',
])
def test_schema_rejects_unusable_label_anchors(anchor):
    with pytest.raises(ValidationError):
        Panel.model_validate({'id': 'p', 'asset_id': 'a' * 64, 'x_mm': 10, 'y_mm': 20,
                              'width_mm': 40, 'height_mm': 30, 'label_anchor': anchor})


def test_legacy_schema_does_not_add_or_relocate_label_anchors():
    legacy = {'id': 'p', 'asset_id': 'a' * 64, 'x_mm': 10, 'y_mm': 20,
              'width_mm': 40, 'height_mm': 30, 'label': 'Original label'}
    assert Panel.model_validate(legacy).model_dump(exclude_unset=True) == legacy
    assert panel_label_position(legacy) == (10, 18.2)
