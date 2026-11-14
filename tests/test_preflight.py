import base64
import io

import fitz
import numpy as np
import tifffile
from PIL import Image
import pytest

from workbench.preflight import check_project
from workbench.storage import ProjectStore


def source_tiff(array):
    buffer=io.BytesIO()
    tifffile.imwrite(buffer,array,photometric='minisblack')
    return buffer.getvalue()


def codes(report):
    return {issue['code'] for issue in report['issues']}


def test_actual_pixels_physical_scale_and_display_clipping(tmp_path):
    store=ProjectStore(tmp_path)
    values=np.arange(10_000,dtype=np.uint16).reshape(100,100)
    asset=store.add_files([('original.tif',source_tiff(values))])[0]
    project=store.get_project()
    panel=store.default_panel(asset,width=25.4,height=25.4)
    panel['display']={'low':1000,'high':8000,'gamma':1.2,'invert':False}
    project['panels']=[panel]
    report=check_project(project,store.resolve)
    assert report['ok']
    metric=report['metrics']['panels'][0]
    assert abs(metric['effective_ppi']-100)<1e-9
    assert {'LOW_EFFECTIVE_PPI','DISPLAY_CLIPPING','NONLINEAR_DISPLAY'}<=codes(report)
    assert report['metrics']['source_hashes_verified']==1
    assert np.array_equal(store.read_array(asset['id']),values)


def test_crop_metadata_and_hash_not_blindly_trusted(tmp_path):
    store=ProjectStore(tmp_path)
    asset=store.add_files([('original.tif',source_tiff(np.ones((100,100),np.uint16)))])[0]
    project=store.get_project()
    asset_record=project['assets'][0]
    asset_record['width_px']=10000
    panel=store.default_panel(asset,width=10,height=10)
    panel['crop']['width']=200
    project['panels']=[panel]
    report=check_project(project,store.resolve)
    assert not report['ok'] and {'SOURCE_METADATA_DIFFERS','CROP_OUTSIDE_SOURCE'}<=codes(report)
    project['panels'][0]['crop']['width']=100
    store.resolve(asset['id']).write_bytes(store.resolve(asset['id']).read_bytes()+b'changed')
    report=check_project(project,store.resolve)
    assert 'SOURCE_HASH_MISMATCH' in codes(report) and not report['ok']


def test_pdf_embedded_raster_uses_actual_transform_not_container_extension(tmp_path):
    raw=io.BytesIO()
    Image.new('RGB',(100,100),'red').save(raw,format='PNG')
    document=fitz.open()
    page=document.new_page(width=180,height=180)
    page.insert_image(fitz.Rect(0,0,180,180),stream=raw.getvalue())
    pdf=document.tobytes();document.close()
    store=ProjectStore(tmp_path)
    asset=store.add_files([('figure.pdf',pdf)])[0]
    project=store.get_project()
    project['panels']=[store.default_panel(asset,width=25.4,height=25.4)]
    report=check_project(project,store.resolve)
    assert 'EMBEDDED_RASTER_LOW_PPI' in codes(report)
    metric=report['metrics']['panels'][0]
    assert len(metric['embedded_rasters'])==1
    assert abs(metric['effective_ppi']-100)<1e-6


def test_svg_embedded_image_and_vector_only_are_distinguished(tmp_path):
    png=io.BytesIO();Image.new('RGB',(50,50),'blue').save(png,format='PNG')
    svg=f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="100" height="100"><image width="100" height="100" xlink:href="data:image/png;base64,{base64.b64encode(png.getvalue()).decode()}"/></svg>'.encode()
    vector=b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="#246"/></svg>'
    store=ProjectStore(tmp_path)
    raster_asset,vector_asset=store.add_files([('raster.svg',svg),('vector.svg',vector)])
    project=store.get_project()
    project['panels']=[store.default_panel(raster_asset,width=25.4,height=25.4),store.default_panel(vector_asset,'B',x=50,width=25.4,height=25.4)]
    report=check_project(project,store.resolve)
    assert 'EMBEDDED_RASTER_LOW_PPI' in codes(report)
    metrics={entry['asset_id']:entry for entry in report['metrics']['panels']}
    assert abs(metrics[raster_asset['id']]['effective_ppi']-50)<1e-5
    assert metrics[vector_asset['id']]['effective_ppi'] is None


def test_hidden_lanes_fonts_bounds_and_invalid_source_coverage(tmp_path):
    store=ProjectStore(tmp_path)
    asset=store.add_files([('source.tif',source_tiff(np.ones((60,120),np.uint16)))])[0]
    project=store.get_project()
    a=store.default_panel(asset,width=80,height=40)
    a['gel']={'lane_centers':[20,60,100],'hidden_lanes':[1],'lane_names':['a','b','c']}
    b=store.default_panel(asset,'A',x=170,width=40,height=30)
    project['panels']=[a,b]
    project['annotations']=[{'id':'special','type':'text','x_mm':10,'y_mm':10,'width_mm':30,'height_mm':4,'text':'中文\U0010ffff','font_size_pt':5}]
    project['measurements']=[{'source_asset_ids':['f'*64]}]
    report=check_project(project,store.resolve)
    assert not report['ok']
    assert {'LANES_REMOVED','DUPLICATE_PANEL_LABEL','PANEL_OUTSIDE_CANVAS','SMALL_TEXT','MISSING_GLYPHS','SOURCE_COVERAGE_INCOMPLETE'}<=codes(report)


def test_source_pdf_font_embedding_reported(tmp_path):
    doc=fitz.open();page=doc.new_page(width=100,height=100);page.insert_text((10,40),'Hello',fontsize=12)
    payload=doc.tobytes();doc.close()
    store=ProjectStore(tmp_path)
    asset=store.add_files([('text.pdf',payload)])[0]
    project=store.get_project();project['panels']=[store.default_panel(asset,width=35.2778,height=35.2778)]
    report=check_project(project,store.resolve)
    assert 'SOURCE_PDF_FONTS_UNEMBEDDED' in codes(report)


@pytest.mark.parametrize('changes,part', [
    ({'y_mm':4, 'label':'AA', 'label_font_size_pt':24}, 'label'),
    ({'x_mm':1, 'width_mm':10, 'title':'WWW', 'title_font_size_pt':60}, 'title'),
    ({'x_mm':0, 'width_mm':10, 'height_mm':40, 'rotation':45}, 'image'),
])
def test_complete_panel_display_bounds_warn_and_identify_the_clipped_part(tmp_path, changes, part):
    store = ProjectStore(tmp_path)
    asset = store.add_files([('source.tif', source_tiff(np.ones((60, 120), np.uint16)))])[0]
    project = store.get_project()
    panel = store.default_panel(asset, '', x=20, y=25, width=40, height=20)
    panel.update(changes)
    project['panels'] = [panel]
    report = check_project(project, store.resolve)
    finding = next(item for item in report['issues'] if item['code'] == 'PANEL_OUTSIDE_CANVAS')
    assert finding['severity'] == 'warning'  # intentional custom overflow stays exportable
    assert finding['panel_id'] == panel['id']
    assert finding['details']['outside_parts'] == [part]
    assert part in finding['details']['bounds_mm']


def test_measured_title_width_avoids_false_clipping_warning(tmp_path):
    store = ProjectStore(tmp_path)
    asset = store.add_files([('source.tif', source_tiff(np.ones((60, 120), np.uint16)))])[0]
    project = store.get_project()
    panel = store.default_panel(asset, '', x=6, y=15, width=8, height=4)
    panel.update(title='iiiiiiiiiii', title_font_size_pt=8)
    project['panels'] = [panel]
    assert 'PANEL_OUTSIDE_CANVAS' not in codes(check_project(project, store.resolve))


@pytest.mark.parametrize('annotation', [
    {'type':'text', 'x_mm':179, 'y_mm':10, 'width_mm':0, 'height_mm':0,
     'text':'Long annotation', 'font_size_pt':12},
    {'type':'text', 'x_mm':10, 'y_mm':115, 'width_mm':0, 'height_mm':0,
     'text':'First line\nSecond line', 'font_size_pt':12},
    {'type':'line', 'x_mm':0, 'y_mm':10, 'width_mm':10, 'height_mm':0,
     'line_width_pt':2},
])
def test_annotation_bounds_include_text_overflow_multiple_lines_and_strokes(tmp_path, annotation):
    store = ProjectStore(tmp_path)
    project = store.get_project()
    project['annotations'] = [{'id':'outside_annotation', **annotation}]
    report = check_project(project, store.resolve)
    finding = next(item for item in report['issues'] if item['code'] == 'ANNOTATION_OUTSIDE_CANVAS')
    assert finding['details']['annotation_id'] == 'outside_annotation'
    assert len(finding['details']['bounds_mm']) == 4


def test_hidden_items_and_none_labels_are_not_checked_as_exported_ink(tmp_path):
    store = ProjectStore(tmp_path)
    asset = store.add_files([('source.tif', source_tiff(np.ones((60, 120), np.uint16)))])[0]
    project = store.get_project()
    hidden = store.default_panel(asset, 'A', x=-100, y=-100)
    hidden['hidden'] = True
    visible = store.default_panel(asset, 'A', x=20, y=1, width=40, height=20)
    visible['label_mode'] = 'none'
    project['panels'] = [hidden, visible]
    project['annotations'] = [{'id':'hidden_text', 'hidden':True, 'type':'text',
                              'text':'Hidden', 'x_mm':-20, 'y_mm':-20}]
    report = check_project(project, store.resolve)
    assert not {'PANEL_OUTSIDE_CANVAS', 'ANNOTATION_OUTSIDE_CANVAS', 'DUPLICATE_PANEL_LABEL'} & codes(report)
