from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
import copy
import xml.etree.ElementTree as ET

import fitz
import numpy as np
from PIL import Image
import pytest

from workbench.exporting import export_project, MM_TO_PT
from workbench.figure_geometry import annotation_display_bounds
from workbench.storage import ProjectStore, new_project
from workbench.vectors import svg_to_pdf


def panel_for(asset, **extra):
    panel = ProjectStore.default_panel(asset, '', 20, 20, 60, 40)
    panel.update(extra)
    return panel


def pdf_pixels(data, dpi=144):
    with fitz.open(stream=data, filetype='pdf') as document:
        pixmap = document[0].get_pixmap(dpi=dpi, alpha=False)
        return np.frombuffer(pixmap.samples, np.uint8).reshape(pixmap.height, pixmap.width, 3).copy()


def vector_pdf():
    document = fitz.open()
    page = document.new_page(width=240, height=160)
    for rect, color in [(fitz.Rect(0,0,120,80),(1,0,0)),(fitz.Rect(120,0,240,80),(0,1,0)),
                        (fitz.Rect(0,80,120,160),(0,0,1)),(fitz.Rect(120,80,240,160),(1,1,0))]:
        page.draw_rect(rect, color=None, fill=color)
    data = document.tobytes()
    document.close()
    return data


def test_cjk_font_embedded_and_visibly_rendered(tmp_path):
    project = new_project('中文字体')
    project['annotations'] = [{'id':'a','type':'text','x_mm':10,'y_mm':10,'width_mm':90,'height_mm':20,
                               'text':'定量分析 / β-actin','font_size_pt':18,'color':'#111111'}]
    data, _, _ = export_project(project, lambda _: None, 'pdf')
    with fitz.open(stream=data, filetype='pdf') as document:
        assert '定量分析' in document[0].get_text()
        for font in document[0].get_fonts():
            assert len(document.extract_font(font[0])[3]) > 100
    pixels = pdf_pixels(data)
    # A CFF glyph renumbering regression left only '/' visible despite correct
    # text extraction. Verify actual ink where the Chinese word must appear.
    assert (pixels[55:110, 55:240].mean(axis=2) < 150).sum() > 700
    outlined, _, _ = export_project(project, lambda _: None, 'pdf', outlined_text=True)
    with fitz.open(stream=outlined, filetype='pdf') as document:
        assert not document[0].get_fonts()
    assert np.mean(np.abs(pdf_pixels(data).astype(float)-pdf_pixels(outlined))) < .8
    # An editable SVG embeds the actual CJK font. Its import/preview must keep
    # visible glyphs and metrics rather than silently falling back to Helvetica.
    svg, _, _ = export_project(project, lambda _: None, 'svg')
    restored = svg_to_pdf(svg)
    with fitz.open(stream=restored, filetype='pdf') as document:
        assert '定量分析' in document[0].get_text()
        assert all(len(document.extract_font(font[0])[3]) > 100 for font in document[0].get_fonts())
    restored_pixels = pdf_pixels(restored)
    assert (restored_pixels[55:110, 55:240].mean(axis=2) < 150).sum() > 700
    assert np.mean(np.abs(pixels.astype(float)-restored_pixels)) < .8
    outlined_svg, _, _ = export_project(project, lambda _: None, 'svg', outlined_text=True)
    assert ET.fromstring(outlined_svg).get('width') == '180mm'
    assert np.mean(np.abs(pixels.astype(float)-pdf_pixels(svg_to_pdf(outlined_svg)))) < .8


@pytest.mark.parametrize('flip_h,flip_v,rotation', [(False,False,0),(True,False,0),(False,True,0),(True,True,23)])
def test_vectors_stay_vectors_and_svg_pdf_geometry_agrees(tmp_path, flip_h, flip_v, rotation):
    store = ProjectStore(tmp_path)
    asset = store.add_files([('quadrants.pdf', vector_pdf())])[0]
    project = store.get_project()
    project['panels'] = [panel_for(asset, flip_h=flip_h, flip_v=flip_v, rotation=rotation,
                                 crop={'x':30,'y':20,'width':180,'height':120})]
    pdf, _, _ = export_project(project, store.resolve, 'pdf')
    svg, _, _ = export_project(project, store.resolve, 'svg')
    with fitz.open(stream=pdf,filetype='pdf') as document:
        assert not document[0].get_images()
        assert len(document[0].get_drawings()) >= 4
    # MuPDF's SVG reader ignores all clipPath elements. Test with the same
    # standards-aware vector importer used by preview and publication export.
    svg_pdf = svg_to_pdf(svg)
    with fitz.open(stream=svg_pdf,filetype='pdf') as document:
        assert not document[0].get_images()
        assert len(document[0].get_drawings()) >= 4
    difference = np.mean(np.abs(pdf_pixels(pdf).astype(float)-pdf_pixels(svg_pdf)))
    assert difference < 1.2
    # The outlined PDF pipeline previously round-tripped through MuPDF's SVG
    # reader, exposing original content outside every crop. Verify the real
    # output pixels and retained vector geometry for every affine combination.
    outlined, _, _ = export_project(project, store.resolve, 'pdf', outlined_text=True)
    with fitz.open(stream=outlined,filetype='pdf') as document:
        assert not document[0].get_images()
        assert not document[0].get_fonts()
        assert len(document[0].get_drawings()) >= 4
    assert np.mean(np.abs(pdf_pixels(pdf).astype(float)-pdf_pixels(outlined))) < 1.2


def test_raster_rotation_does_not_resample_native_source(tmp_path):
    store = ProjectStore(tmp_path)
    pixels = np.arange(120*80, dtype=np.uint16).reshape(80,120)
    buffer = BytesIO(); Image.fromarray(pixels).save(buffer, 'PNG')
    asset = store.add_files([('counts.png', buffer.getvalue())])[0]
    project = store.get_project(); project['panels'] = [panel_for(asset,rotation=27)]
    pdf, _, _ = export_project(project, store.resolve, 'pdf')
    with fitz.open(stream=pdf,filetype='pdf') as document:
        images = document[0].get_images()
        assert len(images) == 1 and images[0][2:4] == (120,80)
    np.testing.assert_array_equal(store.read_array(asset['id']), pixels)


def test_hidden_lane_removed_with_visible_gap_and_consistent_flip(tmp_path):
    store = ProjectStore(tmp_path)
    source = np.zeros((100,300,3),np.uint8)
    source[:,:100] = [255,0,0]; source[:,100:200] = [0,255,0]; source[:,200:] = [0,0,255]
    buffer = BytesIO();Image.fromarray(source).save(buffer,'PNG')
    asset = store.add_files([('three.png',buffer.getvalue())])[0]
    project = store.get_project()
    panel = panel_for(asset, width_mm=60,height_mm=20,flip_h=True)
    panel['gel'].update(lane_centers=[50,150,250],hidden_lanes=[1])
    project['panels']=[panel]
    data,_,_=export_project(project,store.resolve,'pdf')
    pixels=pdf_pixels(data)
    row=int(30*MM_TO_PT*2)
    assert pixels[row,int(30*MM_TO_PT*2),2] > 240
    assert pixels[row,int(70*MM_TO_PT*2),0] > 240
    assert not np.any((pixels[:,:,1]>200)&(pixels[:,:,0]<100)&(pixels[:,:,2]<100))
    panel['gel']['hidden_lanes']=[0,1,2]
    with pytest.raises(ValueError):export_project(project,store.resolve,'pdf')


def test_all_formats_have_real_payloads_physical_units_and_portable_project(tmp_path):
    store=ProjectStore(tmp_path);project=store.demo()
    for fmt in ['pdf','svg','png','tiff','pptx']:
        data,mime,name=export_project(project,store.resolve,fmt,dpi=150)
        assert len(data)>1000 and name.endswith('.'+fmt)
        if fmt=='pdf':
            with fitz.open(stream=data,filetype='pdf') as document:
                assert document[0].rect.width==pytest.approx(180*MM_TO_PT,abs=.001)
        elif fmt=='svg':
            root=ET.fromstring(data);assert root.get('width')=='180mm'
            # The application's own export must be a valid import again.
            assert store.add_files([('roundtrip.svg',data)])[0]['kind']=='svg'
        elif fmt in {'png','tiff'}:
            image=Image.open(BytesIO(data))
            assert image.size==(1063,709)
            assert image.info['dpi'][0]==pytest.approx(150,abs=.1)
        elif fmt=='pptx':
            from pptx import Presentation
            presentation=Presentation(BytesIO(data))
            assert len(presentation.slides)==1
            assert any('示例数据' in shape.text for shape in presentation.slides[0].shapes if shape.has_text_frame)
    archive=store.export_archive()
    restored=ProjectStore(tmp_path/'new').open_archive(archive)
    assert len(restored['assets'])==4


def test_missing_sources_and_invalid_output_resolution_fail(tmp_path):
    store=ProjectStore(tmp_path);project=store.demo()
    with pytest.raises(ValueError):export_project(project,store.resolve,'png',dpi=3000)
    with pytest.raises(ValueError):export_project(project,store.resolve,'html')
    with pytest.raises(ValueError,match='丢失'):
        export_project(project,lambda _:tmp_path/'missing.tif','pdf')


def test_panel_caption_sizes_and_upright_orientation_match_across_formats(tmp_path):
    from pptx import Presentation

    store = ProjectStore(tmp_path)
    asset = store.add_files([('source.pdf', vector_pdf())])[0]
    project = store.get_project()
    project['panels'] = [panel_for(asset, y_mm=35, rotation=27, label='AA',
                                 label_mode='manual', label_font_size_pt=16,
                                 title='Panel title', title_font_size_pt=24)]
    pdf, _, _ = export_project(project, store.resolve, 'pdf')
    with fitz.open(stream=pdf, filetype='pdf') as document:
        lines = [line for block in document[0].get_text('dict')['blocks']
                 for line in block.get('lines', [])]
        spans = {span['text']: span for line in lines for span in line['spans']}
        assert spans['AA']['size'] == pytest.approx(16)
        assert spans['Panel title']['size'] == pytest.approx(24)
        assert all(tuple(line['dir']) == (1, 0) for line in lines)
        assert not document[0].get_images()  # source PDF still contains vectors

    svg, _, _ = export_project(project, store.resolve, 'svg')
    texts = {''.join(node.itertext()): node for node in ET.fromstring(svg).iter('{http://www.w3.org/2000/svg}text')}
    assert float(texts['AA'].get('font-size')) == 16
    assert float(texts['Panel title'].get('font-size')) == 24
    assert texts['Panel title'].get('transform') is None

    pptx, _, _ = export_project(project, store.resolve, 'pptx')
    shapes = {shape.text: shape for shape in Presentation(BytesIO(pptx)).slides[0].shapes
              if shape.has_text_frame}
    assert shapes['AA'].text_frame.paragraphs[0].font.size.pt == 16
    assert shapes['Panel title'].text_frame.paragraphs[0].font.size.pt == 24
    assert shapes['Panel title'].rotation == 0


def test_explicit_none_label_mode_suppresses_cached_caption_in_export(tmp_path):
    store = ProjectStore(tmp_path)
    asset = store.add_files([('source.pdf', vector_pdf())])[0]
    project = store.get_project()
    project['panels'] = [panel_for(asset, label='Suppressed caption', label_mode='none')]
    pdf, _, _ = export_project(project, store.resolve, 'pdf')
    with fitz.open(stream=pdf, filetype='pdf') as document:
        assert 'Suppressed caption' not in document[0].get_text()


@pytest.mark.parametrize('text,bold', [('gjÁβ', False), ('AA', True), ('定量分析 / β-actin', False), ('First\r\nSecond\n', False)])
def test_text_bounds_match_real_export_ink(text, bold):
    project = new_project('Measured text')
    annotation = {'id':'measured', 'type':'text', 'x_mm':25, 'y_mm':20,
                  'width_mm':0, 'height_mm':0, 'text':text, 'font_size_pt':18,
                  'bold':bold, 'color':'#000000'}
    project['annotations'] = [annotation]
    data, _, _ = export_project(project, lambda _: None, 'pdf')
    pixels = pdf_pixels(data)
    rows, columns = np.where(pixels.min(axis=2) < 250)
    assert rows.size
    # Display bounds include the editor's top-left anchor; the right/bottom
    # edges use the actual glyph outline, not the nominal text box or em width.
    x, y, width, height = annotation_display_bounds(annotation)
    scale = MM_TO_PT * 2
    assert columns.min() >= x * scale - 1.5
    assert rows.min() >= y * scale - 1.5
    assert columns.max() + 1 == pytest.approx((x + width) * scale, abs=1.5)
    assert rows.max() + 1 == pytest.approx((y + height) * scale, abs=1.5)
