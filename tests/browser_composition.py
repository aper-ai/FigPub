"""Local-workbench composition acceptance using synthetic data only.

Run with Python development dependencies and Playwright Chromium installed:
    python tests/browser_composition.py
Optional: BROWSER_EXECUTABLE=/path/to/chromium
This script is separate from unit tests because it requires a real browser.
"""
from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fitz
from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright
import uvicorn
from run import reserve_port
from workbench.server import create_app


def main():
    output = Path(os.environ.get('FW_BROWSER_RESULTS', ROOT / 'test-results/composition'))
    output.mkdir(parents=True, exist_ok=True)
    report = {'synthetic_data': True, 'scenarios': [], 'browser_errors': [], 'passed': False}
    with tempfile.TemporaryDirectory(prefix='figure-composition-') as directory:
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
            raise RuntimeError('Test server did not become ready')
        try:
            with sync_playwright() as playwright:
                options = {'headless': True}
                if os.environ.get('BROWSER_EXECUTABLE'):
                    options['executable_path'] = os.environ['BROWSER_EXECUTABLE']
                browser = playwright.chromium.launch(**options)
                context = browser.new_context(viewport={'width': 1520, 'height': 1000}, accept_downloads=True)
                context.route('**/*', lambda route: route.continue_() if route.request.url.startswith((base + '/', 'data:', 'blob:')) else route.abort())
                page = context.new_page()
                page.set_default_timeout(15000)
                page.on('pageerror', lambda error: report['browser_errors'].append(str(error)))

                def state_value(expression):
                    return page.evaluate('async()=>{const {state}=await import("/modules/state.js");return (' + expression + ')}')

                def wait_state(expression):
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        if state_value(expression):
                            return
                        page.wait_for_timeout(40)
                    raise AssertionError('State did not converge: ' + expression)

                def click(action, suffix=''):
                    page.locator(f'[data-action="{action}"]{suffix}').first.click()

                def change(selector, value):
                    page.locator(selector).fill(str(value))
                    page.locator(selector).press('Tab')

                def passed(name):
                    report['scenarios'].append({'name': name, 'passed': True})
                    print('PASS', name, flush=True)

                try:
                    page.goto(base, wait_until='networkidle')
                    click('tab', '[data-tab="compose"]')
                    sources = []
                    for index in range(6):
                        image = Image.new('RGB', (1000 + index * 80, 700), (30 + index * 24, 95, 140))
                        ImageDraw.Draw(image).text((80, 80), f'SYNTHETIC PANEL {index + 1}', fill='white')
                        data = BytesIO()
                        image.save(data, format='PNG')
                        sources.append({'name': f'synthetic_{index + 1}.png', 'mimeType': 'image/png', 'buffer': data.getvalue()})
                    page.locator('#import-files').set_input_files(sources)
                    wait_state('state.tab==="compose" && state.project.panels.length===6 && !state.busy')
                    assert len(set(state_value('state.project.panels.map(p=>p.label)'))) == 6
                    click('layout-preset', '[data-columns="3"][data-rows="2"]')
                    wait_state('state.project.settings.composition.layout.columns===3')
                    assert state_value('state.project.settings.composition.layout.rows') == 2
                    passed('six-file import composes one batch and applies an explicit three-column grid')

                    page.locator('.figure-panel').nth(0).click()
                    wait_state('state.selectedPanelIds.length===1 && state.selectedPanelId===state.project.panels[0].id')
                    history = state_value('state.history.length')
                    page.locator('.figure-panel').nth(0).click()
                    assert state_value('state.history.length') == history
                    change('#panel-label', 'Control')
                    assert state_value('state.project.panels[0].label_mode') == 'manual'
                    before = state_value('JSON.stringify(state.project.panels)')
                    history = state_value('state.history.length')
                    click('composition-inspector', '[data-view="layout"]')
                    click('layout-preset', '[data-columns="2"][data-rows="2"]')
                    page.locator('.toast.error').last.wait_for()
                    assert state_value('JSON.stringify(state.project.panels)') == before
                    assert state_value('state.history.length') == history
                    passed('click-only gestures create no history and an undersized grid fails atomically')

                    page.locator('.figure-panel').nth(1).click(modifiers=['Shift'])
                    wait_state('state.selectedPanelIds.length===2')
                    click('group-panels')
                    wait_state('state.project.panels[0].group_id===state.project.panels[1].group_id && !!state.project.panels[0].group_id')
                    geometry = state_value('state.project.panels.slice(0,2).map(p=>[p.x_mm,p.y_mm,p.width_mm,p.height_mm])')
                    box = page.locator('.figure-panel').nth(0).bounding_box()
                    x, y = box['x'] + box['width'] * .5, box['y'] + box['height'] * .5
                    page.mouse.move(x, y)
                    page.mouse.down()
                    page.mouse.move(x + 32, y + 12, steps=8)
                    page.mouse.up()
                    moved = state_value('state.project.panels.slice(0,2).map(p=>[p.x_mm,p.y_mm,p.width_mm,p.height_mm])')
                    assert moved != geometry
                    assert abs((moved[0][0] - geometry[0][0]) - (moved[1][0] - geometry[1][0])) < .002
                    click('undo')
                    assert state_value('state.project.panels.slice(0,2).map(p=>[p.x_mm,p.y_mm,p.width_mm,p.height_mm])') == geometry
                    page.locator('.figure-panel').nth(0).click()
                    wait_state('state.selectedPanelIds.length===2')
                    handle = page.locator('[data-selection-handle="se"]').bounding_box()
                    x, y = handle['x'] + handle['width'] / 2, handle['y'] + handle['height'] / 2
                    page.mouse.move(x, y)
                    page.mouse.down()
                    page.mouse.move(x + 18, y + 9, steps=8)
                    page.mouse.up()
                    resized = state_value('state.project.panels.slice(0,2).map(p=>[p.x_mm,p.y_mm,p.width_mm,p.height_mm])')
                    assert abs(resized[0][2] / geometry[0][2] - resized[1][2] / geometry[1][2]) < .002
                    click('undo')
                    passed('a group moves and scales together, with one-step gesture undo')

                    click('composition-inspector', '[data-view="layout"]')
                    page.locator('#journal-preset').select_option('nature-single')
                    click('apply-journal-preset')
                    wait_state('state.project.figure.width_mm===89')
                    assert state_value('state.project.panels[0].label') == 'Control'
                    assert state_value('state.project.settings.composition.journal_preset_id') == 'nature-single'
                    wait_state('!state.dirty && state.saveState==="saved"')
                    page.reload(wait_until='networkidle')
                    click('tab', '[data-tab="compose"]')
                    assert state_value('state.project.figure.width_mm') == 89
                    assert state_value('state.project.panels[0].label') == 'Control'
                    assert state_value('!!state.project.panels[0].group_id')
                    passed('journal adaptation, manual captions and groups survive automatic save and reload')

                    with page.expect_download() as pending:
                        click('save-project')
                    archive_path = output / 'composition_roundtrip.figwork'
                    pending.value.save_as(archive_path)
                    with zipfile.ZipFile(archive_path) as archive:
                        saved = json.loads(archive.read('project.json'))
                        assert saved['figure']['width_mm'] == 89 and len(saved['assets']) == 6
                        assert saved['panels'][0]['label'] == 'Control'
                    click('export-dialog')
                    with page.expect_download() as pending:
                        click('export-file')
                    pdf_path = output / 'composition.pdf'
                    pending.value.save_as(pdf_path)
                    with fitz.open(pdf_path) as pdf:
                        assert abs(pdf[0].rect.width * 25.4 / 72 - 89) < .01
                        assert 'Control' in pdf[0].get_text()
                    page.screenshot(path=str(output / 'composition.png'))
                    passed('complete source archive and correctly sized native PDF download')
                    assert not report['browser_errors'], report['browser_errors']
                    report['passed'] = True
                except Exception as error:
                    report['failure'] = f'{type(error).__name__}: {error}'
                    try:
                        page.screenshot(path=str(output/'failure.png'), full_page=True)
                        report['failure_state'] = state_value('({selectedPanelId:state.selectedPanelId,selectedPanelIds:state.selectedPanelIds,history:state.history.length,busy:state.busy,interactionLock:state.interactionLock,formPointerPending:state.formPointerPending,lastError:state.lastError})')
                    except Exception as capture_error:
                        report['capture_error'] = str(capture_error)
                    raise
                finally:
                    browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
