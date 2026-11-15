"""Real Chromium acceptance of automatic property application in the browser studio.

Build first with ``node scripts/build_browser_studio.mjs``. Run with
``BROWSER_EXECUTABLE=/usr/bin/chromium python tests/browser_auto_apply.py``.
The test uses public controls, rendered geometry and downloaded project/export
files. Its images are temporary geometric fixtures, never bundled user data.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from zipfile import ZipFile

from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
GEOMETRY = """()=>Array.from(document.querySelectorAll('#studio-svg > [data-object]'))
  .filter(group=>group.querySelector('image')).map(group=>({
    id:group.dataset.object,transform:group.getAttribute('transform'),
    width:Number(group.querySelector('clipPath rect').getAttribute('width')),
    height:Number(group.querySelector('clipPath rect').getAttribute('height'))
  }))"""


def main():
    version = json.loads((ROOT / 'package.json').read_text('utf-8'))['version']
    html = Path(os.environ.get('BROWSER_STUDIO_HTML', ROOT / f'dist/Figure_Workbench_{version}_Browser.html')).resolve()
    if not html.exists():
        subprocess.run(['node', 'scripts/build_browser_studio.mjs', str(html)], cwd=ROOT, check=True)
    output = Path(os.environ.get('FW_AUTO_APPLY_RESULTS', ROOT / 'test-results/browser-auto-apply'))
    output.mkdir(parents=True, exist_ok=True)
    report = {'entry': html.name, 'html_sha256': hashlib.sha256(html.read_bytes()).hexdigest(), 'html_bytes': html.stat().st_size,
              'real_browser': True, 'synthetic_data': True, 'scenarios': [],
              'browser_errors': [], 'network_attempts': [], 'passed': False}
    started = time.perf_counter()
    current_page = None

    try:
        with tempfile.TemporaryDirectory(prefix='auto-apply-fixtures-') as directory, sync_playwright() as playwright, ExitStack() as evidence:
            fixtures = Path(directory)
            files = []
            for index, color in enumerate(['#27644e', '#3865ad', '#ae6d29', '#8b3974']):
                image = Image.new('RGB', (600, 400), 'white')
                drawing = ImageDraw.Draw(image)
                drawing.rectangle((20, 20, 580, 380), fill=color)
                drawing.rectangle((80 + index * 15, 70, 220 + index * 15, 210), fill='#e7eedc')
                drawing.ellipse((320, 120, 510, 310), fill='#e5b966')
                path = fixtures / f'SYNTHETIC_GEOMETRY_{index}.png'
                image.save(path); files.append(path)
            launch = {'headless': True}
            if os.environ.get('BROWSER_EXECUTABLE'):
                launch['executable_path'] = os.environ['BROWSER_EXECUTABLE']
            browser = playwright.chromium.launch(**launch)

            @evidence.push
            def capture_failure(error_type, error, traceback):
                if error_type is not None and current_page and not current_page.is_closed():
                    try:
                        current_page.screenshot(path=str(output / 'failure.png'), full_page=True, timeout=5000)
                        (output / 'failure-state.json').write_text(json.dumps({
                            'body': current_page.locator('body').inner_text(timeout=5000),
                            'active': current_page.evaluate('()=>({id:document.activeElement?.id,name:document.activeElement?.name,value:document.activeElement?.value})'),
                        }, ensure_ascii=False, indent=2), encoding='utf-8')
                    except Exception as capture_error:
                        report['failure_capture_error'] = str(capture_error)
                return False

            def idle(page):
                page.locator('body:not(.busy)').wait_for(state='attached')

            def click(page, action):
                page.locator(f'[data-action="{action}"]').first.click(); idle(page)

            def geometry(page):
                return page.evaluate(GEOMETRY)

            def changed_geometry(page, before):
                page.wait_for_function(f'before=>JSON.stringify(({GEOMETRY})())!==JSON.stringify(before)', arg=before)

            def wait_canvas(page, width, height=120):
                expect(page.locator('#studio-svg')).to_have_attribute('viewBox', f'0 0 {width} {height}')

            def save_project(page):
                with page.expect_download(timeout=30000) as pending:
                    click(page, 'save')
                with ZipFile(pending.value.path()) as archive:
                    encoded = archive.read('project.json')
                    manifest = json.loads(archive.read('manifest.json'))
                    assert hashlib.sha256(encoded).hexdigest() == manifest['project_sha256']
                    return json.loads(encoded)

            def object_field(page, name):
                return page.locator(f'#object-form [name="{name}"]')

            def select_panel(page, index=0):
                page.locator('.asset-row').nth(index).click()
                expect(page.locator('#object-form')).to_be_visible()

            def presets_and_controls(page):
                assert not page.locator('.properties button').filter(has_text='应用尺寸').count()
                assert not page.locator('.properties button').filter(has_text='应用版式').count()
                select_panel(page)
                assert not page.locator('#object-form button[type="submit"]').count()
                previous = geometry(page)
                page.locator('#journal-preset').select_option('nature-single')
                wait_canvas(page, 89)
                changed_geometry(page, previous)
                expect(page.locator('.asset-label').first).to_have_text('a')
                click(page, 'undo'); wait_canvas(page, 180)
                expect(page.locator('#journal-preset')).to_have_value('')
                click(page, 'redo'); wait_canvas(page, 89)
                expect(page.locator('#journal-preset')).to_have_value('nature-single')
                page.locator('#layout-preset').select_option('hero-left')
                changed_geometry(page, previous)
                project = save_project(page)
                assert project['settings']['composition']['layout']['preset'] == 'hero-left'
                assert project['panels'][0]['width_mm'] > project['panels'][1]['width_mm']

            def debounced_numeric_and_noop(page):
                field = page.locator('#canvas-width')
                field.fill('90'); field.fill('150')
                wait_canvas(page, 150)
                expect(field).to_be_focused()
                # A blur/change event following the debounce must not create a
                # second transaction, nor may submitting an identical value.
                field.fill('150'); field.press('Tab')
                click(page, 'undo'); wait_canvas(page, 180)
                expect(page.locator('[data-action="redo"]')).to_be_enabled()
                page.locator('#canvas-width').fill('180'); page.locator('#canvas-width').press('Tab')
                expect(page.locator('[data-action="redo"]')).to_be_enabled()
                click(page, 'redo'); wait_canvas(page, 150)

            def invalid_drafts(page):
                original = geometry(page)
                for value in ['', '2', '2001']:
                    page.locator('#canvas-width').fill(value)
                    page.wait_for_timeout(900)
                    wait_canvas(page, 180)
                    assert geometry(page) == original
                page.locator('#canvas-width').fill('180'); page.locator('#canvas-width').press('Tab')
                for value in ['', '1.5', '33']:
                    page.locator('#layout-form [name="columns"]').fill(value)
                    page.wait_for_timeout(900)
                    assert geometry(page) == original
                page.locator('#layout-form [name="columns"]').fill('2')
                page.locator('#layout-form [name="columns"]').press('Tab')
                page.locator('#layout-form [name="margin_mm"]').fill('60')
                page.locator('#layout-form [name="margin_mm"]').press('Tab')
                page.wait_for_timeout(900)
                assert geometry(page) == original
                downloads = []
                capture_download = downloads.append
                page.on('download', capture_download)
                click(page, 'save')
                page.wait_for_timeout(500)
                assert not downloads, 'An invalid pending edit must block an ambiguous project save'
                page.remove_listener('download', capture_download)
                page.locator('#layout-form [name="margin_mm"]').focus()
                page.keyboard.press('Escape')
                expect(page.locator('#layout-form [name="margin_mm"]')).to_have_value('6')
                project = save_project(page)
                assert project['figure']['width_mm'] == 180
                assert project['settings']['composition']['layout']['margin_mm'] == 6

            def text_focus_and_composition(page):
                select_panel(page)
                title = object_field(page, 'title')
                title.fill('Alpha')
                title.evaluate('(element)=>element.setSelectionRange(2,2)')
                expect(page.locator('#studio-svg')).to_contain_text('Alpha')
                expect(title).to_be_focused()
                assert title.evaluate('(element)=>element.selectionStart') == 2
                page.keyboard.insert_text('-')
                expect(title).to_have_value('Al-pha')
                expect(page.locator('#studio-svg')).to_contain_text('Al-pha')
                title.evaluate("""element=>{
                  element.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true,data:''}));
                  element.value='中文标题';element.setSelectionRange(4,4);
                  element.dispatchEvent(new InputEvent('input',{bubbles:true,data:'中文标题',inputType:'insertCompositionText',isComposing:true}));
                }""")
                page.wait_for_timeout(900)
                expect(page.locator('#studio-svg')).to_contain_text('Al-pha')
                expect(title).to_have_value('中文标题')
                title.evaluate("element=>element.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true,data:'中文标题'}))")
                expect(page.locator('#studio-svg')).to_contain_text('中文标题')
                expect(title).to_be_focused()
                assert title.evaluate('(element)=>element.selectionStart') == 4

            def unlocked_aspect_and_details(page):
                select_panel(page)
                before = geometry(page)[0]
                object_field(page, 'lock_aspect').uncheck()
                object_field(page, 'width_mm').fill(str(before['width'] * .8))
                page.wait_for_function(f'width=>Math.abs(({GEOMETRY})()[0].width-width)<.001', arg=before['width'] * .8)
                assert abs(geometry(page)[0]['height'] - before['height']) < .001
                expect(object_field(page, 'lock_aspect')).not_to_be_checked()
                object_field(page, 'height_mm').fill(str(before['height'] * .7))
                page.wait_for_function(f'height=>Math.abs(({GEOMETRY})()[0].height-height)<.001', arg=before['height'] * .7)
                assert abs(geometry(page)[0]['width'] - before['width'] * .8) < .001
                page.locator('#object-form details summary').click()
                object_field(page, 'crop_width').fill('300')
                object_field(page, 'crop_width').press('Tab')
                expect(page.locator('#object-form details')).to_have_attribute('open', '')
                expect(object_field(page, 'lock_aspect')).not_to_be_checked()
                project = save_project(page)
                assert project['panels'][0]['crop']['width'] == 300
                assert abs(project['panels'][0]['height_mm'] - before['height'] * .7) < .001

            def numbering_without_reflow(page):
                select_panel(page)
                field = object_field(page, 'x_mm')
                field.fill(str(float(field.input_value()) + 1)); field.press('Tab')
                positions = geometry(page)
                page.locator('#number-style').select_option('lower')
                expect(page.locator('.asset-label').first).to_have_text('a')
                assert geometry(page) == positions
                page.locator('#number-order').select_option('source')
                assert geometry(page) == positions
                page.locator('#number-enabled').uncheck()
                assert geometry(page) == positions
                page.locator('#number-enabled').check()
                page.locator('#journal-preset').select_option('nature-single')
                wait_canvas(page, 89)
                page.locator('#number-style').select_option('upper')
                page.locator('#canvas-width').fill('100'); page.locator('#canvas-width').press('Tab')
                wait_canvas(page, 100)
                expect(page.locator('#number-style')).to_have_value('upper')

            def immediate_save_and_export(page):
                select_panel(page)
                object_field(page, 'title').fill('Pending save')
                project = save_project(page)
                assert project['panels'][0]['title'] == 'Pending save'
                page.locator('#canvas-width').fill('150')
                project = save_project(page)
                assert project['figure']['width_mm'] == 150
                select_panel(page)
                object_field(page, 'title').fill('Pending export')
                click(page, 'export')
                page.locator('#export-format').select_option('svg')
                with page.expect_download(timeout=30000) as pending:
                    page.locator('#export-confirm').click()
                svg = Path(pending.value.path()).read_text('utf-8')
                assert 'Pending export' in svg and 'width="150mm"' in svg
                idle(page)

            def pending_selection_and_reset(page):
                select_panel(page)
                object_field(page, 'title').fill('First panel only')
                # Dispatch the public selection click before native blur, so a
                # pending timer cannot accidentally target the new selection.
                page.locator('.asset-row').nth(1).dispatch_event('click')
                page.wait_for_timeout(900)
                expect(object_field(page, 'title')).to_have_value('')
                project = save_project(page)
                assert project['panels'][1]['title'] == ''
                assert all(panel['title'] != 'First panel only' for panel in project['panels'][1:])
                object_field(page, 'title').fill('Must not survive reset')
                click(page, 'new')
                expect(page.locator('#panel-count')).to_have_text('0')
                page.wait_for_timeout(900)
                assert not page.locator('#studio-svg > [data-object]').count()
                assert not save_project(page)['panels']

            scenarios = [
                ('preset and layout choices apply without confirmation buttons', presets_and_controls),
                ('settled numeric edits preserve focus and one undo step; no-op preserves redo', debounced_numeric_and_noop),
                ('empty, out-of-range and impossible drafts preserve committed geometry', invalid_drafts),
                ('text debounce preserves caret and Chinese composition stays uncommitted until completion', text_focus_and_composition),
                ('unlocked aspect ratio and expanded crop settings survive automatic commits', unlocked_aspect_and_details),
                ('numbering options preserve manual placement and custom numbering survives resize', numbering_without_reflow),
                ('immediate project save and SVG export contain the latest pending values', immediate_save_and_export),
                ('pending edits cannot leak into another selection or a new workspace', pending_selection_and_reset),
            ]
            for index, (name, callback) in enumerate(scenarios, start=1):
                context = browser.new_context(viewport={'width': 1500, 'height': 1000}, accept_downloads=True)

                def block_network(route):
                    report['network_attempts'].append(route.request.url); route.abort()

                context.route('http://**/*', block_network); context.route('https://**/*', block_network)
                page = context.new_page(); current_page = page; page.set_default_timeout(12000)
                page.on('pageerror', lambda error: report['browser_errors'].append(str(error)))
                page.on('dialog', lambda dialog: dialog.accept())
                page.goto(html.as_uri(), wait_until='load'); idle(page)
                page.locator('#image-files').set_input_files(files)
                expect(page.locator('#panel-count')).to_have_text('4')
                page.locator('[data-grid="2,2"]').click(); idle(page)
                callback(page)
                page.screenshot(path=str(output / f'{index:02d}.png'), full_page=True)
                report['scenarios'].append({'name': name, 'passed': True})
                print('PASS', name, flush=True)
                context.close(); current_page = None
            assert not report['browser_errors'], report['browser_errors']
            assert not report['network_attempts'], report['network_attempts']
            assert hashlib.sha256(html.read_bytes()).hexdigest() == report['html_sha256'], 'Tested build changed during acceptance'
            browser.close(); report['passed'] = True
    except Exception as error:
        report['failure'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        report['elapsed_seconds'] = round(time.perf_counter() - started, 2)
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
