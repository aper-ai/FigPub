"""Real Chromium acceptance for home/tasks, recoverable errors and close/save.

All operations use production controls and an isolated real HTTP service. State
is read only to verify results; the only injected failures are explicit HTTP 503
responses used to exercise save recovery. This is separate from the native
Windows host acceptance and the independent DOM/ASGI integration suite.

Run: python tests/browser_onboarding.py
Optional: BROWSER_EXECUTABLE=/path/to/chromium
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from PIL import Image
import tifffile
import uvicorn
from playwright.sync_api import expect, sync_playwright
from workbench.server import create_app
from browser_wb_workflow import image_bytes as wb_image_bytes


def qpcr_csv():
    """Known relative expression: Control=1, Treatment=2, three independent n."""
    rows = ['sample,target,ct,group,biological_replicate,technical_replicate']
    for group in ['Control', 'Treatment']:
        for replicate in range(1, 4):
            sample = f'{group}-{replicate}'
            for target, ct in [('ACTB', 18), ('MYC', 20 if group == 'Control' else 19)]:
                rows.append(f'{sample},{target},{ct},{group},{sample},1')
    return ('\n'.join(rows) + '\n').encode('utf-8')


def png_bytes(color):
    output = BytesIO()
    Image.new('RGB', (240, 160), color).save(output, 'PNG')
    return output.getvalue()


@contextmanager
def capture_failure(page, output, report):
    """Capture the live page before Playwright unwinds its browser connection."""
    try:
        yield
    except Exception as error:
        report['failure'] = f'{type(error).__name__}: {error}'
        try:
            page.screenshot(path=str(output/'failure.png'), full_page=True)
            report['failure_state'] = page.evaluate('''async () => {
                const {state,analysisSelection}=await import('/modules/state.js');
                return {tab:state.tab, selectedAssetId:state.selectedAssetId,
                    analysisKind:state.analysisKind, analysisParams:state.analysisParams,
                    inputError:analysisSelection().input.error,
                    tableError:state.tableError, tableLoading:state.tableLoading,
                    formCommitPending:state.formCommitPending,
                    formPointerPending:state.formPointerPending,
                    renderDeferred:state.renderDeferred, modal:state.modal?.type,
                    focus:{tag:document.activeElement?.tagName,id:document.activeElement?.id,
                        action:document.activeElement?.dataset?.action,task:document.activeElement?.dataset?.task,
                        insideCurrentDialog:!!document.querySelector('[role="dialog"]')?.contains(document.activeElement)},
                    readiness:document.querySelector('#analysis-readiness')?.textContent,
                    lastError:state.lastError};
            }''')
        except Exception as capture_error:
            report['capture_error'] = str(capture_error)
        raise


@contextmanager
def isolated_service():
    """Stop the service before removing its files, including on Windows failures."""
    from run import reserve_port
    listener = server = thread = None
    with tempfile.TemporaryDirectory(prefix='figure-onboarding-') as directory:
        try:
            listener = reserve_port(0)
            port = listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(create_app(directory), host='127.0.0.1',
                                                    port=port, log_level='error'))
            thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
            thread.start()
            for _ in range(200):
                if server.started:
                    break
                time.sleep(.025)
            if not server.started:
                raise RuntimeError('Isolated test HTTP service did not become ready')
            yield f'http://127.0.0.1:{port}'
        finally:
            if server:
                server.should_exit = True
            if thread:
                thread.join(5)
            if listener:
                listener.close()


def main():
    output = Path(os.environ.get('FW_BROWSER_RESULTS', ROOT / 'test-results/onboarding'))
    output.mkdir(parents=True, exist_ok=True)
    report = {'real_browser': True, 'synthetic_data': True, 'scenarios': [],
              'browser_errors': [], 'passed': False}
    browser = page = None

    def checked(name, started):
        report['scenarios'].append({'name': name, 'passed': True,
                                   'elapsed_ms': round((time.perf_counter()-started)*1000, 2)})
        print('PASS', name, flush=True)

    try:
        with isolated_service() as base:
            with sync_playwright() as playwright, ExitStack() as evidence:
                options = {'headless': True}
                if os.environ.get('BROWSER_EXECUTABLE'):
                    options['executable_path'] = os.environ['BROWSER_EXECUTABLE']
                browser = playwright.chromium.launch(**options)
                context = browser.new_context(viewport={'width': 1280, 'height': 800},
                                              device_scale_factor=1, accept_downloads=True)
                context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(
                    (base+'/', 'data:', 'blob:')) else route.abort())
                page = context.new_page()
                evidence.enter_context(capture_failure(page, output, report))
                page.set_default_timeout(15000)
                page.on('pageerror', lambda error: report['browser_errors'].append(str(error)))

                def state_value(expression):
                    return page.evaluate('async()=>{const {state,analysisSelection}=await import("/modules/state.js");return ('+expression+')}')

                def wait_state(expression, seconds=25):
                    deadline = time.monotonic()+seconds
                    while time.monotonic() < deadline:
                        if state_value(expression):
                            return
                        page.wait_for_timeout(40)
                    raise AssertionError('Timed out: '+expression+'; toasts='+page.locator('#toast-region').inner_text())

                def click(action, extra=''):
                    page.locator(f'[data-action="{action}"]{extra}').first.click()

                def change(selector, value):
                    page.locator(selector).fill(str(value))
                    page.locator(selector).press('Tab')

                def upload_with_action(action, files, extra=''):
                    with page.expect_file_chooser() as selected:
                        click(action, extra)
                    selected.value.set_files(files)

                def home():
                    click('tab', '[data-tab="home"]')
                    page.locator('.home-task-grid').wait_for()

                def saved():
                    wait_state('!state.busy && !state.dirty && state.saveState==="saved"')

                def run_job():
                    previous = state_value('state.job?.id || ""')
                    click('run-analysis')
                    wait_state('state.job && state.job.id!=='+json.dumps(previous)+' && ["done","error"].includes(state.job.status)')
                    assert state_value('state.job.status') == 'done', state_value('state.job')
                    wait_state('analysisSelection().persisted && analysisSelection().record.id===state.job.id')
                    return state_value('analysisSelection().result')

                started = time.perf_counter()
                page.goto(base, wait_until='networkidle')
                assert state_value('state.tab') == 'home'
                assert page.locator('.home-task').count() == 4
                assert page.locator('.workspace-grid').count() == 0
                assert page.locator('[data-action="resume-workspace"]').count() == 0
                assert page.locator('#home-title').inner_text() == '科研图像与数据工作台'
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                click('help')
                assert page.locator('.quick-start [data-action="start-task"]').count() == 4
                page.wait_for_function('()=>{const dialog=document.querySelector("[role=dialog]");return dialog?.isConnected&&dialog.contains(document.activeElement);}')
                report['quick_start_focus'] = []
                for key in ['Tab']*12+['Shift+Tab']*12:
                    page.keyboard.press(key)
                    # Query the current dialog and focus atomically: a captured
                    # old dialog can be replaced by a background save before a
                    # locator.evaluate callback runs, while focus remains valid.
                    focused = page.evaluate('''()=>{
                      const dialog=document.querySelector('[role="dialog"]'),active=document.activeElement;
                      return {inside:!!dialog?.isConnected&&dialog.contains(active),
                        action:active?.dataset?.action,task:active?.dataset?.task,text:active?.textContent};
                    }''')
                    report['quick_start_focus'].append({'key': key, **focused})
                    assert focused['inside'], focused
                page.locator('.quick-start [data-task="qpcr"]').focus()
                assert page.evaluate('''async()=>{
                  const {emit}=await import('/modules/state.js');
                  for(let i=0;i<6;i++)emit();
                  return document.querySelector('[role="dialog"]').contains(document.activeElement)
                    &&document.activeElement.dataset.task==='qpcr';
                }'''), 'A status/font redraw must preserve the selected quick-start action'
                click('close-modal')
                page.screenshot(path=str(output/'01-home-1280.png'))
                checked('fresh task home, readable laptop layout and keyboard-contained quick start', started)

                started = time.perf_counter()
                csv = qpcr_csv()
                upload_with_action('start-task', {'name': 'SYNTHETIC-onboarding-qpcr.csv',
                    'mimeType': 'text/csv', 'buffer': csv}, '[data-task="qpcr"]')
                wait_state('!state.busy && !state.tableLoading && state.table?.canonical')
                assert state_value('state.tab') == 'analysis'
                assert state_value('state.analysisKind') == 'qpcr'
                assert state_value('state.table.rows.length') == 12
                expect(page.locator('[data-action="run-analysis"]')).to_be_disabled()
                assert '内参' in page.locator('#analysis-readiness').inner_text()
                assert page.locator('#qpcr-target-options option[value="ACTB"]').count() == 1
                assert page.locator('#qpcr-group-options option[value="Control"]').count() == 1
                change('#analysis-reference_gene', 'ACTB')
                change('#analysis-control_group', 'Control')
                # Native change/Tab commits data before its deferred form redraw.
                # Await the observable ready state rather than sampling that gap.
                expect(page.locator('[data-action="run-analysis"]')).to_be_enabled()
                assert state_value('state.analysisParams.reference_gene') == 'ACTB'
                assert state_value('state.analysisParams.control_group') == 'Control'
                result = run_job()
                groups = {group['group']: group for group in result['groups']}
                assert groups['Control']['mean'] == 1
                assert groups['Treatment']['mean'] == 2
                assert all(group['n_biological'] == 3 for group in groups.values())
                measurement = state_value('analysisSelection().record.id')
                page.screenshot(path=str(output/'02-qpcr-guided-analysis.png'))
                click('plot-results')
                wait_state('!state.busy && state.tab==="compose" && state.project.panels.length===1')
                assert state_value('state.project.panels[0].analysis_source.measurement_id') == measurement
                checked('qPCR home picker routes to actual analysis, prerequisites and exact 1x/2x result chart', started)

                started = time.perf_counter()
                home()
                wb_bytes = wb_image_bytes()
                upload_with_action('start-task', {'name': 'SYNTHETIC-onboarding-WB.tif',
                    'mimeType': 'image/tiff', 'buffer': wb_bytes}, '[data-task="wb"]')
                wait_state('!state.busy && state.modal?.type==="wb-workflow"')
                wb_id = hashlib.sha256(wb_bytes).hexdigest()
                assert state_value('state.selectedAssetId') == wb_id
                assert state_value('state.modal.draft.lanes.length') == 6
                assert state_value('state.modal.draft.rows.every(row=>row.role==="ignore")')
                assert state_value('state.modal.draft.lanes.every(lane=>!lane.biological_replicate)')
                close = page.evaluate('window.figureWorkbenchPrepareClose()')
                assert close['saved'] is False and '尚未应用' in close['message']
                click('close-modal')
                assert state_value('state.project.settings.analysis_rois.length') == 0
                checked('WB home picker opens real candidate wizard without guessing identities or losing uncommitted draft', started)

                started = time.perf_counter()
                home()
                click('start-task', '[data-task="microscopy"]')
                assert state_value('state.tab') == 'analysis'
                assert state_value('state.analysisKind') == 'microscopy'
                values = np.arange(8000, dtype=np.uint16).reshape(80, 100)
                image = BytesIO()
                tifffile.imwrite(image, values, photometric='minisblack')
                upload_with_action('import', {'name': 'SYNTHETIC-known-16bit.tif',
                    'mimeType': 'image/tiff', 'buffer': image.getvalue()})
                image_id = hashlib.sha256(image.getvalue()).hexdigest()
                wait_state('!state.busy && state.selectedAssetId==='+json.dumps(image_id))
                assert state_value('state.tab') == 'analysis'
                assert state_value('state.analysisKind') == 'microscopy'
                page.locator('#analysis-method').select_option('intensity')
                result = run_job()
                assert result['summary']['mean_intensity'] == float(values.mean())
                assert result['summary']['integrated_intensity'] == int(values.sum())
                assert result['summary']['source_bit_depth'] == 16
                # Invalid pending numeric drafts must not be silently discarded at close.
                previous_scale = state_value('state.analysisParams.um_per_px')
                page.locator('#analysis-um_per_px').fill('-2')
                close = page.evaluate('window.figureWorkbenchPrepareClose()')
                assert close['saved'] is False and '输入无效' in close['message']
                assert state_value('state.analysisParams.um_per_px') == previous_scale
                assert page.locator('#analysis-um_per_px').input_value() == '-2'
                page.locator('#analysis-um_per_px').press('Escape')
                checked('microscopy task retains imported source and measures exact raw 16-bit values; invalid close draft stays editable', started)

                started = time.perf_counter()
                home()
                click('start-task', '[data-task="compose"]')
                assert state_value('state.tab') == 'compose'
                count = state_value('state.project.panels.length')
                upload_with_action('import', [
                    {'name': 'SYNTHETIC-panel-green.png', 'mimeType': 'image/png', 'buffer': png_bytes('#67a58c')},
                    {'name': 'SYNTHETIC-panel-blue.png', 'mimeType': 'image/png', 'buffer': png_bytes('#6697b1')},
                ])
                wait_state(f'!state.busy && state.project.panels.length==={count+2}')
                assert state_value('state.tab') == 'compose'
                assert state_value('state.project.panels.map(panel=>panel.label)') == ['A', 'B', 'C']
                click('undo')
                assert state_value('state.project.panels.length') == count
                assert state_value('state.project.assets.some(asset=>asset.name==="SYNTHETIC-panel-green.png")')
                click('redo')
                assert state_value('state.project.panels.length') == count+2
                saved()
                page.screenshot(path=str(output/'03-compose-1280.png'))
                with page.expect_download(timeout=30000) as download:
                    click('save-project')
                archive = output/'onboarding-roundtrip.figwork'
                download.value.save_as(archive)
                wait_state('!state.busy')
                with ZipFile(archive) as backup:
                    saved_project = json.loads(backup.read('project.json'))
                    manifest = json.loads(backup.read('manifest.json'))
                    assert len(saved_project['measurements']) == 2
                    assert len(saved_project['panels']) == 3
                    assert len(manifest['assets']) == len(saved_project['assets'])
                    for source in manifest['assets']:
                        assert hashlib.sha256(backup.read(source['path'])).hexdigest() == source['sha256']
                checked('composition task imports, labels, undo/redo and verified portable backup containing all original bytes', started)

                started = time.perf_counter()
                home()
                click('start-task', '[data-task="qpcr"]')
                wait_state('!state.tableLoading && state.table?.canonical')
                panels_before = state_value('state.project.panels')
                upload_with_action('import', {'name': 'SYNTHETIC-missing-Ct-columns.csv',
                    'mimeType': 'text/csv', 'buffer': b'hello,value\nx,1\n'})
                wait_state('!state.busy && !state.tableLoading && !!state.tableError')
                assert page.locator('.table-read-error').is_visible()
                expect(page.locator('[data-action="run-analysis"]')).to_be_disabled()
                assert state_value('state.project.panels') == panels_before
                assert state_value('state.project.assets.some(asset=>asset.name==="SYNTHETIC-missing-Ct-columns.csv")')
                click('load-table')
                wait_state('!state.tableLoading && !!state.tableError')
                upload_with_action('import', {'name': 'SYNTHETIC-onboarding-qpcr.csv',
                    'mimeType': 'text/csv', 'buffer': csv})
                wait_state('!state.busy && !state.tableLoading && !!state.table?.canonical && !state.tableError')
                assert page.locator('.table-read-error').count() == 0
                expect(page.locator('[data-action="run-analysis"]')).to_be_enabled()
                checked('invalid Ct schema preserves originals and canvas, explains missing input, retries and recovers', started)

                started = time.perf_counter()
                home()
                click('start-task', '[data-task="wb"]')
                selected_before = state_value('state.selectedAssetId')
                assets_before = state_value('state.project.assets.map(asset=>asset.id)')
                panels_before = state_value('state.project.panels')
                upload_with_action('import-wb', {'name': 'SYNTHETIC-corrupt.png',
                    'mimeType': 'image/png', 'buffer': b'this is deliberately not a PNG image'})
                wait_state('!state.busy && !!state.lastError')
                assert state_value('state.selectedAssetId') == selected_before
                assert state_value('state.project.assets.map(asset=>asset.id)') == assets_before
                assert state_value('state.project.panels') == panels_before
                assert state_value('state.modal?.type!=="wb-workflow"')
                assert page.locator('.operation-error').is_visible()
                click('dismiss-error')
                assert page.locator('.operation-error').count() == 0
                checked('corrupt WB import leaves prior workspace intact and never analyzes the old selected image', started)

                started = time.perf_counter()
                page.locator('#project-title').fill('Close bridge saves pending title')
                close = page.evaluate('window.figureWorkbenchPrepareClose()')
                assert close['saved'] is True and close['busy'] is False, close
                assert page.request.get(base+'/api/project').json()['title'] == 'Close bridge saves pending title'

                def failed_save(route):
                    if route.request.method == 'PUT':
                        route.fulfill(status=503, content_type='application/json',
                                      body=json.dumps({'detail': 'Synthetic save failure for recovery acceptance'}))
                    else:
                        route.continue_()

                page.route('**/api/project', failed_save)
                page.locator('#project-title').fill('Recovered after actual save failure')
                close = page.evaluate('window.figureWorkbenchPrepareClose()')
                assert close['saved'] is False and '保存未完成' in close['message']
                assert state_value('state.dirty && state.saveState==="error"')
                assert page.locator('.operation-error [data-action="retry-save"]').is_visible()
                assert page.request.get(base+'/api/project').json()['title'] == 'Close bridge saves pending title'
                page.unroute('**/api/project', failed_save)
                click('retry-save')
                saved()
                assert page.request.get(base+'/api/project').json()['title'] == 'Recovered after actual save failure'
                assert page.evaluate('window.figureWorkbenchPrepareClose()')['saved'] is True
                checked('native close bridge commits focused draft, reports failed HTTP save and succeeds after explicit retry', started)

                started = time.perf_counter()
                before_switch = state_value('state.project')
                with page.expect_file_chooser() as selected:
                    click('open-project')
                selected.value.set_files(archive)
                wait_state('state.modal?.type==="switch-project" && !state.busy')
                assert state_value('state.project.title') == before_switch['title']
                assert state_value('state.project.assets') == before_switch['assets']
                click('close-modal')
                assert state_value('state.project.title') == before_switch['title']
                with page.expect_file_chooser() as selected:
                    click('open-project')
                selected.value.set_files(archive)
                wait_state('state.modal?.type==="switch-project"')
                click('confirm-project-switch')
                wait_state('!state.busy && !state.modal && state.project.title==='+json.dumps(saved_project['title']))
                assert state_value('state.project.panels') == saved_project['panels']
                assert state_value('state.project.measurements') == saved_project['measurements']
                assert state_value('state.project.assets.map(asset=>asset.id)') == [asset['id'] for asset in saved_project['assets']]
                response = page.request.get(base+f'/api/assets/{wb_id}/original')
                assert response.ok and response.body() == wb_bytes
                checked('open-project confirmation permits cancellation and restores exact saved panels, results and source TIFF', started)

                started = time.perf_counter()
                saved()
                page.reload(wait_until='networkidle')
                assert state_value('state.tab') == 'home'
                assert page.locator('[data-action="resume-workspace"]').is_visible()
                assert saved_project['title'] in page.locator('.resume-project').inner_text()
                click('resume-workspace')
                wait_state('state.tab==="compose"')
                assert page.locator('#figure-svg').is_visible()
                assert state_value('state.project.panels.length') == 3
                assert not report['browser_errors'], report['browser_errors']
                checked('relaunch lands on resumable home and continues the saved composition', started)
                report['passed'] = True
                browser.close()
                browser = None
    except Exception as error:
        report['failure'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        (output/'onboarding-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', 'utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
