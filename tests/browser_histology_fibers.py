"""Real UI checks for IHC calibration and fiber/myotube workflows, synthetic only."""
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
import tifffile
import uvicorn
from playwright.sync_api import sync_playwright
from workbench.server import create_app
from workbench.histology import DEFAULT_VECTORS


def fixtures():
    rng = np.random.default_rng(17)
    h = rng.uniform(.15, 1., (80, 80))
    d = np.repeat(np.array([.05, .3, .65, 1.1]), 20)[None, :] + rng.normal(0, .003, (80, 80))
    uploads = []
    for index, multiplier in enumerate([1., 1.5]):
        rgb = np.exp(-(h[..., None] * DEFAULT_VECTORS[0] + d[..., None] * multiplier * DEFAULT_VECTORS[1])).astype(np.float32)
        buffer = BytesIO();tifffile.imwrite(buffer, rgb, photometric='rgb', metadata={'axes':'YXS'})
        uploads.append({'name':f'SYNTHETIC_IHC_batch_{index+1}.tif','mimeType':'image/tiff','buffer':buffer.getvalue()})
    labels = np.zeros((80, 100), np.uint16)
    labels[10:35, 10:30] = 11;labels[15:45, 55:85] = 22
    raw = np.where(labels, 120, 4).astype(np.uint16)
    nuclear = np.zeros_like(labels)
    nuclear[20:23,18:21] = 1;nuclear[12:15,10:12] = 2;nuclear[25:28,68:71] = 3;nuclear[60:63,10:13] = 4
    marker = np.where(labels==11,200,10).astype(np.uint16)
    buffer = BytesIO();tifffile.imwrite(buffer,np.stack([raw,labels,nuclear,marker]),photometric='minisblack',metadata={'axes':'CYX'})
    uploads.append({'name':'SYNTHETIC_fibers_structure_labels_nuclei_marker.tif','mimeType':'image/tiff','buffer':buffer.getvalue()})
    return uploads


def main():
    output=Path(os.environ.get('FW_BROWSER_RESULTS',ROOT/'test-results-histology'))
    output.mkdir(parents=True,exist_ok=True)
    report={'synthetic_data':True,'scenarios':[],'browser_errors':[],'passed':False}
    def checked(name,started):
        report['scenarios'].append({'name':name,'passed':True,'elapsed_ms':round((time.perf_counter()-started)*1000,2)})
        print('PASS',name,flush=True)
    with tempfile.TemporaryDirectory(prefix='figure-histology-browser-') as directory:
        from run import reserve_port
        listener=reserve_port(0);port=listener.getsockname()[1];base=f'http://127.0.0.1:{port}'
        server=uvicorn.Server(uvicorn.Config(create_app(directory),host='127.0.0.1',port=port,log_level='error'))
        thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
        for _ in range(200):
            if server.started:break
            time.sleep(.025)
        try:
            with sync_playwright() as playwright:
                options={'headless':True}
                if os.environ.get('BROWSER_EXECUTABLE'):options['executable_path']=os.environ['BROWSER_EXECUTABLE']
                browser=playwright.chromium.launch(**options)
                context=browser.new_context(viewport={'width':1600,'height':1100},accept_downloads=True)
                context.route('**/*',lambda route:route.continue_() if route.request.url.startswith((base+'/','data:','blob:')) else route.abort())
                page=context.new_page();page.set_default_timeout(20000)
                page.on('pageerror',lambda error:report['browser_errors'].append(str(error)))
                def value(expression):
                    return page.evaluate('async()=>{const {state,analysisSelection,analysisGeometry,selectedAnalysisChannels}=await import("/modules/state.js");return ('+expression+')}')
                def wait(expression):
                    deadline=time.monotonic()+30
                    while time.monotonic()<deadline:
                        if value(expression):return
                        page.wait_for_timeout(40)
                    raise AssertionError('Timed out: '+expression+'; job='+str(value('state.job')))
                def click(action,extra=''):page.locator(f'[data-action="{action}"]{extra}').first.click()
                def choose(selector,option):page.locator(selector).select_option(option)
                def change(selector,new):page.locator(selector).fill(str(new));page.locator(selector).press('Tab')
                def saved():wait('!state.dirty && state.saveState === "saved"')
                def drag(x,y,x2,y2):
                    svg=page.locator('[data-source-canvas]').first;svg.scroll_into_view_if_needed()
                    def point(px,py):return svg.evaluate('(svg,p)=>{const a=new DOMPoint(...p).matrixTransform(svg.getScreenCTM());return {x:a.x,y:a.y}}',[px,py])
                    a,b=point(x,y),point(x2,y2)
                    page.mouse.move(a['x'],a['y']);page.mouse.down();page.mouse.move(b['x'],b['y'],steps=8);page.mouse.up()
                def job():
                    previous=value('state.job?.id || ""');click('run-analysis')
                    wait('state.job && state.job.id !== '+json.dumps(previous)+' && ["done","error"].includes(state.job.status)')
                    assert value('state.job.status')=='done',value('state.job')
                    wait('analysisSelection().persisted && analysisSelection().record.id === state.job.id')
                    assert not value('analysisSelection().stale'),value('analysisSelection().input')
                    return value('analysisSelection().result')
                try:
                    started=time.perf_counter();page.goto(base,wait_until='networkidle')
                    page.locator('#import-files').set_input_files(fixtures());wait('state.project.assets.length===3')
                    ids=value('state.project.assets.map(asset=>asset.id)')
                    click('select-asset',f'[data-id="{ids[0]}"]');click('tab','[data-tab="analysis"]');click('analysis-kind','[data-kind="microscopy"]')
                    choose('#analysis-method','ihc_advanced');choose('#analysis-stain_method','macenko')
                    choose('#analysis-grading_method','gmm');result=job()
                    assert result['method']['stain_estimation']['algorithm']=='macenko_angular_extremes'
                    assert result['method']['grading']['components']==4
                    assert result['summary']['pixel_weighted_score']==150
                    assert page.locator('.ihc-grade-map').count()==1
                    checked('True RGB import, Macenko estimate, supported four-group GMM and grade map',started)

                    started=time.perf_counter();choose('#analysis-stain_method','fixed');choose('#analysis-grading_method','manual')
                    change('#analysis-grade_threshold_1',.15);change('#analysis-grade_threshold_2',.5);change('#analysis-grade_threshold_3',.85)
                    manual=job();assert manual['summary']['positive_fraction']==.75
                    choose('#analysis-grading_method','examples')
                    click('ihc-example-region','[data-role="negative"]');drag(2,2,17,22)
                    wait('!!analysisGeometry().ihc_negative')
                    click('ihc-example-region','[data-role="positive"]');drag(22,2,37,22)
                    wait('!!analysisGeometry().ihc_positive')
                    assert not value('analysisGeometry().roi')
                    result=job();thresholds=result['calibration_profile']['grade_thresholds']
                    assert .16<thresholds[0]<.19 and thresholds[1:]==[.5,.85]
                    assert result['method']['grading']['negative_count']==300
                    checked('Manual thresholds and original-coordinate negative/positive example recalibration',started)

                    started=time.perf_counter();click('ihc-lock-profile');saved()
                    profile_id=value('state.analysisParams.locked_profile.id')
                    click('select-asset',f'[data-id="{ids[1]}"]')
                    assert not value('analysisGeometry().ihc_negative')
                    locked=job();assert locked['calibration_profile']['id']==profile_id
                    assert locked['method']['stain_estimation']['algorithm']=='batch_locked'
                    assert locked['calibration_profile']['grade_thresholds']==thresholds
                    assert locked['summary']['mean_dab_od_in_tissue']>result['summary']['mean_dab_od_in_tissue']*1.49
                    assert page.locator('[data-action="ihc-unlock-profile"]').count()==1
                    checked('Locked vectors/whitepoint/thresholds applied to second raw image with separate ROI context',started)

                    started=time.perf_counter();click('select-asset',f'[data-id="{ids[2]}"]');choose('#analysis-method','fiber_cross_section')
                    for channel in ['c1','c2','c3']:page.locator(f'#analysis-raw-{channel}').check()
                    choose('#analysis-segmentation','labels');choose('#analysis-fiber_channel_id','c0');choose('#analysis-label_channel_id','c1')
                    change('#analysis-fiber_min_area',5);change('#analysis-um_per_px',.5)
                    page.locator('#fiber-nuclei-options summary').click();choose('#analysis-nucleus_label_channel_id','c2');change('#analysis-nucleus_min_area',1)
                    page.locator('#fiber-marker-types summary').click();click('fiber-add-marker')
                    choose('#fiber-marker-0-channel','c3');change('#fiber-marker-0-name','MYH-A');change('#fiber-marker-0-threshold',100)
                    cross=job();assert cross['count']==2
                    assert [o['min_feret_px'] for o in cross['objects']]==[20.,30.]
                    assert [o['area_um2'] for o in cross['objects']]==[125.,225.]
                    assert cross['summary']['central_nuclei']==2 and cross['summary']['peripheral_nuclei']==1
                    assert cross['summary']['nuclei_outside']==1
                    assert [o['phenotype'] for o in cross['objects']]==['MYH-A','negative']
                    assert cross['instance_mask']['encoding']=='uint32-le-zlib-base64-v1'
                    checked('Explicit structure/instance/nucleus/marker channels, Feret calibration and nuclear phenotyping',started)

                    started=time.perf_counter();choose('#analysis-method','myotube')
                    assert len(value('selectedAnalysisChannels()'))==4
                    tubes=job();assert tubes['summary']['fusion_index']==.5
                    assert tubes['summary']['fusion_numerator_nuclei']==2 and tubes['summary']['fusion_denominator_nuclei']==4
                    assert all(o['skeleton_length_px']>0 for o in tubes['objects'])
                    page.locator('#fiber-nuclei-table summary').click()
                    with page.expect_download() as download:click('download-results','[data-result-section="nuclei"]')
                    download.value.save_as(output/'nucleus_membership.csv')
                    assert 'normalized_edge_distance_q10' in (output/'nucleus_membership.csv').read_text(encoding='utf-8-sig')
                    saved();page.screenshot(path=str(output/'histology-fiber-workflow.png'))
                    checked('Myotube length/fusion recomputation, full denominator and original per-nucleus CSV',started)
                    assert not report['browser_errors'],report['browser_errors']
                    report['passed']=True
                except Exception as exc:
                    report['error']=str(exc)
                    page.screenshot(path=str(output/'failure.png'))
                    raise
                finally:
                    (output/'browser-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                    browser.close()
        finally:
            server.should_exit=True;thread.join(timeout=5)
    print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
