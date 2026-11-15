"""Real browser workflow. Starts an isolated server and uses only synthetic data.

Run: python tests/browser_workflow.py
Optional: BROWSER_EXECUTABLE=/path/to/chromium (or chrome-headless-shell)
Requires the development dependencies and a Playwright Chromium installation.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from io import BytesIO

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from PIL import Image
import uvicorn
from playwright.sync_api import sync_playwright
from workbench.server import create_app
from workbench.storage import ProjectStore


def main():
    output = Path(os.environ.get('FW_BROWSER_RESULTS', ROOT / 'test-results'))
    output.mkdir(parents=True, exist_ok=True)
    report = {'synthetic_data':True,'scenarios':[],'browser_errors':[],'passed':False}
    def checked(name, started):
        report['scenarios'].append({'name':name,'passed':True,'elapsed_ms':round((time.perf_counter()-started)*1000,2)})
        print('PASS',name,flush=True)

    with tempfile.TemporaryDirectory(prefix='figure-browser-') as directory:
        # Synthetic fixture creation belongs to test code, never a production UI route.
        fixture_store=ProjectStore(Path(directory)/'fixture-source')
        fixture_store.demo()
        fixture_project=Path(directory)/'SYNTHETIC_FIXTURE.figwork'
        fixture_project.write_bytes(fixture_store.export_archive())
        from run import reserve_port
        listener=reserve_port(0)
        port=listener.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        server=uvicorn.Server(uvicorn.Config(create_app(directory),host='127.0.0.1',port=port,log_level='error'))
        thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
        for _ in range(200):
            if server.started:break
            time.sleep(.025)
        if not server.started:raise RuntimeError('Test server did not become ready')
        try:
            with sync_playwright() as playwright:
                options={'headless':True}
                if os.environ.get('BROWSER_EXECUTABLE'):
                    options['executable_path']=os.environ['BROWSER_EXECUTABLE']
                browser=playwright.chromium.launch(**options)
                context=browser.new_context(viewport={'width':1440,'height':1000},device_scale_factor=1,accept_downloads=True)
                context.route('**/*',lambda route:route.continue_() if route.request.url.startswith((base+'/', 'data:', 'blob:')) else route.abort())
                page=context.new_page();page.set_default_timeout(15000)
                page.on('pageerror',lambda error:report['browser_errors'].append(str(error)))
                def state_value(expression):
                    return page.evaluate('async ()=>{const {state,analysisSelection}=await import("/modules/state.js");return ('+expression+')}')
                def wait_state(expression):
                    deadline=time.monotonic()+20
                    while time.monotonic()<deadline:
                        if state_value(expression):return
                        page.wait_for_timeout(40)
                    raise AssertionError('Timed out waiting for application state: '+expression)
                def saved():
                    wait_state('!state.dirty && state.saveState === "saved"')
                def click(action, extra=''):
                    page.locator(f'[data-action="{action}"]{extra}').first.click()
                def change(selector,value):
                    page.locator(selector).fill(str(value));page.locator(selector).press('Tab')
                def source_point(x,y):
                    return page.locator('#source-svg').evaluate('(svg,p)=>{const point=new DOMPoint(p[0],p[1]).matrixTransform(svg.getScreenCTM());return {x:point.x,y:point.y}}',[x,y])
                def draw_roi(x,y,width,height):
                    click('source-tool','[data-tool="roi"]')
                    start=source_point(x,y);end=source_point(x+width,y+height)
                    page.mouse.move(start['x'],start['y']);page.mouse.down();page.mouse.move(end['x'],end['y'],steps=8);page.mouse.up()
                    page.locator('.selection-bar').wait_for();click('add-roi')
                def run_job():
                    previous=state_value('state.job?.id || ""')
                    click('run-analysis');wait_state('state.job && state.job.id !== '+json.dumps(previous)+' && ["done","error"].includes(state.job.status)')
                    assert state_value('state.job.status')=='done',state_value('state.job')
                    wait_state('analysisSelection().persisted && analysisSelection().record.id === state.job.id')
                    return state_value('analysisSelection().result')
                try:
                    started=time.perf_counter()
                    page.goto(base,wait_until='networkidle')
                    assert page.title().startswith('图研')
                    page.screenshot(path=str(output/'01-empty.png'))
                    assert page.locator('[data-action="demo"]').count()==0
                    page.locator('#open-project').set_input_files(fixture_project)
                    page.locator('#figure-svg').wait_for();wait_state('state.project.assets.length===3 && !state.busy')
                    assert page.locator('.figure-panel').count()==2
                    checked('empty workspace and explicit synthetic fixture import',started)

                    started=time.perf_counter()
                    click('select-panel')
                    wait_state('state.selectedPanelId===state.project.panels[0].id && !state.busy')
                    before=state_value('state.project.panels[0].x_mm')
                    panel_id=state_value('state.project.panels[0].id')
                    hit=page.locator(f'[data-panel-id="{panel_id}"] > .panel-hit')
                    # Autosave/font completion may replace idle SVG nodes. Use
                    # Playwright's visible/stable/detached-node retry instead of
                    # dereferencing a one-shot bounding box on an old handle.
                    # This still performs actual pointer down/move/up on canvas.
                    hit.drag_to(hit,source_position={'x':30,'y':30},target_position={'x':75,'y':45})
                    assert state_value('state.project.panels[0].x_mm')>before
                    click('undo');assert abs(state_value('state.project.panels[0].x_mm')-before)<.001
                    click('redo');assert state_value('state.project.panels[0].x_mm')>before
                    click('undo');saved()
                    change('[data-scope="panel"][data-field="rotation"]',12)
                    click('flip-panel','[data-direction="h"]');saved()
                    page.screenshot(path=str(output/'02-composer.png'))
                    # Input focus must not redirect Delete to the canvas.
                    page.locator('[data-scope="panel"][data-field="title"]').fill('DELETE INPUT TEST')
                    page.locator('[data-scope="panel"][data-field="title"]').press('Control+A')
                    page.locator('[data-scope="panel"][data-field="title"]').press('Delete')
                    page.locator('[data-scope="panel"][data-field="title"]').press('Tab')
                    assert state_value('state.project.panels.length')==2
                    page.locator('[data-action="select-panel"]').nth(1).click()
                    assert not page.locator('.panel-scale-bar').count()
                    change('#scale-bar-um_per_px',.5);change('#scale-bar-length_um',20)
                    page.locator('#scale-bar-visible').check()
                    assert page.locator('.panel-scale-bar').count()==1
                    page.locator('#scale-bar-visible').uncheck()
                    assert page.locator('.panel-scale-bar').count()==0
                    page.locator('#scale-bar-visible').check();saved()
                    checked('drag, undo/redo, transform, keyboard focus',started)

                    started=time.perf_counter()
                    click('select-asset');click('tab','[data-tab="gel"]')
                    change('#lane-count',6);click('detect-gel')
                    wait_state('!state.gelBusy && !!state.gelSuggestions')
                    assert page.locator('.lane-row').count()==6
                    assert state_value('state.gelSuggestions.bands.length')==15
                    page.locator('#gel-marker-tools > summary').click()
                    for y,size in [(60,100),(150,50)]:
                        click('source-tool','[data-tool="marker"]');point=source_point(70,y);page.mouse.click(point['x'],point['y'])
                        change('#new-marker-size',size);click('confirm-marker')
                    assert page.locator('.marker-row').count()==2
                    page.screenshot(path=str(output/'03-gel.png'))
                    checked('binary worker localization and manual marker calibration',started)

                    started=time.perf_counter()
                    click('tab','[data-tab="analysis"]');draw_roi(140,95,60,30)
                    roi_id=state_value('state.selectedRoiId')
                    for key,value in [('sample_id','sample-1'),('group','Control'),('biological_replicate','bio-1'),('technical_replicate','1'),('protein','Target')]:
                        change(f'#roi-{roi_id}-{key}',value)
                    draw_roi(140,240,60,30);control_id=state_value('state.selectedRoiId')
                    page.locator(f'#roi-{control_id}-role').select_option('control')
                    for key,value in [('sample_id','sample-1'),('group','Control'),('biological_replicate','bio-1'),('technical_replicate','1'),('protein','ACTB')]:
                        change(f'#roi-{control_id}-{key}',value)
                    result=run_job()
                    assert len(result['rows'])==2 and result['groups'][0]['n_biological']==1
                    assert result['groups'][0]['sd'] is None
                    with page.expect_download() as download_info:click('download-results')
                    download_info.value.save_as(output/'wb-results.csv')
                    assert not state_value('state.resultStale')
                    page.locator('#analysis-background').select_option('none')
                    assert state_value('state.resultStale')
                    assert page.locator('[data-action="plot-results"]').is_disabled()
                    click('undo')
                    assert state_value('state.analysisParams.background')=='border'
                    assert not state_value('state.resultStale')
                    click('redo');assert state_value('state.resultStale')
                    click('undo');assert not state_value('state.resultStale')
                    page.locator('.asset-card').nth(1).click()
                    assert state_value('analysisSelection().result === null')
                    assert page.locator('.result-empty').is_visible()
                    page.locator('.asset-card').first.click()
                    assert state_value('analysisSelection().persisted && !analysisSelection().stale')
                    click('tab','[data-tab="compose"]');click('select-panel')
                    change('[data-scope="panel"][data-field="title"]','WB 定量')
                    click('tab','[data-tab="analysis"]')
                    assert state_value('analysisSelection().persisted && !analysisSelection().stale')
                    checked('WB ROI identities, n=1, CSV, result invalidation and source isolation',started)

                    started=time.perf_counter()
                    click('analysis-kind','[data-kind="qpcr"]')
                    change('#analysis-reference_gene','ACTB');change('#analysis-control_group','Control')
                    result=run_job()
                    assert len(result['groups'])==2 and all(group['n_biological']==3 for group in result['groups'])
                    page.locator('#analysis-chart-type').select_option('box')
                    assert state_value('state.project.settings.analysis_chart_type')=='box'
                    assert state_value('analysisSelection().persisted && !analysisSelection().stale')
                    chart_measurement_id=state_value('analysisSelection().record.id')
                    page.screenshot(path=str(output/'04-qpcr.png'))
                    click('plot-results');wait_state('state.project.panels.length===3 && state.tab==="compose"');saved()
                    assert state_value('state.project.assets.length')==4
                    assert state_value('state.project.panels.at(-1).analysis_source.measurement_id')==chart_measurement_id
                    checked('qPCR technical aggregation and actual-data vector chart',started)

                    started=time.perf_counter()
                    click('tab','[data-tab="analysis"]');click('analysis-kind','[data-kind="microscopy"]')
                    page.locator('.asset-card').nth(1).click()
                    result=run_job()
                    assert result['count']==9
                    page.screenshot(path=str(output/'05-microscopy.png'))
                    checked('microscopy CPU segmentation and source overlay',started)

                    started=time.perf_counter()
                    click('tab','[data-tab="compose"]');click('preflight');wait_state('!!state.preflight')
                    assert any('ppi' in str(issue).lower() or '分辨率' in str(issue) for issue in state_value('state.preflight.issues'))
                    for fmt in ['pdf','svg','png','tiff','pptx']:
                        click('export-dialog');page.locator(f'label.export-format:has([data-export-format][value="{fmt}"])').click()
                        with page.expect_download(timeout=30000) as download_info:click('export-file')
                        download=download_info.value;download.save_as(output/f'figure.{fmt}')
                        assert (output/f'figure.{fmt}').stat().st_size>1000
                        wait_state('!state.busy && !state.modal')
                    with page.expect_download(timeout=30000) as download_info:click('save-project')
                    archive=output/'roundtrip.figwork';download_info.value.save_as(archive)
                    wait_state('!state.busy')
                    page.locator('#open-project').set_input_files(archive)
                    wait_state("state.modal?.type==='switch-project' && !state.busy")
                    assert state_value("state.project.assets.length>0"), 'Current workspace must remain intact before confirmation'
                    page.locator('[data-action="confirm-project-switch"]').click()
                    wait_state('!state.busy && state.project.assets.length===4')
                    assert state_value('state.project.measurements.length')==3
                    assert state_value('state.project.panels[1].scale_bar.length_um')==20
                    checked('source PPI checks, five file exports, complete project reopen',started)

                    started=time.perf_counter()
                    buffer=BytesIO();Image.fromarray(np.arange(128*128,dtype=np.uint16).reshape(128,128)).save(buffer,'PNG')
                    page.locator('#import-files').set_input_files({'name':'new-16bit.png','mimeType':'image/png','buffer':buffer.getvalue()})
                    wait_state('!state.busy && state.project.assets.length===5')
                    assert state_value('state.project.assets.at(-1).bit_depth')==16
                    click('tab','[data-tab="analysis"]');click('analysis-kind','[data-kind="microscopy"]')
                    assert state_value('analysisSelection().result === null')
                    page.set_viewport_size({'width':1280,'height':800})
                    page.wait_for_load_state('networkidle');page.wait_for_timeout(250)
                    page.screenshot(path=str(output/'06-1280-desktop.png'))
                    assert page.locator('[data-action="export-dialog"]').is_visible()
                    checked('16-bit file picker import and compact desktop layout',started)
                    assert not report['browser_errors'],report['browser_errors']
                    report['passed']=True
                except Exception as error:
                    report['failure']=str(error)
                    try:
                        page.screenshot(path=str(output/'failure.png'),full_page=True)
                        report['failure_state']=state_value('({tab:state.tab,selectedPanelId:state.selectedPanelId,selectedPanelIds:state.selectedPanelIds,busy:state.busy,saveState:state.saveState,dirty:state.dirty,interactionLock:state.interactionLock,formPointerPending:state.formPointerPending,revision:state.project?.revision,lastError:state.lastError})')
                        report['failure_panels']=page.locator('.figure-panel').evaluate_all('(panels)=>panels.map(panel=>({id:panel.dataset.panelId,connected:panel.isConnected,rect:panel.getBoundingClientRect().toJSON()}))')
                    except Exception as capture_error:
                        report['capture_error']=str(capture_error)
                    raise
                finally:
                    (output/'browser-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
                    browser.close()
        finally:
            server.should_exit=True;thread.join(5);listener.close()
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
