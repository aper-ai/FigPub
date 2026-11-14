/** Actual UI event handlers + real ASGI service, with DOM emulation only.
 * Browser layout, WebView2 and native dialogs require separate acceptance.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {readFile, access} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {build} from 'esbuild';
import {Window} from 'happy-dom';

const root = fileURLToPath(new URL('../', import.meta.url));

test('new-user qPCR → real analysis → chart → PDF/project → reopen, through actual DOM actions', {timeout: 90000}, async t => {
  let python = process.env.FW_TEST_PYTHON;
  if (!python) {
    const local = join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
    try {await access(local); python = local;} catch {python = process.platform === 'win32' ? 'python' : 'python3';}
  }
  const child = spawn(python, ['tests/ui_api_bridge.py'], {cwd: root, stdio: ['pipe', 'pipe', 'pipe']});
  let nextId = 0, ready = false, stderr = '';
  const pending = new Map();
  child.stderr.on('data', data => {stderr += data.toString();});
  createInterface({input: child.stdout}).on('line', line => {
    const message = JSON.parse(line);
    if (message.ready) {ready = true; return;}
    const entry = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) entry?.reject(new Error(message.error));
    else entry?.resolve(message.result);
  });
  child.on('exit', code => {
    for (const entry of pending.values()) entry.reject(new Error(`ASGI test adapter exited ${code}: ${stderr}`));
    pending.clear();
  });
  const call = command => new Promise((resolve, reject) => {
    const id = ++nextId; pending.set(id, {resolve, reject});
    child.stdin.write(JSON.stringify({id, ...command}) + '\n');
  });
  const window = new Window({url: 'http://127.0.0.1:8765', settings: {
    disableCSSFileLoading: true, disableJavaScriptFileLoading: true,
    disableIframePageLoading: true, disableComputedStyleRendering: true,
  }});
  window.structuredClone = globalThis.structuredClone;
  window.TextEncoder = globalThis.TextEncoder;
  window.TextDecoder = globalThis.TextDecoder;
  t.after(async () => {
    await window.happyDOM.abort();
    child.stdin.end(JSON.stringify({op: 'stop', id: ++nextId}) + '\n');
    const timer = setTimeout(() => child.kill(), 5000); timer.unref();
  });
  async function until(predicate, description, timeout = 15000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      if (predicate()) return;
      await delay(20);
    }
    throw new Error(`${description}; errors: ${window.document.getElementById('toast-region')?.textContent || ''}; ${stderr}`);
  }
  await until(() => ready, 'ASGI service readiness');
  const requests = [], downloads = [], objectUrls = new Map(), uiErrors = [];
  window.addEventListener('error', event => uiErrors.push(event.message));
  window.fetch = async (url, options = {}) => {
    const parsed = new URL(String(url), window.location.href);
    assert.equal(parsed.origin, window.location.origin, 'no external requests');
    const command = {path: parsed.pathname + parsed.search, method: options.method || 'GET', headers: options.headers || {}};
    if (options.body instanceof window.FormData) {
      command.files = [];
      for (const [field, file] of options.body.entries()) {
        command.files.push({field, name: file.name, type: file.type || 'application/octet-stream',
          data: Buffer.from(await file.arrayBuffer()).toString('base64')});
      }
    } else if (options.body !== undefined) command.body = options.body;
    const result = await call(command);
    requests.push({path: command.path, method: command.method, status: result.status});
    return new Response(Buffer.from(result.data, 'base64'), {status: result.status, headers: result.headers});
  };
  window.URL.createObjectURL = blob => {
    const url = `blob:test-${objectUrls.size}`; objectUrls.set(url, blob); return url;
  };
  window.URL.revokeObjectURL = url => objectUrls.delete(url);
  window.HTMLAnchorElement.prototype.click = function () {
    if (this.download && objectUrls.has(this.href)) downloads.push({filename: this.download, blob: objectUrls.get(this.href)});
  };
  const index = (await readFile(join(root, 'web/index.html'), 'utf8')).replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '');
  window.document.write(index);
  const bundled = await build({stdin: {contents: `import './web/app.js'; import {state,flushSave} from './web/modules/state.js'; window.__testWorkbench={state,flushSave};`,
    resolveDir: root, sourcefile: 'ui-flow-entry.js'}, bundle: true, write: false, format: 'iife', platform: 'browser'});
  window.eval(bundled.outputFiles[0].text);
  const state = () => window.__testWorkbench.state;
  await until(() => state().project && window.document.querySelector('[data-action="start-task"]'), 'home task cards');
  assert.equal(window.document.querySelectorAll('[data-action="start-task"]').length, 4);
  function click(selector) {
    const element = window.document.querySelector(selector);
    assert.ok(element, `missing control ${selector}`);
    assert.equal(Boolean(element.disabled), false, `disabled control ${selector}`);
    element.click();
  }
  function change(selector, value) {
    const element = window.document.querySelector(selector);
    assert.ok(element, `missing field ${selector}`);
    element.value = value;
    element.dispatchEvent(new window.Event('change', {bubbles: true}));
  }
  function upload(selector, name, data, type) {
    const transfer = new window.DataTransfer();
    transfer.items.add(new window.File([Buffer.from(data, 'base64')], name, {type}));
    const input = window.document.querySelector(selector);
    input.files = transfer.files;
    input.dispatchEvent(new window.Event('change', {bubbles: true}));
  }
  const fixtures = await call({op: 'fixtures'});
  click('[data-action="start-task"][data-task="qpcr"]');
  upload('#import-files', 'SYNTHETIC-qPCR.csv', fixtures.qpcr, 'text/csv');
  await until(() => !state().busy && state().table?.canonical, 'canonical qPCR import into selected task');
  assert.equal(state().tab, 'analysis');
  assert.equal(state().analysisKind, 'qpcr');
  assert.equal(state().table.rows.length, 12);
  change('#analysis-reference_gene', 'ACTB');
  change('#analysis-control_group', 'Control');
  click('[data-action="run-analysis"]');
  await until(() => ['done', 'error'].includes(state().job?.status), 'real qPCR job completion', 30000);
  assert.equal(state().job.status, 'done');
  const groups = state().job.result.groups;
  assert.equal(groups.find(group => group.group === 'Treatment').mean, 4);
  assert.equal(groups.find(group => group.group === 'Treatment').n_biological, 3);
  await until(() => state().project.measurements.length === 1 && window.document.querySelector('[data-action="plot-results"]'), 'saved result actions');
  click('[data-action="plot-results"]');
  await until(() => !state().busy && state().tab === 'compose' && state().project.panels.length === 1, 'result chart in composition');
  assert.ok(state().project.panels[0].analysis_source);
  click('[data-action="export-dialog"]');
  await until(() => window.document.querySelector('[data-action="export-file"]'), 'export options dialog');
  click('[data-action="export-file"]');
  await until(() => downloads.some(item => item.filename.endsWith('.pdf')), 'PDF export download');
  const pdf = downloads.find(item => item.filename.endsWith('.pdf'));
  const pdfInfo = await call({op: 'inspect_pdf', data: Buffer.from(await pdf.blob.arrayBuffer()).toString('base64')});
  assert.equal(pdfInfo.pages, 1);
  assert.ok(Math.abs(pdfInfo.width_mm - state().project.figure.width_mm) < .01);
  await until(() => !state().busy, 'export action completion');
  if (window.document.querySelector('[data-action="close-modal"]')) click('[data-action="close-modal"]');
  click('[data-action="save-project"]');
  await until(() => downloads.some(item => item.filename.endsWith('.figwork')), 'portable project backup');
  const backup = downloads.find(item => item.filename.endsWith('.figwork'));
  const archive = Buffer.from(await backup.blob.arrayBuffer()).toString('base64');
  const saved = await call({op: 'inspect_archive', data: archive});
  assert.equal(saved.project.measurements.length, 1);
  assert.equal(saved.project.panels.length, 1);
  upload('#open-project', 'roundtrip.figwork', archive, 'application/zip');
  await delay(30);
  if (window.document.querySelector('[data-action="confirm-project-switch"]')) click('[data-action="confirm-project-switch"]');
  await until(() => !state().busy && requests.some(item => item.path === '/api/project/open'), 'project reopen');
  assert.equal(state().project.panels.length, 1);
  assert.equal(state().project.measurements[0].result.groups.find(group => group.group === 'Treatment').mean, 4);
  const closeState = await window.figureWorkbenchPrepareClose();
  assert.equal(closeState.saved, true);
  assert.equal(closeState.busy, false);
  assert.deepEqual(uiErrors, []);
  assert.ok(requests.some(item => item.path === '/api/jobs' && item.status === 200));
  assert.ok(requests.some(item => item.path === '/api/charts' && item.status === 200));
  assert.ok(requests.some(item => item.path === '/api/export' && item.status === 200));
});
