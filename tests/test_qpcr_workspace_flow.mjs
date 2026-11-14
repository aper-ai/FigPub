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

test('qPCR files and worksheets isolate well exclusions, grouping and analysis; failed sheets recover through real DOM/API', {timeout: 90000}, async t => {
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

  const fixtures = await call({op: 'qpcr_workbooks'});
  const plain = value => JSON.parse(JSON.stringify(value));
  function included(row, value) {
    const element = window.document.querySelector(`#qpcr-include-${row}`);
    assert.ok(element); element.checked=value;
    element.dispatchEvent(new window.Event('change', {bubbles:true}));
  }
  async function tableReady(id, sheet) {
    await until(() => !state().busy && !state().tableLoading && state().table?.canonical &&
      state().table.asset_id===id && state().table.sheet===sheet, `ready ${id} ${sheet}`);
  }
  async function run(expectedFold) {
    const previous=state().job?.id;
    click('[data-action="run-analysis"]');
    await until(() => state().job?.id!==previous && ['done','error'].includes(state().job?.status), 'actual qPCR job', 30000);
    assert.equal(state().job.status,'done',state().job.error);
    await until(() => state().project.measurements.some(item=>item.id===state().job.id),'persisted qPCR record');
    assert.equal(state().job.result.groups.find(group=>group.group==='Treatment').mean,expectedFold);
    assert.equal(state().job.result.groups.find(group=>group.group==='Control').n_biological,3);
    assert.equal(state().job.result.groups.find(group=>group.group==='Treatment').n_biological,3);
  }
  click('[data-action="start-task"][data-task="qpcr"]');
  upload('#import-files','SYNTHETIC-A.xlsx',fixtures.A,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
  await until(()=>!state().busy && state().project.assets.length===1,'import A');
  const idA=state().project.assets[0].id;
  await tableReady(idA,'A-results');
  // Exercise the same draft/change/blur lifecycle as native typing and Tab.
  // The input transaction intentionally finishes before the whole form redraws.
  for(const [selector,value] of [['#analysis-reference_gene','ACTB'],['#analysis-control_group','Control']]) {
    const element=window.document.querySelector(selector);
    element.focus();element.value=value;
    element.dispatchEvent(new window.Event('input',{bubbles:true}));
    element.dispatchEvent(new window.Event('change',{bubbles:true}));
    element.blur();
  }
  assert.equal(state().analysisParams.reference_gene,'ACTB');
  assert.equal(state().analysisParams.control_group,'Control');
  assert.equal(state().formCommitPending,true);
  await until(()=>!state().formCommitPending&&!window.document.querySelector('[data-action="run-analysis"]').disabled,'qPCR readiness after native form commit');
  await run(2);
  change('[data-qpcr-group-key="experiment-1::S1"]','Manual-A');
  included(3,false);
  assert.deepEqual(plain(state().analysisParams.excluded_source_rows),[3]);

  // Same row numbers and sample identities in B must never inherit A's edits.
  upload('#import-files','SYNTHETIC-B.xlsx',fixtures.B,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
  await until(()=>!state().busy && state().project.assets.length===2,'import B');
  const idB=state().project.assets[1].id;
  await tableReady(idB,'B-results');
  assert.deepEqual(plain(state().analysisParams.excluded_source_rows),[]);
  assert.deepEqual(plain(state().analysisParams.group_mapping),{});
  assert.equal(state().analysisParams.reference_gene,'');
  assert.ok(window.document.querySelector('#qpcr-include-3').checked);
  assert.ok(window.document.querySelector('[data-action="run-analysis"]').disabled);
  change('#analysis-reference_gene','ACTB');change('#analysis-control_group','Control');
  await run(4);
  const recordedB=state().project.measurements.find(item=>item.id===state().job.id);
  assert.equal(recordedB.params.asset_id,idB);
  assert.equal(recordedB.params.sheet,'B-results');
  assert.deepEqual(plain(recordedB.params.excluded_source_rows),[]);
  assert.deepEqual(plain(recordedB.params.group_mapping),{});
  assert.ok(!requests.some(item=>item.path.includes(`/api/assets/${idB}/qpcr`) && item.path.includes('sheet=A')));

  click(`[data-action="select-asset"][data-id="${idA}"]`);
  await tableReady(idA,'A-results');
  assert.deepEqual(plain(state().analysisParams.excluded_source_rows),[3]);
  assert.equal(state().analysisParams.group_mapping['experiment-1::S1'],'Manual-A');
  assert.equal(state().analysisParams.reference_gene,'ACTB');
  assert.equal(window.document.querySelector('#qpcr-include-3').checked,false);
  change('#analysis-sheet','A-other');
  await tableReady(idA,'A-other');
  assert.deepEqual(plain(state().analysisParams.excluded_source_rows),[]);
  assert.deepEqual(plain(state().analysisParams.group_mapping),{});
  change('#analysis-reference_gene','ACTB');change('#analysis-control_group','Control');
  included(5,false);
  change('#analysis-sheet','A-results');
  await tableReady(idA,'A-results');
  assert.deepEqual(plain(state().analysisParams.excluded_source_rows),[3]);
  click('[data-action="undo"]');
  await tableReady(idA,'A-other');
  assert.deepEqual(plain(state().analysisParams.excluded_source_rows),[5]);
  click('[data-action="redo"]');
  await tableReady(idA,'A-results');
  assert.deepEqual(plain(state().analysisParams.excluded_source_rows),[3]);

  change('#analysis-sheet','Notes');
  await until(()=>!state().tableLoading && !!state().tableError,'actual invalid worksheet response');
  assert.equal(state().table,null);
  const selector=window.document.querySelector('#analysis-sheet');
  assert.ok(selector, 'worksheet selector remains available after a failed read');
  assert.ok([...selector.options].some(option=>option.value==='A-results'));
  assert.ok([...selector.options].some(option=>option.value===''));
  assert.ok(window.document.querySelector('[data-action="run-analysis"]').disabled);
  change('#analysis-sheet','');
  await tableReady(idA,'A-results');
  assert.equal(state().tableError,null);
  assert.deepEqual(plain(state().analysisParams.excluded_source_rows),[3]);
  assert.equal(state().analysisParams.group_mapping['experiment-1::S1'],'Manual-A');

  click('[data-action="save-project"]');
  await until(()=>!state().busy && downloads.some(item=>item.filename.endsWith('.figwork')),'scoped portable backup');
  const archive=Buffer.from(await downloads.find(item=>item.filename.endsWith('.figwork')).blob.arrayBuffer()).toString('base64');
  const backedUp=await call({op:'inspect_archive',data:archive});
  assert.ok(backedUp.project.settings.qpcr_workspaces[idA]);
  assert.ok(backedUp.project.settings.qpcr_workspaces[idB]);
  upload('#open-project','qPCR-scopes.figwork',archive,'application/zip');
  await until(()=>window.document.querySelector('[data-action="confirm-project-switch"]'),'project confirmation');
  click('[data-action="confirm-project-switch"]');
  await until(()=>!state().busy && !state().modal,'restore scopes');
  click('[data-action="analyze-table"]');
  await tableReady(idA,'A-results');
  assert.deepEqual(plain(state().analysisParams.excluded_source_rows),[3]);
  assert.equal(state().analysisParams.group_mapping['experiment-1::S1'],'Manual-A');
  click(`[data-action="select-asset"][data-id="${idB}"]`);
  await tableReady(idB,'B-results');
  assert.deepEqual(plain(state().analysisParams.excluded_source_rows),[]);
  assert.deepEqual(plain(state().analysisParams.group_mapping),{});
  assert.equal(state().analysisParams.reference_gene,'ACTB');
  assert.equal(state().project.measurements.find(item=>item.id===recordedB.id).result.groups.find(group=>group.group==='Treatment').mean,4);
  assert.deepEqual(uiErrors,[]);
});
