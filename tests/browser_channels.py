"""Real browser regression for multichannel editing and original-pixel analysis."""
from __future__ import annotations
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
from PIL import Image
import tifffile
import uvicorn
from playwright.sync_api import sync_playwright
from workbench.server import create_app


def fixtures():
    t, c, z, y, x = np.indices((2, 4, 2, 128, 192))
    raw = (1000 * t + 150 * z + 230 * c + x + y +
           3000 * np.exp(-((x - 75 - c * 10) ** 2 + (y - 60) ** 2) / 300)).astype(np.uint16)
    stream = BytesIO()
    tifffile.imwrite(stream, raw, photometric='minisblack', metadata={'axes': 'TCZYX'}, compression='deflate')
    other = BytesIO()
    Image.fromarray(np.indices((128, 192))[1].astype(np.uint8)).save(other, format='PNG')
    wrong = BytesIO()
    Image.fromarray(np.ones((60, 70), np.uint8)).save(wrong, format='PNG')
    return raw, [
        {'name': 'SYNTHETIC_TZ_4channels.tif', 'mimeType': 'image/tiff', 'buffer': stream.getvalue()},
        {'name': 'SYNTHETIC_separate_8bit.png', 'mimeType': 'image/png', 'buffer': other.getvalue()},
        {'name': 'SYNTHETIC_wrong_geometry.png', 'mimeType': 'image/png', 'buffer': wrong.getvalue()},
    ]


def main():
    output = Path(os.environ.get('FW_BROWSER_RESULTS', ROOT / 'test-results-channels'))
    output.mkdir(parents=True, exist_ok=True)
    report = {'synthetic_data': True, 'scenarios': [], 'browser_errors': [], 'passed': False}
    def checked(name, started):
        report['scenarios'].append({'name': name, 'passed': True, 'elapsed_ms': round((time.perf_counter() - started) * 1000, 2)})
        print('PASS', name, flush=True)
    raw, uploads = fixtures()
    with tempfile.TemporaryDirectory(prefix='figure-browser-channels-') as directory:
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
        try:
            with sync_playwright() as playwright:
                options = {'headless': True}
                if os.environ.get('BROWSER_EXECUTABLE'):
                    options['executable_path'] = os.environ['BROWSER_EXECUTABLE']
                browser = playwright.chromium.launch(**options)
                context = browser.new_context(viewport={'width': 1600, 'height': 1100}, accept_downloads=True)
                context.route('**/*', lambda route: route.continue_() if route.request.url.startswith((base + '/', 'data:', 'blob:')) else route.abort())
                page = context.new_page()
                page.set_default_timeout(20000)
                page.on('pageerror', lambda error: report['browser_errors'].append(str(error)))
                def value(expression):
                    return page.evaluate('async()=>{const {state,analysisSelection,sourceChannelView,analysisGeometry,selectedAnalysisChannels}=await import("/modules/state.js");return (' + expression + ')}')
                def wait(expression):
                    deadline = time.monotonic() + 25
                    while time.monotonic() < deadline:
                        if value(expression):
                            return
                        page.wait_for_timeout(40)
                    raise AssertionError('Timed out: ' + expression)
                def click(action, extra=''):
                    page.locator(f'[data-action="{action}"]{extra}').first.click()
                def change(selector, new):
                    page.locator(selector).fill(str(new))
                    page.locator(selector).press('Tab')
                def choose(selector, option):
                    page.locator(selector).select_option(option)
                def saved():
                    wait('!state.dirty && state.saveState === "saved"')
                def source_point(selector, x, y):
                    locator = page.locator(selector).first
                    locator.scroll_into_view_if_needed()
                    return locator.evaluate('(svg,p)=>{const point=new DOMPoint(...p).matrixTransform(svg.getScreenCTM());return {x:point.x,y:point.y}}', [x, y])
                def drag_source(selector, x, y, x2, y2):
                    start = source_point(selector, x, y)
                    end = source_point(selector, x2, y2)
                    page.mouse.move(start['x'], start['y'])
                    page.mouse.down()
                    page.mouse.move(end['x'], end['y'], steps=8)
                    page.mouse.up()
                def job():
                    previous = value('state.job?.id || ""')
                    click('run-analysis')
                    wait('state.job && state.job.id !== ' + json.dumps(previous) + ' && ["done","error"].includes(state.job.status)')
                    assert value('state.job.status') == 'done', value('state.job')
                    wait('analysisSelection().persisted && analysisSelection().record.id === state.job.id')
                    return value('analysisSelection().result')
                try:
                    started = time.perf_counter()
                    page.goto(base, wait_until='networkidle')
                    page.locator('#import-files').set_input_files(uploads)
                    wait('state.project.assets.length===3')
                    ids = value('state.project.assets.map(asset=>asset.id)')
                    click('select-asset', f'[data-id="{ids[0]}"]')
                    click('tab', '[data-tab="channels"]')
                    page.locator('#channel-c0-name').wait_for()
                    change('#channel-c0-name', 'DAPI 核')
                    change('#channel-c0-gamma', 1.4)
                    change('#channel-frame', 3)
                    wait('sourceChannelView().channels.every(channel=>channel.source.frame===3)')
                    assert value('sourceChannelView().channels[0].name') == 'DAPI 核'
                    page.locator('#channel-c3-visible').uncheck()
                    click('channel-mode', '[data-mode="split"]')
                    page.locator('#channel-include-merge').check()
                    wait('sourceChannelView().mode==="split"')
                    assert page.locator('[data-source-canvas]').count() == 4
                    saved()
                    page.wait_for_timeout(500)
                    assert page.locator('.preview-error').count() == 0
                    page.screenshot(path=str(output / '01-multichannel-split.png'))
                    checked('TCZYX import, frame identity, per-channel edits and real split display', started)

                    started = time.perf_counter()
                    click('tab', '[data-tab="analysis"]')
                    click('analysis-kind', '[data-kind="microscopy"]')
                    choose('#analysis-method', 'channel_intensity')
                    page.locator('#analysis-raw-c1').check()
                    click('source-tool', '[data-tool="roi"]')
                    drag_source('[data-source-canvas][data-channel-id="c1"]', 20, 25, 100, 85)
                    wait('!!analysisGeometry().roi')
                    roi = value('analysisGeometry().roi')
                    assert (roi['x'], roi['y'], roi['width'], roi['height']) == (20, 25, 80, 60), roi
                    assert value('state.project.settings.analysis_rois.length') == 0
                    result = job()
                    assert len(result['rows']) == 2
                    for row in result['rows']:
                        channel = row['source']['channel']
                        assert row['raw_mean'] == float(np.mean(raw[1, channel, 1, 25:85, 20:100]))
                        assert row['source']['frame'] == 3
                    original_record = value('analysisSelection().record.id')
                    saved()
                    checked('Split tile ROI uses original coordinates, two raw channels, no WB ROI contamination', started)

                    started = time.perf_counter()
                    click('tab', '[data-tab="channels"]')
                    change('#channel-c0-gamma', 2)
                    page.locator('#channel-c0-visible').uncheck()
                    saved()
                    click('tab', '[data-tab="analysis"]')
                    assert not value('analysisSelection().stale')
                    assert value('analysisSelection().record.id') == original_record
                    assert len(value('selectedAnalysisChannels()')) == 2
                    assert value('analysisGeometry().roi.width') == 80
                    click('tab', '[data-tab="channels"]')
                    page.locator('#channel-c0-visible').check()
                    change('#channel-frame', 2)
                    click('tab', '[data-tab="analysis"]')
                    assert value('analysisSelection().stale')
                    assert value('analysisGeometry().roi || null') is None
                    click('tab', '[data-tab="channels"]')
                    change('#channel-frame', 3)
                    click('tab', '[data-tab="analysis"]')
                    assert value('analysisSelection().record.id') == original_record
                    assert not value('analysisSelection().stale')
                    checked('Display edits retain science; frame changes isolate and restore raw ROI/results', started)

                    started = time.perf_counter()
                    choose('#analysis-method', 'threshold')
                    choose('#analysis-threshold_method', 'otsu')
                    click('source-tool', '[data-tool="ellipse"]')
                    drag_source('[data-source-canvas][data-channel-id="c0"]', 30, 30, 130, 100)
                    wait('analysisGeometry().roi?.type==="ellipse"')
                    threshold = job()
                    assert len(threshold['rows']) == 2 and 0 < threshold['summary']['roi_area_px'] < 7000
                    choose('#analysis-method', 'channel_overlap')
                    overlap = job()
                    assert len(overlap['pairs']) == 1
                    assert overlap['pairs'][0]['pearson_r'] is not None
                    page.screenshot(path=str(output / '02-analysis-overlap.png'))
                    checked('Elliptical ROI threshold and real two-channel overlap metrics', started)

                    started = time.perf_counter()
                    choose('#analysis-method', 'line_profile')
                    change('#analysis-samples', 12)
                    click('source-tool', '[data-tool="analysis-line"]')
                    drag_source('[data-source-canvas][data-channel-id="c0"]', 10, 40, 150, 40)
                    wait('!!analysisGeometry().line')
                    profile = job()
                    assert len(profile['profiles']) == 2
                    assert len(profile['profiles'][0]['values']) == 12
                    assert abs(profile['profiles'][0]['distance_px'][-1] - 140) < 1e-6
                    assert page.locator('.profile-figure svg[aria-label="原始通道线强度剖面"]').is_visible()
                    page.screenshot(path=str(output / '03-line-profile.png'))
                    checked('Original-coordinate line gesture, multichannel profile and visible plot', started)

                    started = time.perf_counter()
                    click('tab', '[data-tab="channels"]')
                    click('add-channel-panels')
                    wait('state.project.panels.length===4')
                    assert value('state.project.panels.every(panel=>!!panel.channel_view)')
                    saved()
                    page.screenshot(path=str(output / '04-split-to-composer.png'))
                    with page.expect_download() as download:
                        click('save-project')
                    archive = output / 'multichannel.figwork'
                    download.value.save_as(archive)
                    before_views = value('state.project.panels.map(panel=>panel.channel_view)')
                    page.locator('#open-project').set_input_files(archive)
                    wait("state.modal?.type==='switch-project' && !state.busy")
                    assert value("state.project.assets.length>0"), 'Current workspace must remain intact before confirmation'
                    page.locator('[data-action="confirm-project-switch"]').click()
                    wait('state.project.panels.length===4 && state.saveState==="saved"')
                    assert value('state.project.panels.map(panel=>panel.channel_view)') == before_views
                    assert value('state.project.measurements.length') == 4
                    checked('Split creates editable panels; portable project restores channels, ROIs and measurements', started)

                    started = time.perf_counter()
                    click('select-asset', f'[data-id="{ids[0]}"]')
                    click('tab', '[data-tab="channels"]')
                    click('compose-channels')
                    click('add-composition-source')
                    choose('#composition-1-asset', ids[2])
                    page.locator('#composition-1-channel').wait_for()
                    click('confirm-channel-composition')
                    wait('!!state.modal?.error')
                    assert value('state.modal.type') == 'channel-composition'
                    choose('#composition-1-asset', ids[1])
                    page.locator('#composition-1-channel').wait_for()
                    click('confirm-channel-composition')
                    wait('!state.modal && sourceChannelView().channels.length===2')
                    sources = value('sourceChannelView().channels.map(channel=>channel.source.asset_id)')
                    assert sources == [ids[0], ids[1]]
                    saved()
                    assert value('state.project.assets.length') == 3
                    checked('Different-size sources rejected; uint16 + uint8 combination retains two originals', started)

                    if os.environ.get('FIGURE_WORKBENCH_TEST_CELLPOSE') == '1':
                        started = time.perf_counter()
                        click('tab', '[data-tab="analysis"]')
                        choose('#analysis-method', 'cellpose')
                        page.locator('[data-analysis-channel]').first.check()
                        choose('#analysis-model', 'nuclei')
                        change('#analysis-diameter_px', 25)
                        wait('state.cellposeStatus?.models?.some(model=>model.id==="nuclei" && model.available)')
                        assert page.locator('.model-status.available').is_visible()
                        model_result = job()
                        method = model_result['method']
                        assert method['name'] == 'cellpose'
                        assert method['measurement_basis'] == 'original_pixels'
                        assert method['display_lut_used'] is False
                        assert method['source_channel']['source']['asset_id'] == ids[0]
                        assert method['model_record']['cellpose_version'] == '3.1.1.3'
                        assert method['model_record']['weights_sha256'] == '89ca45e4a45048d5010d29621466b1274b91b3dcb9714dce9f9e90a9a8671303'
                        saved()
                        report['cellpose_cpu'] = {'official_model': 'nuclei', 'candidate_count': model_result['count'],
                                                 'scientific_accuracy_claimed': False}
                        page.screenshot(path=str(output / '05-cellpose-cpu.png'))
                        checked('Actual CPU model status, UI request, isolated inference and persisted original-source result', started)

                    assert not report['browser_errors'], report['browser_errors']
                    report['passed'] = True
                except Exception:
                    page.screenshot(path=str(output / 'FAILED.png'), full_page=True)
                    (output / 'FAILED.html').write_text(page.content(), 'utf-8')
                    (output / 'FAILED-state.json').write_text(json.dumps(value('({tab:state.tab,job:state.job,geometry:analysisGeometry(),selection:analysisSelection(),view:sourceChannelView(),params:state.analysisParams,modal:state.modal})'), ensure_ascii=False, indent=2), 'utf-8')
                    raise
                finally:
                    (output / 'browser-channel-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
                    browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
