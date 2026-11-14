"""Actual UI acceptance: readable workstation, precise data and portable output.

Uses production controls with an isolated service, synthetic data with a known
numerical answer, real downloads and read-only state inspection. No UI workflow
or result is replaced by a mocked successful response.
"""
from __future__ import annotations

from contextlib import ExitStack
import csv
import hashlib
from io import StringIO
import json
import math
import os
from pathlib import Path
import sys
import time
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fitz
import numpy as np
from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright
from browser_onboarding import capture_failure, isolated_service


CONTROL, TREATMENT = '对照组', '处理组'
PRECISE_CT = 18.123456789
EXPECTED_FOLD = 2 ** (20 - PRECISE_CT)


def qpcr_text():
    rows = ['sample\ttarget\tct\tgroup\tbiological_replicate\ttechnical_replicate']
    for group in (CONTROL, TREATMENT):
        for index in range(1, 4):
            sample = f'{group} 样本 {index}'
            for target, ct in [('ACTB', 18), ('MYC', 20 if group == CONTROL else PRECISE_CT)]:
                rows.append(f'{sample}\t{target}\t{ct}\t{group}\t{sample}\t1')
    return b'\xff\xfe' + ('\n'.join(rows) + '\n').encode('utf-16-le')


def main():
    output = Path(os.environ.get('FW_BROWSER_RESULTS', ROOT / 'test-results/professional-ui'))
    output.mkdir(parents=True, exist_ok=True)
    report = {'real_browser': True, 'synthetic_data': True, 'scenarios': [],
              'browser_errors': [], 'passed': False, 'layout_observations': []}

    def checked(name):
        report['scenarios'].append({'name': name, 'passed': True})
        print('PASS', name, flush=True)

    try:
        with isolated_service() as base, sync_playwright() as playwright, ExitStack() as evidence:
            options = {'headless': True}
            if os.environ.get('BROWSER_EXECUTABLE'):
                options['executable_path'] = os.environ['BROWSER_EXECUTABLE']
            browser = playwright.chromium.launch(**options)
            context = browser.new_context(viewport={'width': 1280, 'height': 800},
                                          device_scale_factor=1, accept_downloads=True)
            context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(
                (base + '/', 'data:', 'blob:')) else route.abort())
            page = context.new_page()
            page.set_default_timeout(15000)
            page.on('pageerror', lambda error: report['browser_errors'].append(str(error)))
            analysis_requests = []
            page.on('request', lambda request: analysis_requests.append(request.url)
                    if request.method == 'POST' and request.url == base + '/api/jobs' else None)
            evidence.enter_context(capture_failure(page, output, report))

            def value(expression):
                return page.evaluate('async()=>{const {state,analysisSelection}=await import("/modules/state.js");return (' + expression + ')}')

            def wait(expression, seconds=30):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    if value(expression):
                        return
                    page.wait_for_timeout(40)
                raise AssertionError('Timed out: ' + expression + '; error=' + str(value('state.lastError')))

            def click(action, extra=''):
                page.locator(f'[data-action="{action}"]{extra}').first.click()

            def change(selector, text):
                page.locator(selector).fill(str(text))
                page.locator(selector).press('Tab')

            def saved():
                wait('!state.busy && !state.dirty && state.saveState==="saved"')

            def reveal(selector):
                # Font/resize updates can replace a resolved node between
                # Playwright's visibility check and its scroll operation.
                # Re-resolve only this transient driver error; timeouts and
                # actual visibility failures must still fail acceptance.
                for attempt in range(10):
                    try:
                        page.locator(selector).scroll_into_view_if_needed()
                        return
                    except PlaywrightError as error:
                        if 'not attached to the DOM' not in str(error) or attempt == 9:
                            raise
                        page.wait_for_timeout(40)

            def layout_snapshot(name, width, height):
                page.set_viewport_size({'width': width, 'height': height})
                # Font readiness is part of the actual exported/readable layout.
                page.evaluate('document.fonts.ready')
                expect(page.locator('.app-header')).to_be_visible()
                metrics = page.evaluate('''() => ({
                    width: innerWidth, documentWidth: document.documentElement.scrollWidth,
                    headerHeight: document.querySelector('.app-header').getBoundingClientRect().height,
                    titleFontSize: parseFloat(getComputedStyle(document.querySelector('#project-title')).fontSize),
                    headerControls: ['save-project','export-dialog'].map(action=>{
                        const element=document.querySelector(`.app-header [data-action="${action}"]`);
                        if(!element)return {action,missing:true};
                        const bounds=element.getBoundingClientRect();
                        return {action,x:bounds.x,y:bounds.y,width:bounds.width,height:bounds.height};
                    })
                })''')
                assert metrics['documentWidth'] <= width + 1, metrics
                assert metrics['titleFontSize'] >= 12, metrics
                for bounds in metrics['headerControls']:
                    assert not bounds.get('missing') and bounds['width'] > 0 and bounds['height'] > 0, bounds
                    assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width + 1, bounds
                    assert bounds['y'] >= 0 and bounds['y'] + bounds['height'] <= metrics['headerHeight'] + 1, bounds
                report['layout_observations'].append({'screen': name, 'height': height, **metrics})
                page.screenshot(path=str(output / f'{name}-{width}.png'))

            page.goto(base, wait_until='networkidle')
            assert value('state.tab') == 'home'
            for width, height in ((1024, 768), (1280, 800), (1440, 900)):
                layout_snapshot('home', width, height)
                for task in ('wb', 'microscopy', 'qpcr', 'compose'):
                    selector = f'.home-task[data-task="{task}"]'
                    reveal(selector)
                    expect(page.locator(selector)).to_be_visible()
                reveal('.home-project-tools')
                expect(page.locator('.home-project-tools [data-action="open-project"]')).to_be_visible()
            checked('Responsive task home and project actions remain reachable at 1024, 1280 and 1440 pixels')

            page.set_viewport_size({'width': 1280, 'height': 800})
            source = qpcr_text()
            source_id = hashlib.sha256(source).hexdigest()
            source_name = '合成验收 数据输入与输出精度检查 UTF16 实验结果.txt'
            with page.expect_file_chooser() as picker:
                click('start-task', '[data-task="qpcr"]')
            picker.value.set_files({'name': source_name, 'mimeType': 'text/plain', 'buffer': source})
            wait('!state.busy && !state.tableLoading && state.table?.canonical')
            assert value('state.table.rows.length') == 12
            template_geometry = page.evaluate('''() => {
                const button=document.querySelector('.qpcr-intro [data-action="qpcr-template"]');
                const icon=button.querySelector('.icon');
                const b=button.getBoundingClientRect(),i=icon.getBoundingClientRect();
                return {button:{left:b.left,top:b.top,right:b.right,bottom:b.bottom},
                    icon:{left:i.left,top:i.top,right:i.right,bottom:i.bottom,width:i.width,height:i.height},
                    contained:i.width>0 && i.height>0 && i.left>=b.left && i.top>=b.top &&
                        i.right<=b.right && i.bottom<=b.bottom};
            }''')
            assert template_geometry['contained'], template_geometry
            report['template_icon_geometry'] = template_geometry
            assert page.locator('[data-action="view-analysis-results"]').count() == 0
            expect(page.locator('[data-action="run-analysis"]')).to_be_disabled()
            change('#analysis-reference_gene', 'ACTB')
            change('#analysis-control_group', CONTROL)
            expect(page.locator('[data-action="run-analysis"]')).to_be_enabled()
            click('run-analysis')
            wait('state.job && ["done","error"].includes(state.job.status)')
            assert value('state.job.status') == 'done', value('state.job')
            wait('analysisSelection().persisted && analysisSelection().record.id===state.job.id')
            expect(page.locator('#analysis-results .card-heading .eyebrow')).to_have_text('已完成计算 · ΔΔCt 相对定量')
            result = value('analysisSelection().result')
            groups = {group['group']: group for group in result['groups']}
            assert groups[CONTROL]['mean'] == 1
            assert math.isclose(groups[TREATMENT]['mean'], EXPECTED_FOLD, rel_tol=1e-13)
            assert all(group['n_biological'] == 3 for group in groups.values())
            assert len(result['rows']) == 6
            measurement_id = value('analysisSelection().record.id')
            saved()
            before_navigation = value('state.project')
            assert len(analysis_requests) == 1
            for index, (width, height) in enumerate(((1024, 768), (1280, 800), (1440, 900))):
                page.set_viewport_size({'width': width, 'height': height})
                reveal('.analysis-heading')
                layout_snapshot('analysis', width, height)
                navigation = page.locator('[data-action="view-analysis-results"]')
                # Measure before clicking: automatic scrolling by the test tool
                # must not conceal an unreachable navigation control.
                page.wait_for_function('''() => {
                    const element=document.querySelector('#analysis-results-link');
                    if(!element)return false;
                    const bounds=element.getBoundingClientRect();
                    const viewport=element.closest('.analysis-workspace').getBoundingClientRect();
                    return bounds.width>0 && bounds.height>0 &&
                        bounds.top>=viewport.top && bounds.bottom<=viewport.bottom &&
                        bounds.left>=viewport.left && bounds.right<=viewport.right;
                }''')
                if index in (1, 2):
                    navigation.focus()
                    navigation.press('Enter' if index == 1 else 'Space')
                else:
                    navigation.click()
                # Read all bounds atomically before touching result locators.
                # A later download click is allowed to scroll and is not proof
                # that the explicit navigation actually revealed the actions.
                page.wait_for_function('''() => {
                    const result=document.querySelector('#analysis-results');
                    const workspace=document.querySelector('.analysis-workspace');
                    if(!result||!workspace)return false;
                    const view=workspace.getBoundingClientRect();
                    const actions=[...result.querySelectorAll('.result-actions [data-action]')];
                    return actions.length>=2 && actions.every(element=>{
                        const r=element.getBoundingClientRect();
                        return r.width>0 && r.height>0 && r.top>=view.top && r.bottom<=view.bottom &&
                            r.left>=view.left && r.right<=view.right;
                    });
                }''')
                assert page.evaluate('document.querySelector("#analysis-results").contains(document.activeElement)')
                assert value('state.project') == before_navigation
                assert value('state.job.id') == measurement_id
                assert len(analysis_requests) == 1
                page.screenshot(path=str(output / f'analysis-results-{width}.png'))
            checked('Pointer and keyboard result navigation reveals CSV and chart actions at three widths without recalculation or data edits')
            with page.expect_download() as downloaded:
                click('download-results')
            csv_path = output / '精确结果.csv'
            downloaded.value.save_as(csv_path)
            exported = list(csv.DictReader(StringIO(csv_path.read_text('utf-8-sig'))))
            assert len(exported) == 6
            assert {(row['group'], row['biological_replicate'], row['target']) for row in exported} == {
                (group, f'{group} 样本 {index}', 'MYC') for group in (CONTROL, TREATMENT) for index in range(1, 4)}
            for row in exported:
                assert math.isclose(float(row['fold_change']), 1 if row['group'] == CONTROL else EXPECTED_FOLD,
                                    rel_tol=1e-13)
            checked('UTF-16 Chinese data preserves full numerical precision and six biological sample identities through CSV output')

            page.set_viewport_size({'width': 1280, 'height': 800})
            saved()
            before = value('state.project')
            malformed = b'sample,target,ct,group,biological_replicate,technical_replicate\ns1,MYC,20,Control,s1,1,UNDECLARED\n'
            page.locator('#import-files').set_input_files({'name': 'extra-nonempty-column.csv',
                                                         'mimeType': 'text/csv', 'buffer': malformed})
            wait('!state.busy && !!state.lastError')
            expect(page.locator('.operation-error')).to_be_visible()
            assert '表头' in page.locator('.operation-error').inner_text()
            assert value('state.project') == before
            assert value('analysisSelection().record.id') == measurement_id
            click('dismiss-error')
            with page.expect_download() as downloaded:
                click('download-results')
            retry_csv = output / '错误恢复后结果.csv'
            downloaded.value.save_as(retry_csv)
            assert retry_csv.read_bytes() == csv_path.read_bytes()
            checked('Extra CSV fields are rejected visibly without changing the project or losing the previous valid result')

            click('plot-results')
            wait('!state.busy && state.tab==="compose" && state.project.panels.length===1')
            assert value('state.project.panels[0].analysis_source.measurement_id') == measurement_id
            click('select-panel')
            width_field = '[data-scope="panel"][data-field="width_mm"]'
            precise_width = 41.123456789
            change(width_field, precise_width)
            wait(f'state.project.panels[0].width_mm==={precise_width}')
            saved()
            expect(page.locator(width_field)).to_have_value('41.123')
            for _ in range(3):
                page.locator(width_field).focus()
                page.locator(width_field).press('Tab')
            change('#project-title', '合成验收：科研图像与数据 0.6')
            saved()
            assert value('state.project.panels[0].width_mm') == precise_width
            # A real edit to the concise displayed value is an intentional change.
            change(width_field, '41.123')
            wait('state.project.panels[0].width_mm===41.123')
            change(width_field, precise_width)
            saved()
            checked('Concise layout inputs preserve unedited precision and commit deliberate edits across native focus and save events')
            before_fit = value('state.project')
            fit_geometry = '''() => {
                const viewport=document.querySelector('.artboard-viewport');
                const svg=document.querySelector('#figure-svg');
                if(!viewport||!svg)return null;
                const box=viewport.getBoundingClientRect(),style=getComputedStyle(viewport);
                const content={
                    left:box.left+viewport.clientLeft+parseFloat(style.paddingLeft),
                    top:box.top+viewport.clientTop+parseFloat(style.paddingTop),
                    right:box.left+viewport.clientLeft+viewport.clientWidth-parseFloat(style.paddingRight),
                    bottom:box.top+viewport.clientTop+viewport.clientHeight-parseFloat(style.paddingBottom)
                };
                const figure=svg.getBoundingClientRect();
                return {content,figure:{left:figure.left,top:figure.top,right:figure.right,
                    bottom:figure.bottom,width:figure.width,height:figure.height},
                    viewBox:svg.getAttribute('viewBox'),scrollTop:viewport.scrollTop,
                    scrollLeft:viewport.scrollLeft,
                    fits:figure.width>0 && figure.height>0 &&
                        figure.left>=content.left-1 && figure.top>=content.top-1 &&
                        figure.right<=content.right+1 && figure.bottom<=content.bottom+1 &&
                        viewport.scrollWidth<=viewport.clientWidth+1 &&
                        viewport.scrollHeight<=viewport.clientHeight+1};
            }'''
            for width, height in ((1024, 768), (1280, 800), (1440, 900)):
                page.set_viewport_size({'width': width, 'height': height})
                click('zoom-in')
                click('zoom-in')
                click('zoom-fit')
                page.wait_for_function('() => { const geometry=(' + fit_geometry + ')(); return geometry?.fits; }')
                geometry = page.evaluate(fit_geometry)
                assert geometry['scrollTop'] == 0 and geometry['scrollLeft'] == 0, geometry
                assert tuple(map(float, geometry['viewBox'].split())) == (0, 0,
                    before_fit['figure']['width_mm'], before_fit['figure']['height_mm']), geometry
                assert math.isclose(geometry['figure']['width'] / geometry['figure']['height'],
                                    before_fit['figure']['width_mm'] / before_fit['figure']['height_mm'], rel_tol=.001)
                assert value('state.figureZoom') == 1
                assert value('state.project') == before_fit
                report['layout_observations'].append({'screen': 'fitted-canvas', 'width': width, 'height': height, **geometry})
                layout_snapshot('composer', width, height)
                reveal(width_field)
                expect(page.locator(width_field)).to_be_visible()
            checked('Fit to window reveals the entire canvas at three viewport sizes without scroll overflow or changes to physical geometry')

            with page.expect_download() as downloaded:
                click('save-project')
            archive = output / '完整项目 精度与来源.figwork'
            downloaded.value.save_as(archive)
            wait('!state.busy')
            with ZipFile(archive) as backup:
                project = json.loads(backup.read('project.json'))
                manifest = json.loads(backup.read('manifest.json'))
                assert hashlib.sha256(backup.read('project.json')).hexdigest() == manifest['project_sha256']
                assert project['panels'][0]['width_mm'] == precise_width
                assert source_id in {record['id'] for record in manifest['assets']}
                for record in manifest['assets']:
                    assert hashlib.sha256(backup.read(record['path'])).hexdigest() == record['sha256']
                    if record['id'] == source_id:
                        assert backup.read(record['path']) == source
            prior_project_id = value('state.project.id')
            click('tab', '[data-tab="home"]')
            with page.expect_file_chooser() as picker:
                click('open-project')
            picker.value.set_files(archive)
            wait('!state.busy && state.modal?.type==="switch-project"')
            click('confirm-project-switch')
            wait('!state.busy && !state.modal && state.project.title===' + json.dumps(project['title']) +
                 ' && !' + json.dumps([prior_project_id, project['id']]) + '.includes(state.project.id)')
            # The archive intentionally matches the current project. A fresh
            # session identity proves it was restored rather than left in place.
            restored_id = value('state.project.id')
            assert restored_id not in {prior_project_id, project['id']}
            report['project_roundtrip'] = {'before': prior_project_id, 'archive': project['id'], 'restored': restored_id}
            assert value('state.project.panels') == project['panels']
            assert value('state.project.measurements') == project['measurements']
            original = page.request.get(base + f'/api/assets/{source_id}/original')
            assert original.ok and original.body() == source
            checked('Portable project restores exact source bytes, full precision geometry and recorded analysis provenance')

            page.set_viewport_size({'width': 1280, 'height': 800})
            click('tab', '[data-tab="compose"]')
            click('export-dialog')
            page.locator('label.export-format:has([data-export-format][value="pdf"])').click()
            with page.expect_download(timeout=30000) as downloaded:
                click('export-file')
            pdf_path = output / '精确尺寸图稿.pdf'
            downloaded.value.save_as(pdf_path)
            wait('!state.busy && !state.modal')
            with fitz.open(pdf_path) as document:
                assert len(document) == 1
                assert math.isclose(document[0].rect.width, project['figure']['width_mm'] * 72 / 25.4, abs_tol=.02)
                assert math.isclose(document[0].rect.height, project['figure']['height_mm'] * 72 / 25.4, abs_tol=.02)
                # The exporter always paints a page background, so merely
                # counting drawing objects would accept a blank PDF. Inspect
                # the chart interior, excluding panel borders and its label.
                panel = project['panels'][0]
                scale = 72 / 25.4
                interior = fitz.Rect((panel['x_mm'] + panel['width_mm'] * .1) * scale,
                                     (panel['y_mm'] + panel['height_mm'] * .1) * scale,
                                     (panel['x_mm'] + panel['width_mm'] * .9) * scale,
                                     (panel['y_mm'] + panel['height_mm'] * .9) * scale)
                rendered = document[0].get_pixmap(matrix=fitz.Matrix(2, 2), clip=interior, alpha=False)
                pixels = np.frombuffer(rendered.samples, dtype=np.uint8).reshape(rendered.height, rendered.width, rendered.n)
                ink_pixels = int(np.count_nonzero(np.any(pixels[:, :, :3] < 240, axis=2)))
                assert ink_pixels > 30, f'The exported chart interior is blank: {ink_pixels} nonbackground pixels'
                report['pdf_chart_interior_pixels'] = ink_pixels
                rendered.save(output / 'exported-chart-interior.png')
                document[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5)).save(output / 'exported-pdf.png')
            checked('Reopened project exports a nonempty PDF with verified physical page dimensions')
            assert not report['browser_errors'], report['browser_errors']
            context.close()
            browser.close()
        report['passed'] = True
    except Exception as error:
        report['passed'] = False
        report.setdefault('failure', f'{type(error).__name__}: {error}')
        print(report['failure'], file=sys.stderr)
    finally:
        (output / 'browser-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
