"""WB import wizard, actual quantification/layout and exposure review in Chromium."""
from __future__ import annotations

import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import tifffile
import uvicorn
from playwright.sync_api import sync_playwright
from workbench.server import create_app


def image_bytes(saturated=False, target_multiplier=1.0):
    width, height = 640, 300
    y, x = np.mgrid[:height, :width]
    values = np.full((height, width), 48000, dtype=float)
    for index, center in enumerate([60, 160, 260, 360, 460, 560]):
        amplitude = (10000 + index * 500) * (1.8 if saturated else target_multiplier)
        values -= amplitude * np.exp(-((x-center)/22)**6-((y-85)/5)**2)
        # Distinct loading-control signals make a swapped sample detectable.
        control_amplitude = 12000 + index * 1500
        values -= control_amplitude * np.exp(-((x-center)/22)**6-((y-215)/5)**2)
    values += (x % 3 - 1) * 12
    values = values.astype(np.uint16)
    if saturated:
        values[85, 60] = 0
    stream = BytesIO()
    tifffile.imwrite(stream, values, photometric='minisblack')
    return stream.getvalue()


def assert_wb_values(record, originals, *, target_source, control_source,
                     target_exposure='1s', control_exposure='1s'):
    """Independent oracle for this six-sample, 70 x 28 px synthetic experiment.

    The selected business rule is signed dark-band integral after the median
    of the outer 10% border: 3 top/bottom rows and 7 left/right columns here.
    No production measurement, normalization or grouping helper is used.
    """
    assert record['params']['background'] == 'border'
    assert record['params']['polarity'] == 'dark'
    assert not record['params'].get('control_group')  # Values are target/control, not fold change.
    sources = {}
    for asset_id, data in originals.items():
        assert hashlib.sha256(data).hexdigest() == asset_id
        sources[asset_id] = tifffile.imread(BytesIO(data))
    specs = {roi['id']: roi for roi in record['params']['rois']}
    rows = {row['id']: row for row in record['result']['rows']}
    assert len(rows) == len(record['result']['rows']) == len(specs)
    assert rows.keys() == specs.keys()
    integrals = {}
    for roi_id, roi in specs.items():
        row = rows[roi_id]
        index = int(roi['sample_id'][1:])
        assert 0 <= index < 6 and roi['sample_id'] == f'S{index}'
        role = roi['role']
        assert role in {'target', 'control'}
        assert row['sample_id'] == roi['sample_id']
        assert row['group'] == roi['group'] == ('Control' if index < 3 else 'Treated')
        assert row['biological_replicate'] == roi['biological_replicate'] == f'bio{index}'
        assert row['technical_replicate'] == '1'
        assert row['experiment_id'] == 'synthetic-experiment'
        assert row['role'] == role and row['protein'] == ('Target' if role == 'target' else 'ACTB')
        assert row['source'] == roi['source']
        assert all(roi['source'][key] == 0 for key in ('series', 'frame', 'channel'))
        x, y, width, height = (roi[key] for key in ('x', 'y', 'width', 'height'))
        assert (width, height) == (70, 28)
        assert abs(x + width / 2 - (60 + 100 * index)) <= 2
        assert abs(y + height / 2 - (85 if role == 'target' else 215)) <= 2
        pixels = sources[roi['source']['asset_id']][y:y + height, x:x + width]
        assert pixels.shape == (28, 70) and pixels.dtype == np.uint16
        border = np.concatenate((pixels[:3].ravel(), pixels[-3:].ravel(),
                                 pixels[3:-3, :7].ravel(), pixels[3:-3, -7:].ravel()))
        baseline = float(np.median(border))
        raw_sum = sum(map(int, pixels.ravel()))
        integrals[roi_id] = baseline * pixels.size - raw_sum
        assert row['pixel_count'] == pixels.size and row['source_bit_depth'] == 16
        np.testing.assert_allclose(
            [row['raw_integrated'], row['raw_mean'], row['background'], row['background_subtracted']],
            [raw_sum, raw_sum / pixels.size, baseline, integrals[roi_id]], rtol=1e-12, atol=1e-9)
        selected_exposure = target_exposure if role == 'target' else control_exposure
        assert row['analysis_included'] is (roi['exposure_id'] == selected_exposure)
        if not row['analysis_included'] or role == 'control':
            assert row['normalized'] is None
    expected = {}
    for index in range(6):
        def selected(role, exposure):
            candidates = [row for row in rows.values() if row['sample_id'] == f'S{index}'
                          and row['role'] == role and row['exposure_id'] == exposure]
            assert len(candidates) == 1
            return candidates[0]
        target, control = selected('target', target_exposure), selected('control', control_exposure)
        assert target['source']['asset_id'] == target_source
        assert control['source']['asset_id'] == control_source
        assert target['control_roi_id'] == control['id']
        assert integrals[control['id']] > 0
        expected[f'bio{index}'] = integrals[target['id']] / integrals[control['id']]
        assert math.isclose(target['normalized'], expected[f'bio{index}'], rel_tol=1e-12)
    biological = {row['biological_replicate']: row for row in record['result']['biological_rows']}
    assert len(biological) == len(record['result']['biological_rows']) == 6
    for identity, expected_value in expected.items():
        assert biological[identity]['n_technical'] == 1
        assert math.isclose(biological[identity]['value'], expected_value, rel_tol=1e-12)
    groups = {group['group']: group for group in record['result']['groups']}
    assert set(groups) == {'Control', 'Treated'} and len(record['result']['groups']) == 2
    expected_groups = {}
    for name, indices in [('Control', range(3)), ('Treated', range(3, 6))]:
        values = [expected[f'bio{index}'] for index in indices]
        group = groups[name]
        assert group['metric'] == 'normalized' and group['n_biological'] == group['n_technical'] == 3
        np.testing.assert_allclose(sorted(group['values']), sorted(values), rtol=1e-12)
        np.testing.assert_allclose([group['mean'], group['sd'], group['sem']],
                                  [statistics.mean(values), statistics.stdev(values),
                                   statistics.stdev(values) / math.sqrt(3)], rtol=1e-12)
        expected_groups['Target\n' + name] = values
    return expected_groups


def assert_wb_chart(svg, record, expected_groups):
    document = ET.fromstring(svg)
    description = document.find('.//{http://purl.org/dc/elements/1.1/}description')
    assert description is not None and description.text
    metadata = json.loads(description.text)
    assert metadata['kind'] == 'wb' and metadata['chart_type'] == 'scatter'
    assert metadata['provenance']['measurement_id'] == record['id']
    assert set(metadata['provenance']['source_asset_ids']) == set(record['source_asset_ids'])
    groups = metadata['plot_groups']
    assert {group['label'] for group in groups} == set(expected_groups) and len(groups) == 2
    namespace = '{http://www.w3.org/2000/svg}'
    plotted = [element for element in document.iter(namespace + 'g')
               if element.get('id', '').startswith('PathCollection_')]
    assert len(plotted) == 2
    values, y_positions = [], []
    for group, collection in zip(groups, plotted):
        np.testing.assert_allclose(sorted(group['values']), sorted(expected_groups[group['label']]), rtol=1e-12)
        points = collection.findall('.//' + namespace + 'use')
        assert len(points) == len(group['values']) == 3
        values.extend(group['values'])
        y_positions.extend(float(point.get('y')) for point in points)
    # Verify the actual six scatter marks, not only their descriptive metadata.
    low, high = int(np.argmin(values)), int(np.argmax(values))
    slope = (y_positions[high] - y_positions[low]) / (values[high] - values[low])
    assert slope < 0
    np.testing.assert_allclose(y_positions, [y_positions[low] + slope * (value - values[low])
                                          for value in values], rtol=0, atol=2e-5)


def main():
    output = Path(os.environ.get('FW_BROWSER_RESULTS', ROOT / 'test-results-wb'))
    output.mkdir(parents=True, exist_ok=True)
    report = {'synthetic_data': True, 'scenarios': [], 'browser_errors': [], 'passed': False}
    def checked(name, started):
        report['scenarios'].append({'name': name, 'passed': True, 'elapsed_ms': round((time.perf_counter()-started)*1000, 2)})
        print('PASS', name, flush=True)
    with tempfile.TemporaryDirectory(prefix='figure-wb-browser-') as directory:
        from run import reserve_port
        listener = reserve_port(0)
        port = listener.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        server = uvicorn.Server(uvicorn.Config(create_app(directory), host='127.0.0.1', port=port, log_level='error'))
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        for _ in range(200):
            if server.started:
                break
            time.sleep(.025)
        if not server.started:
            raise RuntimeError('Server did not start')
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, executable_path=os.environ.get('BROWSER_EXECUTABLE'))
                context = browser.new_context(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
                context.route('**/*', lambda route: route.continue_() if route.request.url.startswith((base+'/', 'data:', 'blob:')) else route.abort())
                page = context.new_page()
                page.set_default_timeout(15000)
                page.on('pageerror', lambda error: report['browser_errors'].append(str(error)))
                def state_value(expression):
                    return page.evaluate('async ()=>{const {state,analysisSelection}=await import("/modules/state.js");return ('+expression+')}')
                def wait_state(expression, seconds=25):
                    deadline = time.monotonic()+seconds
                    while time.monotonic() < deadline:
                        if state_value(expression):
                            return
                        page.wait_for_timeout(50)
                    raise AssertionError('Timed out: '+expression+'; toasts='+str(page.locator('#toast-region').inner_text()))
                def click(action, extra=''):
                    page.locator(f'[data-action="{action}"]{extra}').first.click()
                def change(selector, value):
                    page.locator(selector).fill(str(value))
                    page.locator(selector).press('Tab')
                def check_chart(record, expected_groups):
                    panels = state_value('state.project.panels.filter(panel=>panel.analysis_source?.measurement_id==='
                                         + json.dumps(record['id']) + ')')
                    assert len(panels) == 1
                    response = page.request.get(base + '/api/assets/' + panels[0]['asset_id'] + '/original')
                    assert response.ok
                    assert_wb_chart(response.body(), record, expected_groups)
                def configure(pair, quantitative):
                    row_ids = state_value('state.modal.draft.rows.filter(row=>row.coverage===6).map(row=>row.id)')
                    assert len(row_ids) == 2, state_value('state.modal.draft.rows')
                    for row_id, role, protein in zip(row_ids, ['target', 'control'], ['Target', 'ACTB']):
                        page.locator(f'#wb-row-{row_id}-role').select_option(role)
                        change(f'#wb-row-{row_id}-protein', protein)
                        change(f'#wb-row-{row_id}-width', 70)
                        change(f'#wb-row-{row_id}-height', 28)
                    change('#wb-draft-experiment_id', 'synthetic-experiment')
                    change('#wb-draft-exposure_id', pair)
                    change('#wb-draft-exposure_seconds', 1 if pair == '1s' else 5)
                    page.locator('#wb-paste summary').click()
                    rows = '\n'.join(f'S{i}\t{"Control" if i<3 else "Treated"}\tbio{i}\t1' for i in range(6))
                    change('#wb-paste-text', rows)
                    click('wb-apply-paste')
                    if not quantitative:
                        page.locator('#wb-draft-run_quantification').uncheck()
                    return row_ids
                try:
                    started = time.perf_counter()
                    page.goto(base, wait_until='networkidle')
                    short = image_bytes(False)
                    page.locator('#import-wb-files').set_input_files({'name': 'SYNTHETIC_WB_1s.tif', 'mimeType': 'image/tiff', 'buffer': short})
                    wait_state('state.modal?.type==="wb-workflow"')
                    assert state_value('state.modal.draft.lanes.length') == 6
                    assert state_value('state.modal.draft.rows.every(row=>row.role==="ignore")')
                    assert state_value('state.modal.draft.lanes.every(lane=>lane.biological_replicate==="")')
                    first_asset = state_value('state.selectedAssetId')
                    assert first_asset == hashlib.sha256(short).hexdigest()
                    checked('16-bit WB import automatically localizes candidates without inventing identities', started)

                    started = time.perf_counter()
                    row_ids = configure('1s', True)
                    click('wb-create-draft')
                    assert page.locator('.wb-error').is_visible()
                    assert state_value('state.project.settings.analysis_rois.length') == 0
                    page.locator('#wb-rectangles summary').click()
                    roi_x = f'#wb-rect-{row_ids[0]}-0-x'
                    change(roi_x, 9999)
                    assert page.locator(roi_x).is_visible()
                    change(roi_x, 25)
                    page.locator('#wb-draft-confirmed').check()
                    click('wb-create-draft')
                    wait_state('!state.wbWorking && state.project.measurements.length===1 && state.project.panels.length===3', 40)
                    assert state_value('state.project.settings.analysis_rois.length') == 12
                    assert state_value('state.project.measurements[0].result.groups.every(group=>group.n_biological===3)')
                    assert state_value('state.project.panels.filter(panel=>panel.analysis_source).length') == 1
                    assert state_value('state.project.panels.filter(panel=>panel.wb_provenance).every(panel=>panel.wb_provenance.continuous_source_crop && panel.gel.hidden_lanes.length===0)')
                    assert state_value('state.selectedAssetId') == first_asset
                    first_record = state_value('state.project.measurements[0]')
                    expected_groups = assert_wb_values(first_record, {first_asset: short},
                                                       target_source=first_asset, control_source=first_asset)
                    check_chart(first_record, expected_groups)
                    page.screenshot(path=str(output/'01-wb-figure.png'), full_page=True)
                    checked('editable draft coordinates, explicit confirmation, actual quantification and target/control/chart layout', started)

                    started = time.perf_counter()
                    click('tab', '[data-tab="analysis"]')
                    first_roi = state_value('state.project.settings.analysis_rois.find(roi=>roi.role==="target").id')
                    click('select-roi', f'[data-id="{first_roi}"]')
                    change('#roi-geometry-x', 26)
                    assert state_value('analysisSelection().stale')
                    click('open-wb-workflow')
                    wait_state('state.modal?.type==="wb-workflow"')
                    assert state_value('state.modal.draft.rows.filter(row=>row.role!=="ignore").length') == 2
                    assert state_value('Object.values(state.modal.draft.roi_overrides).some(roi=>roi.x===26)')
                    click('close-modal')
                    click('tab', '[data-tab="analysis"]')
                    click('select-roi', f'[data-id="{first_roi}"]')
                    change('#roi-geometry-x', 25)
                    assert not state_value('analysisSelection().stale')
                    checked('reopening full-source wizard preserves manual ROI geometry and exact analysis freshness', started)

                    started = time.perf_counter()
                    long = image_bytes(True)
                    page.locator('#import-wb-files').set_input_files({'name': 'SYNTHETIC_WB_5s_clipped.tif', 'mimeType': 'image/tiff', 'buffer': long})
                    wait_state('state.modal?.type==="wb-workflow"')
                    assert state_value('state.modal.draft.source.asset_id') == hashlib.sha256(long).hexdigest()
                    configure('5s', False)
                    page.locator('#wb-draft-confirmed').check()
                    click('wb-create-draft')
                    wait_state('!state.wbWorking && state.project.settings.analysis_rois.length===24')
                    assert state_value('state.project.panels.length') == 5
                    assert state_value('state.project.measurements.length') == 1
                    click('tab', '[data-tab="analysis"]')
                    click('open-wb-exposures')
                    change('#wb-exposure-adc_bits', 16)
                    click('wb-review-exposures')
                    wait_state('!!state.modal?.review && !state.modal.loading')
                    assert state_value('state.modal.review.groups.length') == 1
                    assert state_value('state.modal.review.groups[0].recommended_pair_id') == '1s', state_value('state.modal.review')
                    assert state_value('state.modal.review.rows.some(row=>row.reasons.includes("saturated_pixels"))')
                    assert state_value('state.project.settings.analysis_rois.every(roi=>roi.included!==false)')
                    page.screenshot(path=str(output/'02-exposure-review.png'), full_page=True)
                    click('wb-apply-exposures')
                    wait_state('state.modal===null')
                    assert state_value('state.project.settings.analysis_rois.filter(roi=>roi.included).length') == 12
                    assert state_value('state.project.settings.analysis_rois.filter(roi=>roi.included).every(roi=>roi.exposure_id==="1s")')
                    assert state_value('state.analysisParams.wb_draft_scope_id') == ''
                    checked('complete exposure-cohort review rejects raw clipping and changes inclusion only after apply', started)

                    started = time.perf_counter()
                    click('undo')
                    assert state_value('state.project.settings.analysis_rois.every(roi=>roi.included!==false)')
                    click('redo')
                    assert state_value('state.project.settings.analysis_rois.filter(roi=>roi.included).length') == 12
                    previous = state_value('state.job?.id || ""')
                    click('run-analysis')
                    wait_state('state.job?.id!=='+json.dumps(previous)+' && state.job?.status==="done"')
                    wait_state('analysisSelection().persisted && !analysisSelection().stale')
                    assert state_value('analysisSelection().result.groups.every(group=>group.n_biological===3)')
                    assert state_value('analysisSelection().result.biological_rows.length') == 6
                    assert state_value('analysisSelection().result.rows.filter(row=>row.role==="target" && row.analysis_included).every(row=>row.normalized!==null)')
                    recalculated = state_value('analysisSelection().record')
                    assert_wb_values(recalculated, {first_asset: short, hashlib.sha256(long).hexdigest(): long},
                                     target_source=first_asset, control_source=first_asset)
                    checked('exposure apply undo/redo and recalculation keep true biological n with paired controls', started)

                    started = time.perf_counter()
                    wait_state('!state.dirty && state.saveState==="saved"')
                    with page.expect_download() as download_info:
                        click('save-project')
                    archive = output/'wb-workflow.figwork'
                    download_info.value.save_as(archive)
                    wait_state('!state.busy')
                    page.locator('#open-project').set_input_files(str(archive))
                    wait_state("state.modal?.type==='switch-project' && !state.busy")
                    assert state_value("state.project.assets.length>0"), 'Current workspace must remain intact before confirmation'
                    page.locator('[data-action="confirm-project-switch"]').click()
                    wait_state('!state.busy && state.project.settings.analysis_rois.length===24')
                    assert state_value('Object.keys(state.project.settings.wb_drafts).length') == 2
                    assert state_value('state.project.settings.wb_exposure_reviews.length') == 1
                    assert state_value('state.project.panels.length') == 5
                    response = page.request.get(base+f'/api/assets/{first_asset}/original')
                    assert response.ok and response.body() == short
                    assert not report['browser_errors'], report['browser_errors']
                    checked('portable project roundtrip retains draft/quality recipes and exact original TIFF bytes', started)

                    started = time.perf_counter()
                    click('project-menu')
                    click('new-dialog')
                    change('#new-project-title', 'Synthetic cross-source WB')
                    click('create-project')
                    wait_state('!state.busy && state.project.assets.length===0')
                    page.locator('#import-wb-files').set_input_files({'name': 'SYNTHETIC_control_source.tif', 'mimeType': 'image/tiff', 'buffer': short})
                    wait_state('state.modal?.type==="wb-workflow"')
                    control_rows = configure('control-1s', False)
                    page.locator(f'#wb-row-{control_rows[0]}-role').select_option('ignore')
                    page.locator('#wb-draft-confirmed').check()
                    click('wb-create-draft')
                    wait_state('!state.wbWorking && state.project.settings.analysis_rois.length===6')
                    control_draft_id = state_value('Object.keys(state.project.settings.wb_drafts)[0]')
                    assert state_value('state.project.settings.analysis_rois.every(roi=>roi.role==="control")')
                    separate_target = image_bytes(False, 1.25)
                    page.locator('#import-wb-files').set_input_files({'name': 'SYNTHETIC_target_source.tif', 'mimeType': 'image/tiff', 'buffer': separate_target})
                    wait_state('state.modal?.type==="wb-workflow"')
                    target_rows = configure('target-1s', True)
                    page.locator(f'#wb-row-{target_rows[1]}-role').select_option('ignore')
                    page.locator('#wb-draft-control_mode').select_option('existing')
                    page.locator('#wb-draft-control_draft_id').select_option(control_draft_id)
                    page.locator('#wb-draft-confirmed').check()
                    click('wb-create-draft')
                    wait_state('!state.wbWorking && state.project.measurements.length===1', 40)
                    assert state_value('state.project.settings.analysis_rois.length') == 12
                    record = state_value('state.project.measurements[0]')
                    assert len(record['params']['rois']) == 12
                    assert all(row['normalized'] is not None for row in record['result']['rows'] if row['role'] == 'target')
                    assert len(record['source_asset_ids']) == 2
                    assert all(group['n_biological'] == 3 for group in record['result']['groups'])
                    expected_groups = assert_wb_values(record, {
                        first_asset: short, hashlib.sha256(separate_target).hexdigest(): separate_target,
                    }, target_source=hashlib.sha256(separate_target).hexdigest(), control_source=first_asset,
                        target_exposure='target-1s', control_exposure='control-1s')
                    check_chart(record, expected_groups)
                    draft_id = state_value('state.analysisParams.wb_draft_scope_id')
                    panels = state_value('state.project.panels.filter(panel=>panel.wb_draft_id==='+json.dumps(draft_id)+')')
                    assert len(panels) == 3
                    image_panels = [panel for panel in panels if panel.get('wb_provenance')]
                    assert len({panel['asset_id'] for panel in image_panels}) == 2
                    assert all(panel['wb_provenance']['continuous_source_crop'] for panel in image_panels)
                    assert not report['browser_errors'], report['browser_errors']
                    checked('separate target/control source files pair explicit identities and produce one real linked figure', started)
                    report['passed'] = True
                except Exception as error:
                    report['failure'] = str(error)
                    report['state_on_failure'] = state_value('({asset:state.selectedAssetId,pointer:state.sourcePointerIdentity,modal:state.modal?.type,error:state.modal?.error,roi_count:state.project?.settings.analysis_rois.length,panel_count:state.project?.panels.length,job:state.job})')
                    page.screenshot(path=str(output/'failure.png'), full_page=True)
                    raise
                finally:
                    (output/'browser-wb-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
                    browser.close()
        finally:
            server.should_exit = True
            thread.join(5)
            listener.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
