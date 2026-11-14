"""Real browser regressions for task ownership, recovery and native keys.

The production HTTP application, JobManager and numerical algorithms run in a
temporary workspace. Test-only middleware delays/rejects submission or refuses
status reads for one actual job ID. It never invents a successful job/result.
Cancellation uses occupied production worker slots to place a real task in its
queue; all barriers are released before server and worker shutdown.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from fastapi.responses import JSONResponse
from playwright.sync_api import expect, sync_playwright
import tifffile
import uvicorn

from browser_onboarding import capture_failure, qpcr_csv
from browser_wb_workflow import image_bytes as gel_image_bytes
from workbench.server import create_app


class NetworkConditions:
    """Observable faults at the HTTP boundary, with genuine server responses."""

    def __init__(self):
        self.lock = threading.RLock()
        self.post_release = threading.Event()
        self.post_release.set()
        self.pixel_release = threading.Event()
        self.pixel_release.set()
        self.delayed_pixel_asset = None
        self.pixel_requests = 0
        self.active_pixel_requests = 0
        self.post_requests = 0
        self.rejected_posts = 0
        self.reject_next_post = False
        self.fail_next_job_status = False
        self.failing_job_id = None
        self.accepted_job_ids = []
        self.status_reads = {}
        self.failed_reads = {}
        self.cancel_ids = []
        self.project_replacements = []
        self.worker_barriers = []

    def snapshot(self):
        with self.lock:
            return {
                'post_requests': self.post_requests,
                'rejected_posts': self.rejected_posts,
                'accepted_job_ids': list(self.accepted_job_ids),
                'status_reads': dict(self.status_reads),
                'failed_reads': dict(self.failed_reads),
                'cancel_ids': list(self.cancel_ids),
                'project_replacements': list(self.project_replacements),
                'pixel_requests': self.pixel_requests,
                'active_pixel_requests': self.active_pixel_requests,
            }

    def release_all(self):
        self.post_release.set()
        self.pixel_release.set()
        for barrier in self.worker_barriers:
            barrier.set()


@contextmanager
def controlled_service():
    from run import reserve_port

    controls = NetworkConditions()
    listener = server = thread = None
    with tempfile.TemporaryDirectory(prefix='figure-task-lifecycle-') as directory:
        app = create_app(directory)

        @app.middleware('http')
        async def controlled_network(request, call_next):
            path, method = request.url.path, request.method
            if (method == 'GET' and controls.delayed_pixel_asset
                    and path == f'/api/assets/{controls.delayed_pixel_asset}/pixels.bin'):
                with controls.lock:
                    controls.pixel_requests += 1
                    controls.active_pixel_requests += 1
                try:
                    while not controls.pixel_release.is_set():
                        await asyncio.sleep(.01)
                    return await call_next(request)
                finally:
                    with controls.lock:
                        controls.active_pixel_requests -= 1
            if method == 'POST' and path in {'/api/project/new', '/api/project/open', '/api/demo'}:
                with controls.lock:
                    controls.project_replacements.append(path)
            if method == 'POST' and path == '/api/jobs':
                with controls.lock:
                    controls.post_requests += 1
                    reject = controls.reject_next_post
                    controls.reject_next_post = False
                while not controls.post_release.is_set():
                    await asyncio.sleep(.01)
                if reject:
                    with controls.lock:
                        controls.rejected_posts += 1
                    return JSONResponse({'detail': 'TEST: submission temporarily unavailable'}, status_code=503)
                with app.state.jobs.lock:
                    before = set(app.state.jobs.jobs)
                response = await call_next(request)
                with app.state.jobs.lock:
                    created = set(app.state.jobs.jobs) - before
                with controls.lock:
                    controls.accepted_job_ids.extend(sorted(created))
                    if controls.fail_next_job_status and len(created) == 1:
                        controls.failing_job_id = next(iter(created))
                        controls.fail_next_job_status = False
                return response
            if path.startswith('/api/jobs/'):
                job_id = path.rsplit('/', 1)[-1]
                with controls.lock:
                    if method == 'GET':
                        controls.status_reads[job_id] = controls.status_reads.get(job_id, 0) + 1
                        if job_id == controls.failing_job_id:
                            controls.failed_reads[job_id] = controls.failed_reads.get(job_id, 0) + 1
                            return JSONResponse({'detail': 'TEST: known task status temporarily unavailable'}, status_code=503)
                    elif method == 'DELETE':
                        controls.cancel_ids.append(job_id)
            return await call_next(request)

        try:
            listener = reserve_port(0)
            port = listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error'))
            thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
            thread.start()
            for _ in range(200):
                if server.started:
                    break
                time.sleep(.025)
            if not server.started:
                raise RuntimeError('Task lifecycle service did not start')
            yield f'http://127.0.0.1:{port}', app, controls
        finally:
            controls.release_all()
            if server:
                server.should_exit = True
            if thread:
                thread.join(10)
            if listener:
                listener.close()
            if thread and thread.is_alive():
                raise RuntimeError('Task lifecycle service or real analysis workers did not stop')


@contextmanager
def occupied_workers(app, controls):
    """Queue congestion only: do not replace the real analysis runner."""
    release = threading.Event()
    controls.worker_barriers.append(release)
    ready = [threading.Event() for _ in range(app.state.jobs.executor._max_workers)]

    def occupy(entered):
        entered.set()
        release.wait()

    futures = [app.state.jobs.executor.submit(occupy, entered) for entered in ready]
    try:
        if not all(entered.wait(5) for entered in ready):
            raise AssertionError('Could not occupy the production worker slots')
        yield
    finally:
        release.set()
        for future in futures:
            future.result(timeout=5)


def main():
    output = Path(os.environ.get('FW_BROWSER_RESULTS', ROOT / 'test-results/task-lifecycle-local'))
    output.mkdir(parents=True, exist_ok=True)
    report = {'real_browser': True, 'synthetic_data': True, 'passed': False,
              'scenarios': [], 'browser_errors': [],
              'faults': 'Delayed/rejected POST, 503 status reads for one real job ID and delayed real gel pixels; no successful response is mocked.',
              'cancellation_fixture': 'Production worker slots are occupied temporarily so a real JobManager task can be cancelled while queued.'}
    controls = None

    def checked(name):
        report['scenarios'].append({'name': name, 'passed': True})
        print('PASS', name, flush=True)

    try:
        with controlled_service() as (base, app, controls), sync_playwright() as playwright, ExitStack() as evidence:
            options = {'headless': True}
            if os.environ.get('BROWSER_EXECUTABLE'):
                options['executable_path'] = os.environ['BROWSER_EXECUTABLE']
            browser = playwright.chromium.launch(**options)
            evidence.callback(browser.close)
            context = browser.new_context(viewport={'width': 1280, 'height': 800},
                                          device_scale_factor=1, accept_downloads=True)
            evidence.callback(context.close)
            context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(
                (base + '/', 'data:', 'blob:')) else route.abort())
            page = context.new_page()
            page.set_default_timeout(15000)
            page.on('pageerror', lambda error: report['browser_errors'].append(str(error)))
            evidence.enter_context(capture_failure(page, output, report))

            def value(expression):
                return page.evaluate('async()=>{const {state,analysisSelection,currentRawSource}=await import("/modules/state.js");return (' + expression + ')}')

            def wait(expression, seconds=30):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    if value(expression):
                        return
                    page.wait_for_timeout(30)
                raise AssertionError('Timed out: ' + expression + '; state=' + str(value('({job:state.job,submission:state.analysisSubmission,error:state.lastError})')))

            def wait_network(predicate, seconds=10):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    if predicate(controls.snapshot()):
                        return
                    page.wait_for_timeout(30)
                raise AssertionError('Timed out waiting for HTTP boundary: ' + str(controls.snapshot()))

            def click(action, suffix=''):
                page.locator(f'[data-action="{action}"]{suffix}').first.click()

            def change(selector, text):
                page.locator(selector).fill(text)
                page.locator(selector).press('Tab')

            def wait_completed(previous=None):
                different = 'true' if previous is None else 'state.job.id!==' + json.dumps(previous)
                wait('state.job && (' + different + ') && ["done","error","cancelled"].includes(state.job.status)')
                assert value('state.job.status') == 'done', value('state.job')
                wait('analysisSelection().persisted && analysisSelection().record.id===state.job.id')
                return value('analysisSelection().result')

            def exact_qpcr(result):
                groups = {group['group']: group for group in result['groups']}
                assert groups['Control']['mean'] == 1
                assert groups['Treatment']['mean'] == 2
                assert all(group['n_biological'] == 3 for group in groups.values())
                assert len(result['rows']) == 6

            page.goto(base, wait_until='networkidle')
            with page.expect_file_chooser() as selected:
                click('start-task', '[data-task="qpcr"]')
            selected.value.set_files({'name': 'SYNTHETIC-task-lifecycle.csv', 'mimeType': 'text/csv', 'buffer': qpcr_csv()})
            wait('!state.busy && !state.tableLoading && state.table?.canonical')
            change('#analysis-reference_gene', 'ACTB')
            change('#analysis-control_group', 'Control')
            wait('!state.dirty && state.saveState==="saved"')
            source_a = value('state.selectedAssetId')
            table_b = qpcr_csv().decode('utf-8').replace('Control', 'B-Control').replace('Treatment', 'B-Treatment').replace('MYC', 'B_MARKER').encode('utf-8')
            with page.expect_file_chooser() as selected:
                click('import')
            selected.value.set_files({'name': 'SYNTHETIC-distinct-table-B.csv', 'mimeType': 'text/csv', 'buffer': table_b})
            wait('!state.busy && !state.tableLoading && state.table?.canonical && state.table.rows[0].sample==="B-Control-1"')
            source_b = value('state.selectedAssetId')
            assert source_a != source_b
            click('select-asset', '[data-id=' + json.dumps(source_a) + ']')
            wait('!state.tableLoading && state.table?.asset_id===' + json.dumps(source_a))
            assert value('state.analysisParams.reference_gene') == 'ACTB'
            assert value('state.analysisParams.control_group') == 'Control'
            wait('!state.dirty && state.saveState==="saved"')
            project_id = value('state.project.id')
            controls.post_release.clear()
            controls.fail_next_job_status = True
            button = page.locator('[data-action="run-analysis"]')
            expect(button).to_be_enabled()
            button.scroll_into_view_if_needed()
            bounds = button.bounding_box()
            page.mouse.dblclick(bounds['x'] + bounds['width']/2, bounds['y'] + bounds['height']/2, delay=20)
            wait('state.analysisSubmission?.phase==="submitting"')
            expect(page.locator('[data-action="run-analysis"]')).to_be_disabled()
            expect(page.locator('[data-action="run-analysis"]')).to_have_attribute('aria-busy', 'true')
            expect(page.locator('[data-action="run-analysis"]')).to_contain_text('正在提交')
            page.wait_for_timeout(350)
            assert controls.snapshot()['post_requests'] == 1, controls.snapshot()
            click('tab', '[data-tab="home"]')
            expect(page.locator('.background-activity [data-action="show-running-analysis"]')).to_be_visible()
            click('new-dialog')
            assert value('state.project.id') == project_id
            assert value('state.modal') is None
            assert '正在提交' in page.locator('#toast-region').inner_text()
            expect(page.locator('.operation-error')).to_contain_text('正在提交')
            assert not controls.snapshot()['project_replacements']
            click('show-running-analysis')
            expect(page.locator('[data-action="run-analysis"]')).to_be_disabled()
            expect(page.locator('.job-progress')).to_contain_text('正在提交分析任务')
            page.screenshot(path=str(output/'01-single-pending-submission.png'))
            checked('Rapid real double-click creates one POST; pending feedback and cross-page project-switch protection remain active')

            controls.post_release.set()
            wait('state.job?.pollingError && state.job.polling===false')
            first_id = value('state.job.id')
            assert value('state.job.status') in {'queued', 'running'}
            assert controls.snapshot()['failed_reads'][first_id] == 3
            expect(page.locator('[data-action="retry-job-status"]')).to_be_visible()
            expect(page.locator('.notice.warning').filter(has=page.locator('[data-action="retry-job-status"]'))).to_contain_text('暂时无法读取任务状态')
            assert page.locator('[data-action="run-analysis"]').count() == 0
            read_count = controls.snapshot()['status_reads'][first_id]
            page.wait_for_timeout(900)
            assert controls.snapshot()['status_reads'][first_id] == read_count
            click('tab', '[data-tab="home"]')
            expect(page.locator('.background-activity')).to_contain_text('暂时无法读取')
            click('new-dialog')
            assert value('state.project.id') == project_id and value('state.modal') is None
            expect(page.locator('.operation-error')).to_contain_text('分析任务状态尚未确认')
            click('show-running-analysis')
            assert value('state.job.id') == first_id
            assert controls.snapshot()['post_requests'] == 1
            page.screenshot(path=str(output/'02-known-task-status-paused.png'))
            checked('Three failed status reads stop automatic retry, preserve the same job ID and prevent duplicate submission across workspace navigation')

            click('select-asset', '[data-id=' + json.dumps(source_b) + ']')
            wait('!state.tableLoading && state.table?.asset_id===' + json.dumps(source_b))
            assert value('state.table.rows[0].sample') == 'B-Control-1'
            expect(page.locator('.table-workspace h2')).to_have_text('SYNTHETIC-distinct-table-B.csv')
            click('tab', '[data-tab="home"]')
            click('show-running-analysis')
            wait('state.selectedAssetId===' + json.dumps(source_a) + ' && !state.tableLoading && state.table?.asset_id===' + json.dumps(source_a))
            assert value('state.table.rows[0].sample') == 'Control-1'
            assert value('state.table.rows[0].source_row') == 2
            assert value('state.table.rows[0].target') == 'ACTB'
            assert value('state.analysisParams.reference_gene') == 'ACTB'
            assert value('state.analysisParams.control_group') == 'Control'
            expect(page.locator('.table-workspace h2')).to_have_text('SYNTHETIC-task-lifecycle.csv')
            expect(page.locator('.table-workspace thead')).to_contain_text('原表行')
            expect(page.locator('.table-workspace tbody tr').first).not_to_contain_text('B-Control')
            assert value('state.job.id') == first_id
            assert controls.snapshot()['post_requests'] == 1
            report['restored_table_identity'] = value('({selectedAssetId:state.selectedAssetId,tableAssetId:state.table.asset_id,firstRow:state.table.rows[0],referenceGene:state.analysisParams.reference_gene,controlGroup:state.analysisParams.control_group})')
            page.screenshot(path=str(output/'02b-running-task-restores-source-table.png'))
            checked('Viewing table B then returning to task A restores its actual source table, original rows and parameters without another submission')

            controls.failing_job_id = None
            click('retry-job-status')
            exact_qpcr(wait_completed())
            assert value('state.job.id') == first_id
            assert controls.snapshot()['post_requests'] == 1
            assert value('state.project.measurements.filter(item=>item.id===' + json.dumps(first_id) + ').length') == 1
            expect(page.locator('.operation-error')).to_have_count(0)
            expect(page.locator('#toast-region .toast').filter(has_text=re.compile(r'分析(?:任务正在提交|仍在运行|任务状态尚未确认).*再切换项目'))).to_have_count(0, timeout=2000)
            expect(page.locator('[data-action="retry-job-status"]')).to_have_count(0)
            expect(page.locator('[data-action="run-analysis"]')).to_be_enabled()
            expect(page.locator('[data-action="view-analysis-results"]')).to_be_visible()
            report['recovered_task_feedback'] = {
                'job_id': value('state.job.id'), 'status': value('state.job.status'),
                'last_error': value('state.lastError'),
                'visible_error_banners': page.locator('.operation-error').count(),
                'visible_retry_controls': page.locator('[data-action="retry-job-status"]').count(),
            }
            page.screenshot(path=str(output/'03-recovered-real-result.png'))
            checked('Retry status reads the original real qPCR result without another POST or duplicated measurement and clears obsolete task warnings')

            controls.reject_next_post = True
            click('run-analysis')
            wait('!state.analysisSubmission && String(state.lastError || "").includes("submission temporarily unavailable")')
            expect(page.locator('[data-action="run-analysis"]')).to_be_enabled()
            assert controls.snapshot()['post_requests'] == 2
            assert controls.snapshot()['accepted_job_ids'] == [first_id]
            assert value('analysisSelection().record.id') == first_id
            click('dismiss-error')
            click('run-analysis')
            exact_qpcr(wait_completed(first_id))
            second_id = value('state.job.id')
            assert controls.snapshot()['post_requests'] == 3
            assert len(controls.snapshot()['accepted_job_ids']) == 2
            checked('Rejected submission releases its lock, retains the previous result and allows a new successful real analysis')

            with occupied_workers(app, controls):
                click('run-analysis')
                wait('state.job?.status==="queued" && state.job.id!==' + json.dumps(second_id))
                cancelled_id = value('state.job.id')
                assert app.state.jobs.get(cancelled_id)['status'] == 'queued'
                click('tab', '[data-tab="home"]')
                click('new-dialog')
                expect(page.locator('.operation-error')).to_contain_text('分析仍在运行')
                assert value('state.project.id') == project_id and value('state.modal') is None
                click('show-running-analysis')
                click('cancel-job')
                wait('state.job?.status==="cancelled"')
                assert value('state.job.id') == cancelled_id
                record = app.state.jobs.get(cancelled_id)
                assert record['status'] == 'cancelled' and 'result' not in record
                assert not any(item['id'] == cancelled_id for item in app.state.store.get_project()['measurements'])
                assert controls.snapshot()['cancel_ids'] == [cancelled_id]
                expect(page.locator('[data-action="run-analysis"]')).to_be_enabled()
                expect(page.locator('.operation-error')).to_have_count(0)
                expect(page.locator('#toast-region .toast').filter(has_text=re.compile(r'分析(?:任务正在提交|仍在运行|任务状态尚未确认).*再切换项目'))).to_have_count(0, timeout=2000)
                page.screenshot(path=str(output/'04-queued-task-cancelled.png'))
            click('run-analysis')
            exact_qpcr(wait_completed(cancelled_id))
            assert controls.snapshot()['post_requests'] == 5
            assert value('state.job.id') not in {first_id, second_id, cancelled_id}
            checked('Cancelling a genuinely queued production task sends DELETE for its ID, commits no result and permits a subsequent real computation')

            click('tab', '[data-tab="home"]')
            pixels = np.arange(8000, dtype=np.uint16).reshape(80, 100)
            image = BytesIO()
            tifffile.imwrite(image, pixels, photometric='minisblack')
            with page.expect_file_chooser() as selected:
                click('start-task', '[data-task="microscopy"]')
            selected.value.set_files({'name': 'SYNTHETIC-keyboard-16bit.tif', 'mimeType': 'image/tiff', 'buffer': image.getvalue()})
            wait('!state.busy && state.analysisKind==="microscopy" && state.selectedAssetId && !analysisSelection().input.error')
            page.locator('#analysis-method').select_option('intensity')
            expect(page.locator('#source-svg')).to_be_visible()
            click('zoom-in')
            wait('state.sourceZoom>1')
            page.locator('[data-action="zoom-fit"]').focus()
            page.keyboard.press('Space')
            wait('state.sourceZoom===1 && !state.spacePressed')
            page.locator('[data-action="zoom-in"]').focus()
            page.keyboard.press('Enter')
            wait('state.sourceZoom>1')
            page.locator('[data-action="zoom-fit"]').focus()
            page.keyboard.press('Enter')
            wait('state.sourceZoom===1')
            previous = value('state.job.id')
            page.locator('[data-action="run-analysis"]').focus()
            page.keyboard.press('Space')
            result = wait_completed(previous)
            assert result['summary']['mean_intensity'] == float(pixels.mean())
            assert result['summary']['integrated_intensity'] == int(pixels.sum())
            assert result['summary']['source_bit_depth'] == 16
            assert not value('state.spacePressed')
            page.screenshot(path=str(output/'05-image-analysis-native-keyboard.png'))
            checked('Space and Enter activate native buttons on image analysis, including a real 16-bit intensity computation')

            help_button = page.locator('.workflow-meta [data-action="help"]')
            help_button.focus()
            page.keyboard.press('Space')
            expect(page.locator('[role="dialog"]')).to_be_visible()
            page.keyboard.press('Escape')
            expect(page.locator('[role="dialog"]')).to_have_count(0)
            expect(help_button).to_be_focused()
            assert not value('state.spacePressed')
            checked('Keyboard opens help from image analysis and Escape restores focus to that exact trigger')

            click('source-tool', '[data-tool="select"]')
            page.locator('#source-svg').scroll_into_view_if_needed()
            page.locator('#source-svg').click(position={'x': 80, 'y': 80})
            before = value('({project:state.project,pan:{...state.sourcePan}})')
            bounds = page.locator('#source-svg').bounding_box()
            start_x, start_y = bounds['x'] + 80, bounds['y'] + 80
            page.keyboard.down('Space')
            expect(page.locator('#source-svg')).to_have_class(re.compile(r'\bpannable\b'))
            page.mouse.move(start_x, start_y)
            page.mouse.down()
            page.mouse.move(start_x + 44, start_y + 26, steps=6)
            page.mouse.up()
            page.keyboard.up('Space')
            wait('!state.spacePressed && !state.interactionLock')
            after = value('({project:state.project,pan:{...state.sourcePan}})')
            assert abs(after['pan']['x'] - before['pan']['x'] - 44) < 2
            assert abs(after['pan']['y'] - before['pan']['y'] - 26) < 2
            assert after['project'] == before['project']
            assert 'pannable' not in (page.locator('#source-svg').get_attribute('class') or '')
            page.screenshot(path=str(output/'06-canvas-space-pan.png'))
            checked('Space plus pointer drag still pans the actual image canvas and releases cleanly without changing project data')

            # Import through the current microscopy workspace so importing the
            # fixture does not automatically start the separate WB wizard.
            gel_bytes = gel_image_bytes()
            gel_asset_id = hashlib.sha256(gel_bytes).hexdigest()
            with page.expect_file_chooser() as selected:
                click('import')
            selected.value.set_files({'name': 'SYNTHETIC-gel-task-lifecycle.tif', 'mimeType': 'image/tiff', 'buffer': gel_bytes})
            wait('!state.busy && state.selectedAssetId===' + json.dumps(gel_asset_id))
            click('tab', '[data-tab="gel"]')
            change('#lane-count', '6')
            wait('!state.dirty && state.saveState==="saved"')
            gel_before = value('({project:state.project,source:currentRawSource()})')
            assert gel_before['project']['id'] == project_id
            assert gel_before['source']['asset_id'] == gel_asset_id
            controls.delayed_pixel_asset = gel_asset_id
            controls.pixel_release.clear()
            click('detect-gel')
            wait('state.gelBusy && state.gelTaskToken')
            wait_network(lambda current: current['pixel_requests'] == 1)
            expect(page.locator('[data-action="detect-gel"]')).to_be_disabled()

            def assert_gel_project_guard():
                click('tab', '[data-tab="home"]')
                click('new-dialog')
                assert value('state.project.id') == project_id and value('state.modal') is None
                expect(page.locator('.operation-error')).to_contain_text('条带识别仍在运行')
                assert not controls.snapshot()['project_replacements']
                click('tab', '[data-tab="gel"]')
                expect(page.locator('[data-action="cancel-gel"]')).to_be_visible()

            def assert_gel_guard_cleared():
                expect(page.locator('.operation-error')).to_have_count(0, timeout=2000)
                expect(page.locator('#toast-region .toast').filter(has_text=re.compile(r'条带识别仍在运行.*再切换项目'))).to_have_count(0, timeout=2000)
                expect(page.locator('[data-action="detect-gel"]')).to_be_enabled()
                expect(page.locator('[data-action="cancel-gel"]')).to_have_count(0)

            assert_gel_project_guard()
            page.screenshot(path=str(output/'07-gel-active-project-guard.png'))
            click('cancel-gel')
            wait('!state.gelBusy && !state.gelTaskToken')
            assert_gel_guard_cleared()
            controls.pixel_release.set()
            wait_network(lambda current: current['active_pixel_requests'] == 0)
            assert value('state.gelSuggestions') is None
            assert value('({project:state.project,source:currentRawSource()})') == gel_before
            page.screenshot(path=str(output/'08-gel-cancelled.png'))

            controls.pixel_release.clear()
            click('detect-gel')
            wait('state.gelBusy && state.gelTaskToken')
            wait_network(lambda current: current['pixel_requests'] == 2)
            assert_gel_project_guard()
            controls.pixel_release.set()
            wait('!state.gelBusy && state.gelSuggestions')
            wait_network(lambda current: current['active_pixel_requests'] == 0)
            assert_gel_guard_cleared()
            assert value('state.project.id') == project_id
            assert value('state.selectedAssetId') == gel_asset_id
            assert value('currentRawSource()') == gel_before['source']
            gel = value('state.project.settings.asset_edits[state.selectedAssetId].gel')
            assert len(gel['lane_centers']) == 6 and len(gel['bands']) == 12
            np.testing.assert_allclose(gel['lane_centers'], [60, 160, 260, 360, 460, 560], rtol=0, atol=2)
            for index in range(6):
                positions = sorted(band['y'] for band in gel['bands'] if band['lane_index'] == index)
                np.testing.assert_allclose(positions, [85, 215], rtol=0, atol=2)
            assert gel['localization']['source_asset_id'] == gel_asset_id
            assert gel['localization']['requires_review'] is True
            assert controls.snapshot()['post_requests'] == 6
            report['gel_lifecycle'] = {
                'project_id': project_id, 'source': gel_before['source'],
                'pixel_requests': controls.snapshot()['pixel_requests'],
                'cancelled_project_unchanged': True,
                'lane_centers': gel['lane_centers'], 'band_count': len(gel['bands']),
                'localization': gel['localization'],
                'last_error': value('state.lastError'),
            }
            page.screenshot(path=str(output/'09-gel-detection-completed.png'))
            checked('Delayed real gel pixels preserve project ownership; cancellation and actual six-lane twelve-band Worker completion both clear the resolved project-switch guard without analysis POSTs')
            assert not report['browser_errors'], report['browser_errors']
            report['network_observations'] = controls.snapshot()
            assert report['network_observations']['post_requests'] == 6
            assert len(report['network_observations']['accepted_job_ids']) == 5
            assert report['network_observations']['rejected_posts'] == 1
        # Successful browser, HTTP server and real worker cleanup are required.
        report['passed'] = True
    except Exception as error:
        report['passed'] = False
        report.setdefault('failure', f'{type(error).__name__}: {error}')
        print(report['failure'], file=sys.stderr)
    finally:
        if controls:
            controls.release_all()
            report['network_observations'] = controls.snapshot()
        (output/'browser-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
