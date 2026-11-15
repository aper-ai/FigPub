"""Real pointer acceptance for aligned labels and visible snapping in both studios.

All files are generated geometry fixtures. No research image is used. Run:
  BROWSER_EXECUTABLE=/usr/bin/chromium python tests/browser_alignment_snap.py
"""
from __future__ import annotations

from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from browser_composition import start_server


def near(actual, expected, tolerance=1e-5):
    assert abs(actual - expected) < tolerance, f'{actual} differs from {expected}'


class Surface:
    def __init__(self, page, kind, report, output):
        self.page, self.kind, self.report, self.output = page, kind, report, output
        self.svg = '#figure-svg' if kind == 'desktop-web' else '#studio-svg'
        self.groups = '.figure-panel' if kind == 'desktop-web' else '[data-object]'

    def snapshot(self):
        return self.page.evaluate("""({svg,groups})=>Array.from(document.querySelector(svg).querySelectorAll(':scope > '+groups)).filter(g=>g.querySelector('image')).map(g=>{
          const m=g.transform.baseVal.getItem(0).matrix,rect=g.querySelector('.panel-hit')||g.querySelector('clipPath rect'),label=g.querySelector('.panel-label')||g.querySelector(':scope > text');
          return {id:g.dataset.panelId||g.dataset.object,x:m.e,y:m.f,width:Number(rect.getAttribute('width')),height:Number(rect.getAttribute('height')),
            label:label?.textContent||'',labelX:m.e+Number(label?.getAttribute('x')||0),labelY:m.f+Number(label?.getAttribute('y')||0)};
        })""", {'svg': self.svg, 'groups': self.groups})

    def metric(self):
        # Selection can synchronously replace the SVG. Flush its layout before
        # reading the screen matrix; Chromium may otherwise return identity.
        return self.page.locator(self.svg).evaluate('(svg)=>{const bounds=svg.getBoundingClientRect();if(!bounds.width||!bounds.height)throw new Error("Canvas has no visible layout");const m=svg.getScreenCTM();return {a:m.a,b:m.b,c:m.c,d:m.d,e:m.e,f:m.f,scale:Math.hypot(m.a,m.b)}}')

    def screen(self, x, y, matrix=None):
        matrix = self.metric() if matrix is None else matrix
        return matrix['a'] * x + matrix['c'] * y + matrix['e'], matrix['b'] * x + matrix['d'] * y + matrix['f']

    def clear_selection(self):
        self.page.mouse.click(*self.screen(2, 2))

    def select(self, index, add=False):
        panel = self.snapshot()[index]
        if add:
            self.page.keyboard.down('Shift')
        self.page.mouse.click(*self.screen(panel['x'] + panel['width'] * .3, panel['y'] + panel['height'] * .5))
        if add:
            self.page.keyboard.up('Shift')

    def guides(self):
        return self.page.locator('#snap-guides [data-snap-axis]').evaluate_all("""groups=>groups.map(g=>({axis:g.dataset.snapAxis,kind:g.dataset.snapKind,
          position:Number(g.querySelector('line').getAttribute(g.dataset.snapAxis==='x'?'x1':'y1')),
          visible:Array.from(g.querySelectorAll('line')).some(line=>getComputedStyle(line).stroke==='rgb(8, 127, 206)')}))""")

    def checked(self, name, **details):
        self.report['scenarios'].append({'surface': self.kind, 'name': name, 'passed': True, **details})
        print('PASS', self.kind, name, flush=True)

    def undo_and_compare(self, before):
        self.page.locator('[data-action="undo"]').first.click()
        self.page.wait_for_timeout(50)
        after = self.snapshot()
        assert [item['id'] for item in after] == [item['id'] for item in before]
        for actual, expected in zip(after, before):
            for key in ['x', 'y', 'width', 'height', 'labelX', 'labelY']:
                near(actual[key], expected[key])

    def snapping(self, enabled):
        if self.kind == 'desktop-web':
            button = self.page.locator('[data-action="toggle-snap"]')
            if (button.get_attribute('aria-pressed') == 'true') != enabled:
                button.click()
            assert button.get_attribute('aria-pressed') == str(enabled).lower()
            assert button.inner_text().strip() == ('对齐吸附' if enabled else '吸附已关闭')
        else:
            self.page.locator('#snap-enabled').set_checked(enabled)

    def drag(self, index, dx, dy, *, alt=False, select_only=True):
        # Capture real DOM hit testing and pointer delivery so a failed guide
        # assertion distinguishes a missed image from a gesture regression.
        self.page.evaluate("""()=>{
          window.__alignmentPointerEvents=[];
          if(window.__alignmentPointerRecorder)return;
          window.__alignmentPointerRecorder=event=>{
            const target=event.target,object=target?.closest?.('[data-panel-id],[data-object]');
            window.__alignmentPointerEvents.push({type:event.type,tag:target?.tagName||'',id:target?.id||'',object:object?.dataset.panelId||object?.dataset.object||'',
              x:event.clientX??null,y:event.clientY??null,buttons:event.buttons??null,pointerId:event.pointerId??null});
          };
          for(const type of ['pointerdown','pointermove','pointerup','pointercancel','lostpointercapture','blur'])window.addEventListener(type,window.__alignmentPointerRecorder,true);
        }""")
        if select_only:
            self.clear_selection()
        panel = self.snapshot()[index]
        matrix = self.metric()
        start = self.screen(panel['x'] + panel['width'] * .3, panel['y'] + panel['height'] * .5, matrix)
        self.current_trace = {'surface': self.kind, 'panel_id': panel['id'], 'start': start,
                              'dx_mm': dx, 'dy_mm': dy, 'alt': alt, 'matrix': matrix}
        self.report.setdefault('pointer_traces', []).append(self.current_trace)
        self.current_trace['hit_before_move'] = self.pointer_hit(start)
        if alt:
            self.page.keyboard.down('Alt')
        self.page.mouse.move(*start)
        self.current_trace['hit_before_down'] = self.pointer_hit(start)
        assert self.current_trace['hit_before_down']['object'] == panel['id'], self.current_trace
        self.page.mouse.down()
        self.current_trace['state_after_down'] = self.pointer_hit(start)
        self.current_trace['events_after_down'] = self.page.evaluate('()=>window.__alignmentPointerEvents')
        end = (start[0] + dx * matrix['a'] + dy * matrix['c'],
               start[1] + dx * matrix['b'] + dy * matrix['d'])
        self.page.mouse.move(*end, steps=8)
        self.page.wait_for_timeout(120)
        self.current_trace['state_after_move'] = self.pointer_hit(end)
        self.current_trace['events'] = self.page.evaluate('()=>window.__alignmentPointerEvents')
        self.current_trace['geometry_during_drag'] = self.snapshot()

    def pointer_hit(self, point):
        return self.page.evaluate("""([x,y])=>{
          const target=document.elementFromPoint(x,y),object=target?.closest('[data-panel-id],[data-object]');
          return {tag:target?.tagName||'',id:target?.id||'',object:object?.dataset.panelId||object?.dataset.object||'',
            className:target?.getAttribute('class')||'',busy:document.body.classList.contains('busy'),
            selection_info:document.querySelector('#selection-info')?.textContent||'',
            selected_outlines:document.querySelectorAll('.selection-outline,.figure-panel.is-selected').length};
        }""", point)

    def release(self, alt=False):
        self.page.mouse.up()
        if alt:
            self.page.keyboard.up('Alt')
        self.page.wait_for_timeout(60)
        self.current_trace['events'] = self.page.evaluate('()=>window.__alignmentPointerEvents')
        assert not self.guides(), 'Reference lines must clear at pointer release'

    def run(self):
        panels = self.snapshot()
        assert len(panels) == 4
        assert [panel['label'] for panel in panels] == ['A', 'B', 'C', 'D']
        near(panels[0]['labelX'], panels[2]['labelX'])
        near(panels[1]['labelX'], panels[3]['labelX'])
        near(panels[0]['labelY'], panels[1]['labelY'])
        near(panels[2]['labelY'], panels[3]['labelY'])
        assert panels[0]['x'] > panels[2]['x'] + 5, 'Fixture must reproduce different image-left positions'
        for panel, ratio in zip(panels, [.4, 3, 5, .75]):
            near(panel['width'] / panel['height'], ratio)
        self.page.screenshot(path=str(self.output / f'{self.kind}-aligned-grid.png'), full_page=True)
        self.checked('four mixed-aspect originals retain ratio with aligned columns and row baselines', geometry=panels)

        scales = []
        for level in range(2):
            if level:
                if self.kind == 'desktop-web':
                    self.page.locator('[data-action="zoom-out"]').click()
                    self.page.locator('[data-action="zoom-out"]').click()
                else:
                    # The browser studio uses a range slider, which cannot be
                    # filled like a text input. Exercise its real keyboard UI.
                    slider = self.page.locator('#zoom')
                    slider.press('Home')
                    for _ in range(11):
                        slider.press('ArrowRight')
                    assert slider.input_value() == '60'
                self.page.wait_for_timeout(100)
            scale = self.metric()['scale']
            scales.append(scale)
            before = self.snapshot()
            dx = before[0]['x'] - before[2]['x'] + 3 / scale
            self.snapping(True)
            self.drag(2, dx, 0)
            guides = self.guides()
            assert any(guide['axis'] == 'x' and guide['kind'] == 'frame' and guide['visible'] for guide in guides), guides
            near(self.snapshot()[2]['x'], before[0]['x'])
            self.page.screenshot(path=str(self.output / f'{self.kind}-zoom{level}-edge-guide.png'), full_page=True)
            self.release()
            near(self.snapshot()[2]['x'], before[0]['x'])
            self.undo_and_compare(before)
            self.checked('image edge snaps within three screen pixels; blue guide clears; undo restores', pixels_per_mm=scale)

            self.drag(2, dx, 0, alt=True)
            assert not self.guides()
            unsnapped = self.snapshot()[2]['x']
            near(unsnapped, before[2]['x'] + dx, 1.1 / scale)
            assert abs(unsnapped - before[0]['x']) > 1 / scale
            self.release(alt=True)
            self.undo_and_compare(before)
            self.checked('holding Alt bypasses snapping and reference lines', pixels_per_mm=scale)

            # Modifiers may change after pointerdown. Keep the same drag alive
            # while Alt temporarily bypasses snapping, then restores it.
            self.drag(2, dx, 0)
            near(self.snapshot()[2]['x'], before[0]['x'])
            assert self.guides()
            start, matrix = self.current_trace['start'], self.current_trace['matrix']
            end = (start[0] + dx * matrix['a'], start[1] + dx * matrix['b'])
            self.page.keyboard.down('Alt')
            self.page.mouse.move(end[0] + 1, end[1], steps=2)
            self.page.wait_for_function('()=>!document.querySelector("#snap-guides [data-snap-axis]")')
            unsnapped = self.snapshot()[2]['x']
            near(unsnapped, before[2]['x'] + dx + 1 / scale, 1.1 / scale)
            assert abs(unsnapped - before[0]['x']) > 1 / scale
            self.current_trace['mid_drag_alt'] = {'unsnapped_x': unsnapped, 'state_with_alt': self.pointer_hit((end[0] + 1, end[1]))}
            if not level:
                self.page.screenshot(path=str(self.output / f'{self.kind}-mid-drag-alt.png'), full_page=True)
            self.page.keyboard.up('Alt')
            self.page.mouse.move(*end, steps=2)
            self.page.locator('#snap-guides [data-snap-axis="x"][data-snap-kind="frame"]').wait_for(state='attached')
            near(self.snapshot()[2]['x'], before[0]['x'])
            assert any(guide['axis'] == 'x' and guide['visible'] for guide in self.guides())
            self.current_trace['mid_drag_alt']['resnapped_x'] = self.snapshot()[2]['x']
            self.release()
            self.undo_and_compare(before)
            self.checked('Alt pressed and released during one drag bypasses and restores snapping without cancelling', pixels_per_mm=scale)

            self.snapping(False)
            self.drag(2, dx, 0)
            assert not self.guides()
            unsnapped = self.snapshot()[2]['x']
            near(unsnapped, before[2]['x'] + dx, 1.1 / scale)
            assert abs(unsnapped - before[0]['x']) > 1 / scale
            self.release()
            self.undo_and_compare(before)
            self.snapping(True)
            self.checked('visible snap control disables and restores snapping', pixels_per_mm=scale)

            dy = before[0]['labelY'] - before[2]['labelY'] + 3 / scale
            self.drag(2, 0, dy)
            guides = self.guides()
            assert any(guide['axis'] == 'y' and guide['kind'] == 'label' and guide['visible'] for guide in guides), guides
            near(self.snapshot()[2]['labelY'], before[0]['labelY'])
            self.page.screenshot(path=str(self.output / f'{self.kind}-zoom{level}-label-guide.png'), full_page=True)
            self.release()
            self.undo_and_compare(before)
            self.checked('numbering baselines snap independently of image top edges', pixels_per_mm=scale)

            self.clear_selection()
            self.select(2)
            self.select(3, add=True)
            self.drag(2, 8, -8, select_only=False)
            during = self.snapshot()
            for key in ['x', 'y', 'labelX', 'labelY']:
                near(during[3][key] - during[2][key], before[3][key] - before[2][key])
            assert abs(during[2]['x'] - before[2]['x']) > 1
            self.release()
            after = self.snapshot()
            for key in ['x', 'y', 'labelX', 'labelY']:
                near(after[3][key] - after[2][key], before[3][key] - before[2][key])
            self.undo_and_compare(before)
            self.checked('multiple selected panels preserve relative geometry during a real drag', pixels_per_mm=scale)
        assert abs(scales[0] - scales[1]) > .1, scales


def main():
    output = Path(os.environ.get('FW_ALIGNMENT_RESULTS', ROOT / 'test-results/alignment-snap'))
    output.mkdir(parents=True, exist_ok=True)
    version = json.loads((ROOT / 'package.json').read_text('utf-8'))['version']
    html = Path(os.environ.get('BROWSER_STUDIO_HTML', ROOT / f'dist/Figure_Workbench_{version}_Browser.html')).resolve()
    subprocess.run(['node', 'scripts/build_browser_studio.mjs', str(html)], cwd=ROOT, check=True)
    report = {'real_browser': True, 'synthetic_data': True, 'scenarios': [], 'browser_errors': [],
              'html_sha256': hashlib.sha256(html.read_bytes()).hexdigest(), 'passed': False}
    current_page = None
    started = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix='figure-alignment-') as directory:
            files = []
            for index, (width, height) in enumerate([(320, 800), (960, 320), (1200, 240), (480, 640)]):
                image = Image.new('RGB', (width, height), ['#27644e', '#3865ad', '#ae6d29', '#8b3974'][index])
                drawing = ImageDraw.Draw(image)
                drawing.rectangle((width * .1, height * .2, width * .7, height * .7), fill='#dbe9dc')
                drawing.text((10, 10), f'SYNTHETIC ORIGINAL {index + 1}', fill='white')
                data = BytesIO()
                image.save(data, 'PNG')
                files.append({'name': f'SYNTHETIC_{index}.png', 'mimeType': 'image/png', 'buffer': data.getvalue()})
            server, thread, base = start_server(directory)
            try:
                with sync_playwright() as playwright:
                    options = {'headless': True}
                    if os.environ.get('BROWSER_EXECUTABLE'):
                        options['executable_path'] = os.environ['BROWSER_EXECUTABLE']
                    browser = playwright.chromium.launch(**options)
                    for kind in ['desktop-web', 'file-browser']:
                        context = browser.new_context(viewport={'width': 1680, 'height': 1080}, accept_downloads=True)
                        context.route('**/*', lambda route: route.continue_() if route.request.url.startswith((base + '/', 'file:', 'data:', 'blob:')) else route.abort())
                        page = current_page = context.new_page()
                        page.set_default_timeout(20000)
                        page.on('pageerror', lambda error: report['browser_errors'].append(str(error)))
                        page.goto(base if kind == 'desktop-web' else html.as_uri(), wait_until='networkidle')
                        if kind == 'desktop-web':
                            page.locator('[data-action="tab"][data-tab="compose"]').click()
                            page.locator('#import-files').set_input_files(files)
                            page.wait_for_function('()=>document.querySelectorAll(".figure-panel").length===4')
                            page.locator('[data-action="layout-preset"][data-columns="2"][data-rows="2"]').click()
                        else:
                            page.locator('#image-files').set_input_files(files)
                            page.wait_for_function('()=>document.querySelectorAll("#studio-svg > [data-object]").length===4')
                            page.locator('[data-grid="2,2"]').click()
                        page.wait_for_timeout(150)
                        surface = Surface(page, kind, report, output)
                        try:
                            surface.run()
                        except Exception:
                            report['failure_surface'] = kind
                            report['failure_geometry'] = surface.snapshot()
                            report['failure_guides'] = surface.guides()
                            page.screenshot(path=str(output / f'{kind}-failure.png'), full_page=True, timeout=5000)
                            raise
                        context.close()
                        current_page = None
                    browser.close()
            finally:
                server.should_exit = True
                thread.join(timeout=10)
        assert not report['browser_errors'], report['browser_errors']
        report['passed'] = True
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
        if current_page and not current_page.is_closed():
            try:
                current_page.screenshot(path=str(output / 'failure.png'), full_page=True, timeout=5000)
            except Exception:
                pass
        raise
    finally:
        report['seconds'] = round(time.perf_counter() - started, 3)
        (output / 'alignment-snap.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
