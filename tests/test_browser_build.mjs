import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync, spawnSync} from 'node:child_process';
import {mkdtemp, readFile, rm} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

test('the final offline HTML contains two complete parseable scripts and no external resources', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'figure-offline-build-'));
  try {
    const output = path.join(directory, 'Figure_Workbench_Browser.html');
    execFileSync(process.execPath, ['scripts/build_browser_studio.mjs', output], {cwd: root, stdio: 'pipe'});
    const html = await readFile(output, 'utf8');
    // Parse the delivered HTML boundaries, not esbuild's pre-insertion output.
    const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi)];
    assert.equal(scripts.length, 2);
    assert.match(scripts[0][2], /globalThis\.FW_STUDIO_FONTS=/);
    assert.match(scripts[1][1], /type="module"/);
    assert(!html.includes('src="browser-studio.js"'));
    assert(!html.includes('href="browser-studio.css"'));
    for (const [index, script] of scripts.entries()) {
      const checked = spawnSync(process.execPath, ['--input-type=module', '--check'], {
        input: script[2], encoding: 'utf8', maxBuffer: 1024 * 1024,
      });
      assert.equal(checked.status, 0, `Delivered script ${index} has invalid syntax: ${checked.stderr?.slice(-3000)}`);
    }
    assert(!/<script\b[^>]*\bsrc=|<link\b[^>]*\bhref=/i.test(html));
  } finally {
    await rm(directory, {recursive: true, force: true});
  }
});
