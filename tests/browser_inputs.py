"""Native input drafts survive forced asynchronous redraws; uses real browser events."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import uvicorn
from playwright.sync_api import sync_playwright
from workbench.server import create_app
from browser_channels import fixtures


def main():
    output = Path(os.environ.get('FW_BROWSER_RESULTS', ROOT / 'test-results-inputs'))
    output.mkdir(parents=True, exist_ok=True)
    report = {'synthetic_data': True, 'scenarios': [], 'browser_errors': [], 'passed': False}
    _, uploads = fixtures()
    with tempfile.TemporaryDirectory(prefix='figure-input-browser-') as directory:
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
                browser = playwright.chromium.launch(headless=True, executable_path=os.environ.get('BROWSER_EXECUTABLE'))
                page = browser.new_page(viewport={'width': 1600, 'height': 1100})
                page.set_default_timeout(15000)
                page.on('pageerror', lambda error: report['browser_errors'].append(str(error)))
                def value(expression):
                    return page.evaluate('async()=>{const {state,sourceChannelView,analysisGeometry}=await import("/modules/state.js");return (' + expression + ')}')
                def wait(expression):
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        if value(expression):
                            return
                        page.wait_for_timeout(25)
                    raise AssertionError('Timed out: ' + expression)
                def redraw_during_draft(selector):
                    retained = page.locator(selector).evaluate('async el=>{const {emit}=await import("/modules/state.js");for(let i=0;i<6;i++){emit();await Promise.resolve();}return el.isConnected && document.activeElement===el}')
                    assert retained, 'A pending native input was replaced by an asynchronous redraw'
                def complete(name):
                    wait('!state.formCommitPending && !state.formPointerPending')
                    report['scenarios'].append({'name': name, 'passed': True})
                    print('PASS', name, flush=True)
                def committed_grid(minimum_gap=None):
                    # Query and measure current nodes in one synchronous page
                    # callback. Locator.evaluate_all can hold an earlier node
                    # array across an intervening save/preview redraw.
                    snapshot = page.wait_for_function('''minimumGap=>{
                      const grid=document.querySelector('.source-multigrid');
                      if(!grid?.isConnected)return false;
                      const elements=[...grid.querySelectorAll('.source-tile')];
                      if(elements.length!==4||elements.some(el=>!el.isConnected))return false;
                      const tiles=elements.map(el=>{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};});
                      if(tiles.some(r=>r.width<=0||r.height<=0))return false;
                      const style=getComputedStyle(grid),gap=tiles[1].x-tiles[0].x-tiles[0].width;
                      if(style.gridTemplateColumns.split(' ').length!==3||Math.abs(tiles[0].y-tiles[2].y)>=1||tiles[3].y<=tiles[0].y)return false;
                      if(minimumGap!==null&&(parseFloat(style.columnGap)<=minimumGap||gap<=minimumGap))return false;
                      return {tiles,gap,computed:{gap:style.columnGap,columns:style.gridTemplateColumns,style:grid.getAttribute('style')}};
                    }''', arg=minimum_gap)
                    try:
                        return snapshot.json_value()
                    finally:
                        snapshot.dispose()
                try:
                    page.goto(base, wait_until='networkidle')
                    page.evaluate('''async()=>{
                      const {state,sourceChannelView,subscribe}=await import('/modules/state.js');
                      const nodes=new WeakMap();let serial=0;window.inputEvents=[];
                      function record(type,event){
                        const element=event?.target instanceof HTMLElement?event.target:document.activeElement;
                        if(element&&!nodes.has(element))nodes.set(element,++serial);
                        const field=document.querySelector('#channel-c0-black');
                        window.inputEvents.push({type,key:event?.key,id:element?.id,node:nodes.get(element),
                          value:element?.value,defaultValue:element?.defaultValue,connected:element?.isConnected,
                          pending:element?.dataset?.pendingEdit,discarded:element?.dataset?.discardedEdit,
                          active:document.activeElement?.id,black:sourceChannelView()?.channels[0]?.black,
                          blackInput:field?.value,blackDefault:field?.defaultValue,
                          commit:state.formCommitPending,pointer:state.formPointerPending,deferred:state.renderDeferred});
                        if(window.inputEvents.length>300)window.inputEvents.shift();
                      }
                      for(const type of ['input','change','focusin','focusout','keydown']){
                        document.addEventListener(type,event=>record(type+':capture',event),true);
                        document.addEventListener(type,event=>record(type+':bubble',event));
                      }
                      subscribe(()=>record('state'));
                    }''')
                    page.locator('#import-files').set_input_files(uploads[:2])
                    wait('state.project.assets.length===2 && !state.busy')
                    ids = value('state.project.assets.map(asset=>asset.id)')
                    page.locator('[data-action="tab"][data-tab="channels"]').click()
                    page.locator('#channel-c0-name').wait_for()
                    for frame in [1, 2, 3, 0, 3]:
                        field = page.locator('#channel-frame')
                        field.fill(str(frame))
                        redraw_during_draft('#channel-frame')
                        field.press('Tab')
                        wait(f'sourceChannelView().channels.every(channel=>channel.source.frame==={frame}) && !state.busy')
                    complete('Five consecutive numeric frame edits survive forced async renders and native Tab')

                    page.locator('#channel-c0-name').fill('DAPI 核 — Enter')
                    redraw_during_draft('#channel-c0-name')
                    page.locator('#channel-c0-name').press('Enter')
                    wait('sourceChannelView().channels[0].name==="DAPI 核 — Enter"')
                    page.locator('#channel-c0-black').fill('120')
                    page.locator('#channel-c0-black').press('Tab')
                    wait('sourceChannelView().channels[0].black===120')
                    # A clean number input must retain the native select-all state,
                    # which cannot be recovered through selectionStart/End APIs.
                    page.locator('#channel-c0-black').focus()
                    page.locator('#channel-c0-black').press('ControlOrMeta+A')
                    redraw_during_draft('#channel-c0-black')
                    page.locator('#channel-c0-black').press('Delete')
                    assert page.locator('#channel-c0-black').input_value() == ''
                    redraw_during_draft('#channel-c0-black')
                    page.locator('#channel-c0-black').press('Tab')
                    wait('sourceChannelView().channels[0].black===null')
                    complete('Enter commits text; native number select-all survives redraw and empty values commit automatic range')

                    page.locator('#channel-c0-name').fill('Discard this draft')
                    redraw_during_draft('#channel-c0-name')
                    page.locator('#channel-c0-name').press('Escape')
                    wait('!state.renderDeferred')
                    assert value('sourceChannelView().channels[0].name') == 'DAPI 核 — Enter'
                    assert page.locator('#channel-c0-name').input_value() == 'DAPI 核 — Enter'
                    page.locator('[data-action="project-menu"]').click()
                    page.locator('[data-action="new-dialog"]').click()
                    page.locator('#new-project-title').fill('Discard modal draft')
                    redraw_during_draft('#new-project-title')
                    page.locator('#new-project-title').press('Escape')
                    wait('state.modal===null && !state.renderDeferred')
                    assert page.locator('.modal').count() == 0
                    complete('Escape discards an uncommitted field and closes a modal without blocking redraw')

                    page.locator('#channel-c0-gamma').fill('1.7')
                    redraw_during_draft('#channel-c0-gamma')
                    page.locator('[data-action="tab"][data-tab="compose"]').click()
                    wait('state.tab==="compose"')
                    assert value('state.project.settings.asset_edits[state.selectedAssetId].channel_view.channels[0].gamma') == 1.7
                    page.locator('[data-action="tab"][data-tab="channels"]').click()
                    page.locator('#channel-c1-visible').uncheck()
                    wait('sourceChannelView().channels[1].visible===false')
                    page.locator('#channel-c1-visible').check()
                    wait('sourceChannelView().channels[1].visible===true')
                    page.locator('#channel-c0-gamma').focus()
                    button=page.locator('[data-action="tab"][data-tab="compose"]')
                    button.hover()
                    page.mouse.down()
                    assert button.evaluate('async el=>{const {emit}=await import("/modules/state.js");for(let i=0;i<6;i++)emit();return el.isConnected}'), 'A pointerdown target was replaced before click'
                    page.mouse.up()
                    wait('state.tab==="compose"')
                    page.locator('[data-action="tab"][data-tab="channels"]').click()
                    complete('Workflow clicks commit drafts; clean button pointer transactions and checkbox changes survive redraw')

                    page.locator('#channel-c0-name').fill('Saved before source switch')
                    redraw_during_draft('#channel-c0-name')
                    page.locator(f'[data-action="select-asset"][data-id="{ids[1]}"]').click()
                    wait('state.selectedAssetId===' + json.dumps(ids[1]))
                    page.locator('#channel-c0-name').wait_for()
                    assert value('state.project.settings.asset_edits[' + json.dumps(ids[0]) + '].channel_view.channels[0].name') == 'Saved before source switch'
                    assert value('sourceChannelView().channels[0].source.asset_id') == ids[1]
                    assert value('sourceChannelView().channels[0].name') != 'Saved before source switch'
                    complete('Clicking another source commits only the prior source and leaves no blocked render')

                    page.locator(f'[data-action="select-asset"][data-id="{ids[0]}"]').click()
                    page.locator('[data-action="channel-mode"][data-mode="split"]').click()
                    page.set_viewport_size({'width': 900, 'height': 900})
                    page.locator('#channel-split-columns').fill('3')
                    page.locator('#channel-split-columns').press('Tab')
                    wait('sourceChannelView().split.columns===3')
                    # Viewport resize and field commit each schedule a render.
                    # Observe the committed layout, not the previous auto-grid.
                    before_snapshot = committed_grid()
                    before = before_snapshot['tiles']
                    assert len(before) == 4
                    assert abs(before[0]['y'] - before[2]['y']) < 1 and before[3]['y'] > before[0]['y'], before
                    gap_before = before[1]['x'] - before[0]['x'] - before[0]['width']
                    page.locator('#channel-split-gap').fill('40')
                    page.locator('#channel-split-gap').press('Tab')
                    wait('sourceChannelView().split.gap_px===40')
                    after_snapshot = committed_grid(gap_before+5)
                    after = after_snapshot['tiles']
                    gap_after = after[1]['x'] - after[0]['x'] - after[0]['width']
                    report['grid_probe']={'before':before,'after':after,'gap_before_px':gap_before,'gap_after_px':gap_after,'computed':after_snapshot['computed']}
                    assert gap_after > gap_before + 5, report['grid_probe']
                    assert abs(after[0]['y'] - after[2]['y']) < 1
                    page.locator('[data-action="tab"][data-tab="analysis"]').click()
                    page.locator('[data-action="analysis-kind"][data-kind="microscopy"]').click()
                    page.locator('#analysis-method').select_option('channel_intensity')
                    page.locator('#analysis-raw-c1').check()
                    page.locator('[data-action="source-tool"][data-tool="roi"]').click()
                    canvas_selector = '[data-source-canvas][data-channel-id="c1"]'
                    canvas = page.locator(canvas_selector)
                    canvas.scroll_into_view_if_needed()
                    wait('!state.busy && !state.formCommitPending && !state.formPointerPending')
                    coordinates = page.wait_for_function('''selector=>{
                      const svg=document.querySelector(selector),matrix=svg?.getScreenCTM(),rect=svg?.getBoundingClientRect();
                      if(!svg?.isConnected||!matrix||!rect.width||!rect.height)return false;
                      return [[20,25],[100,85]].map(p=>{const q=new DOMPoint(...p).matrixTransform(matrix);return {x:q.x,y:q.y};});
                    }''', arg=canvas_selector)
                    try:
                        points = coordinates.json_value()
                    finally:
                        coordinates.dispose()
                    page.mouse.move(points[0]['x'], points[0]['y'])
                    page.mouse.down()
                    page.mouse.move(points[1]['x'], points[1]['y'], steps=8)
                    page.mouse.up()
                    wait('!!analysisGeometry().roi')
                    roi = value('analysisGeometry().roi')
                    assert (roi['x'], roi['y'], roi['width'], roi['height']) == (20, 25, 80, 60), roi
                    report['grid_geometry'] = {'viewport_width_px': 900, 'columns': 3, 'gap_before_px': gap_before, 'gap_after_px': gap_after, 'source_roi': roi}
                    page.screenshot(path=str(output / 'gap-columns-roi.png'))
                    complete('Raw gap changes visible spacing; explicit columns survive 900px viewport and source ROI CTM stays exact')
                    wait('!state.dirty && state.saveState==="saved"')
                    assert not report['browser_errors'], report['browser_errors']
                    report['passed'] = True
                    page.screenshot(path=str(output / 'input-lifecycle.png'))
                except Exception as error:
                    report['failure'] = str(error)
                    report['failure_dom'] = page.evaluate('''()=>({
                      focused:{tag:document.activeElement?.tagName,id:document.activeElement?.id,action:document.activeElement?.dataset?.action},
                      tiles:[...document.querySelectorAll('.source-tile')].map(el=>({connected:el.isConnected,...el.getBoundingClientRect().toJSON()})),
                      grid:(()=>{const el=document.querySelector('.source-multigrid');return el?{connected:el.isConnected,gap:getComputedStyle(el).columnGap,columns:getComputedStyle(el).gridTemplateColumns}:null;})()
                    })''')
                    (output / 'FAILED-state.json').write_text(json.dumps(value('({black:sourceChannelView()?.channels[0]?.black,formCommitPending:state.formCommitPending,formPointerPending:state.formPointerPending,renderDeferred:state.renderDeferred,events:window.inputEvents})'), ensure_ascii=False, indent=2), 'utf-8')
                    (output / 'FAILED.html').write_text(page.content(), 'utf-8')
                    page.screenshot(path=str(output / 'FAILED.png'), full_page=True)
                    raise
                finally:
                    (output / 'browser-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
                    browser.close()
        finally:
            server.should_exit = True
            thread.join(5)
            listener.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
