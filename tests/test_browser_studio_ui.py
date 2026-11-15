"""Real browser acceptance of the built, self-contained file:// application.

Run manually in an environment that permits Chromium:
  node scripts/build_browser_studio.mjs
  BROWSER_EXECUTABLE=/path/to/chromium python tests/test_browser_studio_ui.py

This is intentionally not launched by pytest. A browser launch failure is a
failed/unavailable acceptance run, never reported as passing UI coverage.
"""
from __future__ import annotations

import base64
from contextlib import ExitStack
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from zipfile import ZipFile, ZIP_DEFLATED

import fitz
import numpy as np
from PIL import Image
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def assert_raster_sources(image, project, originals):
    """Check the real exported pixels against each known original gradient.

    Interior probes avoid labels and image edges. Skip only source wrap-around
    discontinuities, where browser downsampling kernels legitimately differ.
    The four-level tolerance permits rounding, but not another source's colors.
    """
    pixels = np.asarray(image.convert('RGB'), dtype=np.int16)
    assert np.min(pixels.std(axis=(0, 1))) > 8, 'Exported figure is blank or lacks its color sources'
    probes = 0
    maximum_error = 0
    scale_x = image.width / project['figure']['width_mm']
    scale_y = image.height / project['figure']['height_mm']
    assert len(project['panels']) == 6
    for panel in project['panels']:
        assert not panel.get('rotation') and not panel.get('flip_h') and not panel.get('flip_v')
        source = np.asarray(originals[panel['asset_id']].convert('RGB'), dtype=np.int16)
        crop = panel['crop']
        radius = max(2, int(np.ceil(max(crop['width'] / (panel['width_mm'] * scale_x),
                                        crop['height'] / (panel['height_mm'] * scale_y)))) * 2)
        matched = 0
        for fx in (.2, .37, .53, .71, .83):
            for fy in (.2, .37, .53, .71, .83):
                px = int((panel['x_mm'] + panel['width_mm'] * fx) * scale_x)
                py = int((panel['y_mm'] + panel['height_mm'] * fy) * scale_y)
                sx = int(crop['x'] + ((px + .5) / scale_x - panel['x_mm']) / panel['width_mm'] * crop['width'])
                sy = int(crop['y'] + ((py + .5) / scale_y - panel['y_mm']) / panel['height_mm'] * crop['height'])
                patch = source[sy - radius:sy + radius + 1, sx - radius:sx + radius + 1]
                assert patch.shape[:2] == (radius * 2 + 1, radius * 2 + 1)
                if np.any(np.ptp(patch, axis=(0, 1)) > 12):
                    continue
                error = int(np.abs(pixels[py, px] - source[sy, sx]).max())
                assert error <= 4, f"Wrong rendered source/color at {panel['asset_id']} ({px}, {py}): error={error}"
                maximum_error = max(maximum_error, error)
                matched += 1
        assert matched >= 15, 'Too few independent interior source probes'
        probes += matched
    return {'source_pixel_probes': probes, 'maximum_rgb_error': maximum_error,
            'png_rgb_sha256': hashlib.sha256(pixels.astype(np.uint8).tobytes()).hexdigest()}


def main():
    version = json.loads((ROOT / 'package.json').read_text('utf-8'))['version']
    html = Path(os.environ.get('BROWSER_STUDIO_HTML', ROOT / f'dist/Figure_Workbench_{version}_Browser.html')).resolve()
    if not html.exists():
        subprocess.run(['node', 'scripts/build_browser_studio.mjs', str(html)], cwd=ROOT, check=True)
    output = Path(os.environ.get('FW_BROWSER_STUDIO_RESULTS', ROOT / 'test-results/browser-studio-ui'))
    output.mkdir(parents=True, exist_ok=True)
    report = {'entry': html.name, 'real_browser': True, 'synthetic_data': True,
              'html_sha256': hashlib.sha256(html.read_bytes()).hexdigest(), 'html_bytes': html.stat().st_size,
              'scenarios': [], 'browser_errors': [], 'network_attempts': [], 'passed': False}
    started = time.perf_counter()

    def checked(name):
        report['scenarios'].append({'name': name, 'passed': True})
        print('PASS', name, flush=True)

    try:
        with tempfile.TemporaryDirectory(prefix='browser-studio-fixtures-') as directory, sync_playwright() as playwright, ExitStack() as evidence:
            fixture_root = Path(directory); files = []
            y, x = np.indices((800, 1200))
            for index in range(6):
                pixels = np.stack([(x // 7 + index * 31) % 255, (y // 5 + index * 17) % 255,
                                   ((x + y) // 8 + index * 43) % 255], axis=-1).astype(np.uint8)
                path = fixture_root / f'SYNTHETIC_{index + 1}.png'
                Image.fromarray(pixels).save(path); files.append(path)
            safe_svg = fixture_root / 'safe.svg'
            safe_svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="600" height="400"><defs><linearGradient id="g"><stop stop-color="#20553a"/><stop offset="1" stop-color="#dbedc9"/></linearGradient></defs><rect width="600" height="400" fill="url(\'\u0023g\')"/></svg>')
            unsafe_svg = fixture_root / 'unsafe.svg'
            unsafe_svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="600" height="400"><script>alert(1)</script><rect width="600" height="400"/></svg>')
            options = {'headless': True}
            if os.environ.get('BROWSER_EXECUTABLE'):
                options['executable_path'] = os.environ['BROWSER_EXECUTABLE']
            browser = playwright.chromium.launch(**options)
            context = browser.new_context(viewport={'width': 1500, 'height': 1000}, accept_downloads=True)

            def block_network(route):
                report['network_attempts'].append(route.request.url)
                route.abort()

            context.route('http://**/*', block_network); context.route('https://**/*', block_network)
            page = context.new_page(); page.set_default_timeout(20000)
            page.on('pageerror', lambda error: report['browser_errors'].append(str(error)))

            @evidence.push
            def capture_failure(error_type, error, traceback):
                # Capture before the Playwright context exits, including failures
                # during startup. Keep the real page CSP enabled throughout.
                if error_type is not None and not page.is_closed():
                    try:
                        page.screenshot(path=str(output / 'failure.png'), full_page=True, timeout=5000)
                        (output / 'failure-state.json').write_text(json.dumps({
                            'url': page.url,
                            'body': page.locator('body').inner_text(timeout=5000),
                            'last_completed_scenario': report['scenarios'][-1] if report['scenarios'] else None,
                        }, ensure_ascii=False, indent=2), encoding='utf-8')
                    except Exception as capture_error:
                        report['failure_capture_error'] = str(capture_error)
                return False

            accept_dialog = lambda dialog: dialog.accept()
            page.on('dialog', accept_dialog)
            page.goto(html.as_uri(), wait_until='load')

            def idle():
                page.locator('body:not(.busy)').wait_for(state='attached')

            def click(action):
                page.locator(f'[data-action="{action}"]').first.click(); idle()

            def count(value):
                expect(page.locator('#panel-count')).to_have_text(str(value), timeout=20000)

            def labels():
                return [label.split(' · ')[0] for label in page.locator('.asset-label').all_text_contents()]

            def saved():
                expect(page.locator('#save-status')).to_contain_text('已自动保存', timeout=20000)

            def save_project(name='figure.figwork'):
                with page.expect_download(timeout=60000) as info:
                    click('save')
                path = output / name; info.value.save_as(path)
                with ZipFile(path) as archive:
                    project = json.loads(archive.read('project.json'))
                    manifest = json.loads(archive.read('manifest.json'))
                    assert hashlib.sha256(archive.read('project.json')).hexdigest() == manifest['project_sha256']
                    for source in manifest['assets']:
                        assert hashlib.sha256(archive.read(source['path'])).hexdigest() == source['sha256']
                return path, project

            def export_file(format_name):
                click('export'); page.locator('#export-format').select_option(format_name)
                if format_name != 'svg':
                    page.locator('#export-dpi').select_option('300')
                with page.expect_download(timeout=90000) as info:
                    page.locator('#export-confirm').click()
                path = output / f'figure.{format_name}'; info.value.save_as(path); idle()
                return path

            idle(); assert page.title().startswith('图研'); count(0)
            page.locator('#image-files').set_input_files(files); idle(); count(6)
            assert labels() == list('ABCDEF')
            page.locator('[data-grid="3,2"]').click(); idle()
            page.locator('#image-files').set_input_files(files[:1]); idle(); count(6)
            capacity_error = page.locator('#messages').inner_text()
            assert '只能容纳 6 个面板' in capacity_error and '本次操作需要容纳 7 个面板' in capacity_error
            page.screenshot(path=str(output / '01-grid.png'), full_page=True)
            checked('offline file:// startup, six original imports, automatic labels and 3×2 grid')

            page.locator('[data-grid="0,0"]').click(); idle()
            page.locator('#image-files').set_input_files([files[0]] * 21); idle(); count(27)
            assert labels()[-1] == 'AA'; assert len(set(labels())) == 27
            click('undo'); count(6)
            data = base64.b64encode(files[0].read_bytes()).decode()
            page.evaluate("""({data})=>{const raw=Uint8Array.from(atob(data),c=>c.charCodeAt(0));const transfer=new DataTransfer();transfer.items.add(new File([raw],'dragged.png',{type:'image/png'}));document.querySelector('#canvas-viewport').dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:transfer}));}""", {'data': data})
            idle(); count(7); click('undo'); count(6)
            checked('AA numbering, batch undo and a real DataTransfer drop')

            page.locator('.asset-row').first.click()
            page.locator('#object-form [name="label"]').fill('Target')
            page.locator('#object-form [name="label"]').press('Tab'); idle()
            assert labels()[0] == 'Target'
            page.locator('#layout-preset').select_option('hero-left'); idle()
            assert labels()[0] == 'Target'
            _, hero_project = save_project('hero.figwork')
            assert hero_project['panels'][0]['width_mm'] > hero_project['panels'][1]['width_mm']
            page.locator('.asset-row').first.click(); click('auto-label')
            page.locator('[data-grid="3,2"]').click(); idle()
            checked('hero layout and manually locked labels survive automatic rearrangement')

            click('select-all'); click('group')
            page.locator('.asset-row').first.click(); assert '6' in page.locator('#selection-info').inner_text()
            before = page.locator('#studio-svg > [data-object]').first.get_attribute('transform')
            box = page.locator('#studio-svg > [data-object]').first.bounding_box()
            page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2); page.mouse.down()
            page.mouse.move(box['x'] + box['width'] / 2 + 25, box['y'] + box['height'] / 2 + 9, steps=8); page.mouse.up(); idle()
            assert page.locator('#studio-svg > [data-object]').first.get_attribute('transform') != before
            click('undo'); assert page.locator('#studio-svg > [data-object]').first.get_attribute('transform') == before
            _, grouped_before = save_project('group-before.figwork')
            handle = page.locator('[data-resize="selection"]').bounding_box()
            page.mouse.move(handle['x'] + handle['width'] / 2, handle['y'] + handle['height'] / 2); page.mouse.down()
            page.mouse.move(handle['x'] + handle['width'] / 2 - 20, handle['y'] + handle['height'] / 2 - 15, steps=8); page.mouse.up(); idle()
            _, grouped_after = save_project('group-scaled.figwork')
            ratios = [after['width_mm'] / before['width_mm'] for before, after in zip(grouped_before['panels'], grouped_after['panels'])]
            assert .5 < ratios[0] < 1 and max(ratios) - min(ratios) < .001
            click('undo'); click('auto-layout'); assert '先解组' in page.locator('#messages').inner_text()
            click('ungroup'); page.locator('.asset-row').first.click()
            old_width = float(page.locator('#object-form [name="width_mm"]').input_value())
            old_height = float(page.locator('#object-form [name="height_mm"]').input_value())
            page.locator('#object-form [name="width_mm"]').fill(str(old_width * .9))
            page.locator('#object-form [name="width_mm"]').press('Tab'); idle()
            assert abs(float(page.locator('#object-form [name="height_mm"]').input_value()) / old_height - .9) < .001
            click('undo')
            # A pending save must capture committed geometry, never this cancelled live drag.
            before_cancel = page.locator('#studio-svg > [data-object]').first.get_attribute('transform')
            box = page.locator('#studio-svg > [data-object]').first.bounding_box()
            page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2); page.mouse.down()
            page.mouse.move(box['x'] + box['width'] / 2 + 35, box['y'] + box['height'] / 2 + 15, steps=5)
            page.wait_for_timeout(1100); page.keyboard.press('Escape'); page.mouse.up()
            assert page.locator('#studio-svg > [data-object]').first.get_attribute('transform') == before_cancel
            saved(); page.reload(wait_until='load'); idle(); count(0)
            expect(page.locator('#workspace-recovery-dialog')).to_be_visible()
            click('restore'); count(6)
            assert page.locator('#studio-svg > [data-object]').first.get_attribute('transform') == before_cancel
            checked('group drag/resize, atomic undo, grouped-layout refusal and cancelled-gesture autosave isolation')

            page.locator('#journal-preset').select_option('nature-single')
            idle()
            assert page.locator('#canvas-width').input_value() == '89'
            assert labels() == list('abcdef')
            _, resized = save_project('nature.figwork')
            assert all(panel['label_font_size_pt'] == 8 for panel in resized['panels'])
            assert resized['settings']['composition']['journal_preset_id'] == 'nature-single'
            assert all(panel['x_mm'] >= 0 and panel['x_mm'] + panel['width_mm'] <= 89.01 for panel in resized['panels'])
            page.locator('.asset-row').first.click()
            assert page.locator('#object-form').evaluate('(form)=>form.checkValidity()'), 'Fractional fitted dimensions must remain editable'
            checked('Nature physical width, lowercase 8pt numbering, fitted geometry and valid fractional fields')

            page.locator('#layout-form [name="rows"]').fill('0'); page.locator('#layout-form [name="rows"]').press('Tab'); idle()
            page.locator('#image-files').set_input_files(safe_svg); idle(); count(7)
            click('undo'); count(6)
            page.locator('#image-files').set_input_files([files[0], unsafe_svg]); idle(); count(6)
            assert any(word in page.locator('#messages').inner_text() for word in ['动态', '外部', '安全'])
            checked('safe quoted SVG gradients import; scripted SVG rejects the whole batch atomically')

            click('text')
            page.locator('#object-form [name="text"]').fill('中文图稿\nProtein expression')
            page.locator('#object-form [name="text"]').press('Tab')
            for name, value in [('x_mm', 14), ('y_mm', 106), ('width_mm', 60), ('height_mm', 7), ('font_size_pt', 6)]:
                page.locator(f'#object-form [name="{name}"]').fill(str(value))
                page.locator(f'#object-form [name="{name}"]').press('Tab')
            idle()
            saved(); page.screenshot(path=str(output / '02-nature.png'), full_page=True)
            project_path, document = save_project()
            assert len(document['annotations']) == 1 and '\n' in document['annotations'][0]['text']
            png = export_file('png'); image = Image.open(png)
            assert image.size == (round(89 / 25.4 * 300), round(120 / 25.4 * 300))
            assert abs(image.info['dpi'][0] - 300) < .02
            originals = {hashlib.sha256(path.read_bytes()).hexdigest(): Image.open(path).convert('RGB')
                         for path in files}
            report['raster_validation'] = assert_raster_sources(image, document, originals)
            svg = export_file('svg').read_text()
            assert 'data:font/woff2;base64,' in svg and 'data:image/png;base64,' in svg
            assert '<tspan' in svg and '中文图稿' in svg and 'blob:' not in svg
            with fitz.open(export_file('pdf')) as pdf:
                assert len(pdf) == 1
                assert abs(pdf[0].rect.width * 25.4 / 72 - 89) < .001
                assert len(pdf[0].get_images()) == 1 and not pdf[0].get_text()
                embedded = Image.open(BytesIO(pdf.extract_image(pdf[0].get_images()[0][0])['image'])).convert('RGB')
                assert embedded.size == image.size
                assert embedded.tobytes() == image.convert('RGB').tobytes(), 'PDF pixels differ from the validated PNG'
                report['raster_validation']['pdf_rgb_sha256'] = hashlib.sha256(embedded.tobytes()).hexdigest()
                report['raster_validation']['pdf_matches_png'] = True
            checked('Chinese multiline labels, embedded-font SVG, physical PNG and lossless raster PDF downloads')

            saved(); page.reload(wait_until='load'); idle(); count(0)
            expect(page.locator('#workspace-recovery-dialog')).to_be_visible()
            click('restore'); count(6)
            assert page.locator('#studio-svg > [data-object]').count() == 7
            page.locator('#project-file').set_input_files(project_path); idle(); count(6)
            _, reopened = save_project('reopened.figwork')
            assert reopened['panels'] == document['panels']
            assert reopened['annotations'] == document['annotations']
            assert [asset['id'] for asset in reopened['assets']] == [asset['id'] for asset in document['assets']]
            checked('IndexedDB explicit recovery and complete downloaded .figwork reopen preserve originals and layout')

            with ZipFile(project_path) as archive:
                members = {name: archive.read(name) for name in archive.namelist()}
            complex_project = json.loads(members['project.json']); complex_project['panels'][0]['display']['gamma'] = 1.2
            members['project.json'] = json.dumps(complex_project, ensure_ascii=False).encode()
            manifest = json.loads(members['manifest.json']); manifest['project_sha256'] = hashlib.sha256(members['project.json']).hexdigest()
            members['manifest.json'] = json.dumps(manifest).encode()
            unsupported = fixture_root / 'scientific.figwork'
            with ZipFile(unsupported, 'w', ZIP_DEFLATED) as archive:
                for name, value in members.items(): archive.writestr(name, value)
            page.locator('#project-file').set_input_files(unsupported); idle(); count(6)
            assert '本机工作台' in page.locator('#messages').inner_text()
            _, after_refusal = save_project('after-refusal.figwork')
            assert after_refusal['panels'] == reopened['panels']
            checked('unsupported scientific presentation is refused without replacing the active project')

            assert not report['network_attempts'], report['network_attempts']
            assert not report['browser_errors'], report['browser_errors']
            failed_storage = browser.new_context(viewport={'width': 1500, 'height': 1000}, accept_downloads=True)
            failed_storage.add_init_script("Object.defineProperty(globalThis,'indexedDB',{value:{open(){throw new DOMException('Simulated storage denial','SecurityError')}}});")
            blocked = failed_storage.new_page(); blocked.goto(html.as_uri(), wait_until='load')
            blocked.locator('body:not(.busy)').wait_for(state='attached')
            assert '自动保存不可用' in blocked.locator('#save-status').inner_text()
            blocked.locator('#image-files').set_input_files(files[:1])
            expect(blocked.locator('#panel-count')).to_have_text('1', timeout=20000)
            with blocked.expect_download() as manual_backup:
                blocked.locator('[data-action="save"]').click()
            manual_backup.value.save_as(output / 'manual-storage-fallback.figwork')
            checked('IndexedDB refusal stays visible and manual project backup remains usable')

            click('new'); count(0); click('text')
            _, annotation_only = save_project('annotation-only.figwork')
            assert len(annotation_only['annotations']) == 1 and not annotation_only['assets']
            dismissed = []

            def dismiss_replacement(dialog):
                dismissed.append(dialog.message)
                dialog.dismiss()

            page.remove_listener('dialog', accept_dialog)
            page.on('dialog', dismiss_replacement)
            assert page.locator('[data-action="demo"]').count() == 0
            click('new'); click('clear')
            page.locator('#project-file').set_input_files(project_path); idle(); count(0)
            assert len(dismissed) == 3
            assert page.locator('#studio-svg > [data-object]').count() == 1
            page.remove_listener('dialog', dismiss_replacement)
            page.on('dialog', accept_dialog)
            _, after_cancel = save_project('annotation-after-cancel.figwork')
            assert after_cancel['annotations'] == annotation_only['annotations']
            checked('annotation-only projects survive cancelled new, clear and open actions')
            # A transaction failure must keep both the current project and stored
            # originals, and expose an error instead of claiming successful cleanup.
            saved()
            page.evaluate("""()=>{window.fixtureClear=IDBObjectStore.prototype.clear;IDBObjectStore.prototype.clear=function(){if(this.name==='originals')throw new DOMException('Synthetic cleanup denial','SecurityError');return window.fixtureClear.call(this);};}""")
            click('clear'); count(0)
            assert 'Synthetic cleanup denial' in page.locator('#messages').inner_text()
            assert page.locator('#studio-svg > [data-object]').count() == 1
            stored_after_failure=page.evaluate("""()=>new Promise((resolve,reject)=>{const request=indexedDB.open('figure-workbench-browser-studio-v1',1);request.onsuccess=()=>{const db=request.result,tx=db.transaction(['workspace','originals'],'readonly'),active=tx.objectStore('workspace').get('active'),originals=tx.objectStore('originals').count();tx.oncomplete=()=>{resolve({project:active.result,count:originals.result});db.close();};tx.onerror=()=>reject(tx.error);};request.onerror=()=>reject(request.error);})""")
            assert len(stored_after_failure['project']['annotations']) == 1
            assert stored_after_failure['count'] > 0
            page.evaluate('()=>{IDBObjectStore.prototype.clear=window.fixtureClear;delete window.fixtureClear;}')
            checked('failed cleanup aborts the entire IndexedDB transaction and remains visible')
            click('clear'); count(0)
            assert page.locator('#studio-svg > [data-object]').count() == 0
            assert page.locator('[data-action="undo"]').is_disabled()
            page.wait_for_timeout(900)
            page.reload(wait_until='load'); idle(); count(0)
            assert not page.locator('#workspace-recovery-dialog').is_visible()
            remaining=page.evaluate("""()=>new Promise((resolve,reject)=>{const request=indexedDB.open('figure-workbench-browser-studio-v1',1);request.onsuccess=()=>{const db=request.result,tx=db.transaction(['workspace','originals'],'readonly'),active=tx.objectStore('workspace').get('active'),originals=tx.objectStore('originals').count();tx.oncomplete=()=>{resolve({workspace:active.result?1:0,originals:originals.result});db.close();};tx.onerror=()=>reject(tx.error);};request.onerror=()=>reject(request.error);})""")
            assert remaining == {'workspace':0,'originals':0}
            checked('confirmed cleanup removes stored copies, undo history and recovery across reload')
            # Both pages share the same real IndexedDB database. A generation
            # check in the write transaction must reject stale autosaves, even
            # when the other page still has all old image bytes in memory.
            page.locator('#image-files').set_input_files(files[:1]);idle();count(1);saved()
            other=context.new_page();other.on('dialog',accept_dialog)
            other.on('pageerror',lambda error:report['browser_errors'].append(str(error)))
            other.goto(html.as_uri(),wait_until='load')
            other.locator('body:not(.busy)').wait_for(state='attached')
            expect(other.locator('#panel-count')).to_have_text('0')
            expect(other.locator('#workspace-recovery-dialog')).to_be_visible()
            other.locator('[data-action="restore"]').click()
            other.locator('body:not(.busy)').wait_for(state='attached')
            expect(other.locator('#panel-count')).to_have_text('1')
            click('clear');count(0)
            other.locator('#project-title').fill('Unsaved synthetic work in second tab')
            other.locator('#project-title').press('Tab')
            expect(other.locator('#save-status')).to_contain_text('自动保存失败',timeout=20000)
            assert '其他窗口清理或替换' in other.locator('#messages').inner_text()
            expect(other.locator('#panel-count')).to_have_text('1')
            still_empty=page.evaluate("""()=>new Promise((resolve,reject)=>{const request=indexedDB.open('figure-workbench-browser-studio-v1',1);request.onsuccess=()=>{const db=request.result,tx=db.transaction(['workspace','originals'],'readonly'),active=tx.objectStore('workspace').get('active'),originals=tx.objectStore('originals').count(),epoch=tx.objectStore('workspace').get('workspace-epoch');tx.oncomplete=()=>{resolve({active:!!active.result,originals:originals.result,epoch:epoch.result});db.close();};tx.onerror=()=>reject(tx.error);};request.onerror=()=>reject(request.error);})""")
            assert not still_empty['active'] and still_empty['originals']==0
            assert isinstance(still_empty['epoch'],int) and still_empty['epoch']>0
            # A rejected stale save must not discard the user's unsaved in-memory
            # work: downloading an independent project remains available.
            with other.expect_download() as retained_download:
                other.locator('[data-action="save"]').click()
            retained_path=output/'second-tab-retained.figwork'
            retained_download.value.save_as(retained_path)
            with ZipFile(retained_path) as retained_archive:
                retained_project=json.loads(retained_archive.read('project.json'))
                assert retained_project['title']=='Unsaved synthetic work in second tab'
                assert len(retained_project['assets'])==1
            other.locator('body:not(.busy)').wait_for(state='attached')
            other.locator('[data-action="new"]').first.click()
            other.locator('body:not(.busy)').wait_for(state='attached')
            expect(other.locator('#panel-count')).to_have_text('0')
            other.locator('#image-files').set_input_files(files[:1])
            other.locator('body:not(.busy)').wait_for(state='attached')
            expect(other.locator('#save-status')).to_contain_text('已自动保存',timeout=20000)
            other.locator('[data-action="clear"]').first.click()
            other.locator('body:not(.busy)').wait_for(state='attached')
            other.close();page.reload(wait_until='load');idle();count(0)
            assert not page.locator('#workspace-recovery-dialog').is_visible()
            checked('cross-tab clear rejects stale autosave without discarding manual backup or explicit new workflow')


            assert not report['network_attempts'], report['network_attempts']
            assert not report['browser_errors'], report['browser_errors']
            assert hashlib.sha256(html.read_bytes()).hexdigest() == report['html_sha256'], 'Tested HTML changed during acceptance'
            failed_storage.close(); context.close(); browser.close(); report['passed'] = True
    except Exception as error:
        report['failure'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        report['elapsed_seconds'] = round(time.perf_counter() - started, 2)
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
