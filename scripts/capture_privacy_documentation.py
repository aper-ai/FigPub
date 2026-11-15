"""Capture documentation from the app using only the labelled AI demo input.

This script and its output live outside the client payload. The AI image is a
fictional documentation illustration; no measured result here is experimental.
"""
from __future__ import annotations

import hashlib
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
from run import reserve_port
from workbench.server import create_app


def main():
    source = ROOT / 'docs/examples/ai-wb-0.6.2/ai-generated-wb.png'
    output = Path(os.environ.get('FW_DOC_OUTPUT', ROOT / 'docs/screenshots/0.6.2'))
    output.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    errors = []
    with tempfile.TemporaryDirectory(prefix='figure-ai-documentation-') as directory:
        listener = reserve_port(0)
        port = listener.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        app = create_app(directory)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error'))
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if server.started:
                    break
                time.sleep(.025)
            if not server.started:
                raise RuntimeError('Documentation server did not start.')
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, executable_path=os.environ.get('BROWSER_EXECUTABLE'))
                context = browser.new_context(viewport={'width': 1600, 'height': 1040}, device_scale_factor=1)
                context.route('**/*', lambda route: route.continue_() if route.request.url.startswith((base+'/', 'data:', 'blob:')) else route.abort())
                page = context.new_page()
                page.set_default_timeout(20000)
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base, wait_until='networkidle')
                page.locator('[data-action="import"]').first.wait_for()
                assert app.state.store.get_project()['assets'] == []
                assert page.locator('[data-action="demo"]').count() == 0
                page.evaluate('document.fonts.ready')
                page.screenshot(path=str(output / 'home-empty.png'))
                with page.expect_file_chooser() as chooser:
                    page.locator('[data-action="import"]').first.click()
                chooser.value.set_files(str(source))
                page.wait_for_function("async()=>{const {state}=await import('/modules/state.js');return state.project.assets.length===1&&!state.busy}")
                page.locator('[data-action="tab"][data-tab="gel"]').click()
                page.locator('#source-svg').wait_for()
                page.locator('#project-title').fill('AI 生成演示 · 非实验数据')
                page.locator('#project-title').press('Tab')
                page.locator('#lane-count').fill('6')
                page.locator('#lane-count').press('Tab')
                page.locator('[data-action="detect-gel"]').click()
                page.wait_for_function("async()=>{const {state}=await import('/modules/state.js');return !state.gelBusy&&state.project.assets.length===1}")
                page.wait_for_function("async()=>{const {state}=await import('/modules/state.js');return !state.dirty&&state.saveState==='saved'}")
                page.evaluate('document.fonts.ready')
                page.screenshot(path=str(output / 'wb-ai-demonstration.png'))
                project = app.state.store.get_project()
                assert len(project['assets']) == 1
                assert project['assets'][0]['id'] == digest
                assert not errors, errors
                result = {'software_version': '0.6.2', 'source_image': str(source.relative_to(ROOT)),
                          'source_sha256': digest, 'source_provenance': 'Generated from scratch with imagegen; no user images referenced.',
                          'experimental_data': False, 'scientific_validation': False,
                          'screenshots': {'home-empty.png': 'Actual blank first launch; no imported assets.',
                                          'wb-ai-demonstration.png': 'Actual app after importing the labelled AI image and running lane localization.'},
                          'packaged_in_runtime': False, 'browser_errors': errors}
                (output / 'manifest.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
                print(json.dumps(result, ensure_ascii=False, indent=2))
                context.close()
                browser.close()
        finally:
            server.should_exit = True
            thread.join(10)
            listener.close()


if __name__ == '__main__':
    main()
