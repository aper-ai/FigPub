/** Production event handlers, request boundary and task controller in a DOM.
 * A real HTTP/browser test separately checks native button and request timing.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {build} from 'esbuild';
import {Window} from 'happy-dom';
import {defaultEdit} from '../web/modules/domain.js';

const root = fileURLToPath(new URL('../', import.meta.url));
const bundled = await build({stdin: {contents: `
  import {state,replaceProject,subscribe,emit,notify,notifyError} from './web/modules/state.js';
  import {runAnalysis,cancelJob,retryJobStatus} from './web/modules/analysis.js';
  import {renderApp} from './web/modules/view.js';
  import {bindInteractions} from './web/modules/interaction.js';
  subscribe(renderApp);bindInteractions();
  window.workbench={state,replaceProject,emit,notify,notifyError,runAnalysis,cancelJob,retryJobStatus};
`, resolveDir: root, sourcefile: 'analysis-submission-test-entry.js'}, bundle: true, write: false, format: 'iife', platform: 'browser'});
const index = (await readFile(join(root, 'web/index.html'), 'utf8')).replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '');
const response = data => new Response(JSON.stringify(data), {status: 200, headers: {'content-type': 'application/json'}});
function deferred() {let resolve, reject; const promise = new Promise((yes, no) => {resolve = yes; reject = no;}); return {promise, resolve, reject};}
async function until(predicate, label) {
  for (let attempt = 0; attempt < 150; attempt++) {if (predicate()) return; await delay(5);}
  assert.ok(predicate(), label);
}
function fixture(t, handler) {
  const window = new Window({url: 'http://127.0.0.1:8765', settings: {disableCSSFileLoading: true,
    disableJavaScriptFileLoading: true, disableIframePageLoading: true, disableComputedStyleRendering: true}});
  window.structuredClone = globalThis.structuredClone;
  const timeout = window.setTimeout.bind(window);
  window.setTimeout = (callback, milliseconds, ...args) => timeout(callback, milliseconds === 500 ? 1 : milliseconds, ...args);
  const calls = [];
  window.fetch = async (path, options = {}) => {
    const call = {path: String(path), method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null};
    calls.push(call); return handler(call, window.workbench, calls);
  };
  t.after(() => window.happyDOM.abort());
  window.document.write(index); window.eval(bundled.outputFiles[0].text);
  const app = window.workbench;
  app.replaceProject({schema_version: 1, id: 'submission-ui', title: 'Synthetic task lifecycle', revision: 0,
    assets: [{id: 'raw-a', name: 'Synthetic.tif', kind: 'image', width_px: 100, height_px: 100, channels: 1}],
    panels: [], annotations: [], measurements: [], settings: {qpcr_workspaces:{}}, figure: {width_mm: 180, height_mm: 120}});
  app.state.channelDescriptors.set('raw-a:0:0', {channels: [{id:'c0',name:'Channel 1',source:{asset_id:'raw-a',series:0,frame:0,channel:0}}],default_view:null});
  app.state.tab = 'analysis'; app.state.analysisKind = 'microscopy'; app.state.analysisParams.method = 'intensity'; app.emit();
  return {window, app, doc: window.document, calls, posts: () => calls.filter(call => call.method === 'POST')};
}
function completedProject(app, id, params) {
  const project = structuredClone(app.state.project);
  const sourceRevision = app.state.job.requestContext.sourceRevision;
  project.revision = sourceRevision + 1;
  project.measurements.push({id, kind: 'microscopy', params, source_project_revision: sourceRevision,
    source_asset_ids: ['raw-a'], result: {kind: 'microscopy', rows: [{mean: 2}], summary: {mean_intensity: 2}}});
  return project;
}
const guardToasts = doc => [...doc.querySelectorAll('#toast-region [data-notice-code]')]
  .filter(toast => ['analysis-switch-submission','analysis-switch-job','workspace-switch-gel','workspace-switch-wb'].includes(toast.dataset.noticeCode));
async function blockedProjectSwitch(f, message) {
  if(f.app.state.modal?.type!=='project') {
    f.doc.querySelector('[data-action="project-menu"]').click();
    await until(()=>f.app.state.modal?.type==='project','project menu opens');
  }
  f.doc.querySelector('.modal [data-action="new-dialog"]').click();
  await until(()=>message.test(f.doc.querySelector('.operation-error')?.textContent || ''),'the project-switch guard is visible');
  assert.equal(f.app.state.modal.type,'project');
  assert.equal(f.calls.filter(call=>call.path==='/api/project/new').length,0);
}

test('actual analysis action disables submission before autosave and POST finish, then exposes the same cancellable job', async t => {
  const saved = deferred(), posted = deferred(), queried = deferred(); let params;
  const f = fixture(t, call => {
    if (call.method === 'PUT') return saved.promise;
    if (call.method === 'POST') {params = call.body.params; return posted.promise;}
    if (call.method === 'DELETE') return response({id: 'ui-job', status: 'cancelled'});
    return queried.promise;
  });
  f.app.state.dirty = true; f.app.state.saveState = 'pending';
  const running = f.app.runAnalysis();
  let button = f.doc.querySelector('[data-action="run-analysis"]');
  assert.equal(button.disabled, true); assert.equal(button.getAttribute('aria-busy'), 'true');
  assert.match(f.doc.querySelector('.job-progress').textContent, /正在保存分析设置/);
  await f.app.runAnalysis(); assert.equal(f.calls.filter(call => call.method === 'PUT').length, 1); assert.equal(f.posts().length, 0);
  saved.resolve(response({...f.app.state.project, revision: 1})); await until(() => f.posts().length === 1, 'POST starts after autosave');
  await f.app.runAnalysis(); assert.equal(f.posts().length, 1);
  button = f.doc.querySelector('[data-action="run-analysis"]'); assert.equal(button.disabled, true);
  assert.match(f.doc.querySelector('.job-progress').textContent, /正在提交分析任务/);
  f.app.state.tab = 'home'; f.app.emit();
  assert.match(f.doc.querySelector('.background-activity').textContent, /任务正在提交/);
  f.doc.querySelector('[data-action="show-running-analysis"]').click();
  await until(() => f.app.state.tab === 'analysis', 'pending task navigation');
  posted.resolve(response({id: 'ui-job', status: 'queued'})); await until(() => f.app.state.job?.id === 'ui-job', 'known submitted job');
  assert.equal(f.app.state.analysisSubmission, null);
  assert.equal(f.doc.querySelector('[data-action="cancel-job"]').disabled, false);
  assert.equal(params.method, 'intensity');
  await f.app.cancelJob(); queried.resolve(response({id: 'ui-job', status: 'running'})); await running;
  assert.equal(f.app.state.job.status, 'cancelled'); assert.equal(f.posts().length, 1);
  assert.equal(f.doc.querySelector('[data-action="run-analysis"]').disabled, false);
});

test('production DOM retains an unqueryable job across page changes and the retry action restores its scientific result', async t => {
  let reachable = false, params;
  const f = fixture(t, (call, app) => {
    if (call.method === 'POST') {params = call.body.params; return response({id: 'recover-ui', status: 'running'});}
    if (call.path === '/api/project') return response(completedProject(app, 'recover-ui', params));
    if (!reachable) throw new Error('injected transport failure');
    return response({id: 'recover-ui', status: 'done', result: {kind: 'microscopy', rows: [{mean: 2}], summary: {mean_intensity: 2}}});
  });
  await f.app.runAnalysis();
  assert.equal(f.calls.filter(call => call.method === 'GET').length, 3, JSON.stringify({calls:f.calls,error:f.app.state.lastError,job:f.app.state.job}));
  assert.equal(f.app.state.job.id, 'recover-ui'); assert.equal(f.app.state.job.status, 'running');
  assert.ok(f.doc.querySelector('[data-action="retry-job-status"]'));
  assert.ok(f.doc.querySelector('[data-action="cancel-job"]'));
  await f.app.runAnalysis(); assert.equal(f.posts().length, 1);
  f.app.state.tab = 'home'; f.app.emit();
  assert.match(f.doc.querySelector('.background-activity').textContent, /暂时无法读取/);
  f.doc.querySelector('[data-action="show-running-analysis"]').click();
  await until(() => f.app.state.tab === 'analysis', 'known task remains reachable');
  reachable = true;
  f.doc.querySelector('[data-action="retry-job-status"]').click();
  await until(() => f.app.state.job.status === 'done', 'manual retry loads the existing result');
  assert.equal(f.posts().length, 1); assert.equal(f.app.state.project.measurements.length, 1);
  assert.equal(f.app.state.project.measurements[0].id, 'recover-ui');
  assert.equal(f.doc.querySelector('[data-action="retry-job-status"]'), null);
  assert.ok(f.doc.querySelector('#analysis-results-link'));
  assert.equal(f.doc.querySelector('[data-action="run-analysis"]').disabled, false);
});

test('a failed submission is visible, unlocks its control, and a successful retry clears the previous operation error', async t => {
  let attempts = 0, params;
  const f = fixture(t, (call, app) => {
    if (call.method === 'POST') {params = call.body.params; if (++attempts === 1) throw new Error('submission failure'); return response({id: 'retry-ui', status: 'queued'});}
    if (call.path === '/api/project') return response(completedProject(app, 'retry-ui', params));
    return response({id: 'retry-ui', status: 'done', result: {kind: 'microscopy', rows: [{mean: 2}], summary: {mean_intensity: 2}}});
  });
  await f.app.runAnalysis();
  assert.ok(f.doc.querySelector('.operation-error')); assert.equal(f.app.state.analysisSubmission, null);
  assert.ok(f.doc.querySelector('[data-notice-code="analysis-submit-error"]'));
  assert.equal(f.doc.querySelector('[data-action="run-analysis"]').disabled, false);
  await f.app.runAnalysis();
  assert.equal(f.app.state.job?.status, 'done', JSON.stringify({calls:f.calls,error:f.app.state.lastError})); assert.equal(f.posts().length, 2);
  assert.equal(f.doc.querySelector('.operation-error'), null);
  assert.equal(f.doc.querySelector('[data-notice-code="analysis-submit-error"]'), null);
});

test('all three production analysis entry points share pending and active-task disabling and terminal recovery', t => {
  const f=fixture(t,()=>{throw new Error('Button state inspection must not submit work');});
  f.app.state.analysisParams.method='nuclei';
  f.app.state.sourceRoi={type:'rectangle',x:10,y:10,width:20,height:20};
  f.app.state.project.measurements=[{id:'legacy-maskless',kind:'microscopy',source_asset_ids:['raw-a'],
    params:{method:'nuclei',asset_id:'raw-a'},result:{kind:'microscopy',rows:[],objects:[]}}];
  f.app.emit();
  const entries=()=>[...f.doc.querySelectorAll('[data-action="run-analysis"]')];
  assert.equal(entries().length,3,'Main action, ROI action and legacy segmentation action must all be exercised');
  assert.ok(entries().every(button=>!button.disabled));
  for(const phase of ['saving','submitting']) {
    f.app.state.analysisSubmission={phase,kind:'microscopy',sourceAssetId:'raw-a'};f.app.emit();
    assert.equal(entries().length,3);
    for(const button of entries()) {
      assert.equal(button.disabled,true);assert.equal(button.getAttribute('aria-busy'),'true');
      assert.match(button.textContent,/正在提交/);
    }
  }
  f.app.state.analysisSubmission=null;
  for(const [status,pollingError] of [['queued',null],['running',null],['running','Status unavailable']]) {
    f.app.state.job={id:'known-job',status,pollingError,startedAt:Date.now()};f.app.emit();
    assert.equal(entries().length,2,'The main action becomes the cancellation action');
    assert.ok(entries().every(button=>button.disabled));
    assert.equal(f.doc.querySelector('[data-action="cancel-job"]').disabled,false);
  }
  for(const status of ['done','error','cancelled']) {
    f.app.state.job={id:'known-job',status,startedAt:Date.now()};f.app.emit();
    assert.equal(entries().length,3);assert.ok(entries().every(button=>!button.disabled));
  }
  assert.equal(f.posts().length,0);
});

test('project-switch notices track their own pending submission and job, and disappear after real completion', async t => {
  const posted=deferred(),queried=deferred();let params;
  const f=fixture(t,(call,app)=>{
    if(call.method==='POST'){params=call.body.params;return posted.promise;}
    if(call.path==='/api/project')return response(completedProject(app,'guard-complete',params));
    return queried.promise;
  });
  const running=f.app.runAnalysis();await until(()=>f.posts().length===1,'submission reaches POST');
  await blockedProjectSwitch(f,/分析任务正在提交/);
  assert.equal(guardToasts(f.doc).length,1);
  posted.resolve(response({id:'guard-complete',status:'queued'}));
  await until(()=>f.app.state.job?.id==='guard-complete','submission receives its job');
  assert.equal(f.doc.querySelector('.operation-error'),null,'the submission guard ends with that submission');
  assert.equal(guardToasts(f.doc).length,0);
  await blockedProjectSwitch(f,/分析仍在运行/);
  assert.equal(guardToasts(f.doc).length,1);
  queried.resolve(response({id:'guard-complete',status:'done',result:{kind:'microscopy',rows:[{mean:2}],summary:{mean_intensity:2}}}));
  await running;
  assert.equal(f.app.state.job.status,'done');assert.equal(f.doc.querySelector('.operation-error'),null);
  assert.equal(guardToasts(f.doc).length,0,'neither old guard remains as a toast');
  assert.equal(f.doc.querySelector('[data-action="run-analysis"]').disabled,false);
  f.doc.querySelector('.modal [data-action="new-dialog"]').click();
  await until(()=>f.app.state.modal?.type==='new','project switching becomes available again');
});

test('unknown-status guard uses accurate wording and retry removes both adjacent obsolete notices without permitting a new job', async t => {
  let reachable=false,params;const resumed=deferred();
  const f=fixture(t,(call,app)=>{
    if(call.method==='POST'){params=call.body.params;return response({id:'guard-retry',status:'running'});}
    if(call.path==='/api/project')return response(completedProject(app,'guard-retry',params));
    if(!reachable)throw new Error('injected status transport failure');
    return resumed.promise;
  });
  await f.app.runAnalysis();
  await blockedProjectSwitch(f,/分析任务状态尚未确认/);
  assert.ok(f.doc.querySelector('[data-notice-code="analysis-status-unavailable"]'));
  assert.equal(guardToasts(f.doc).length,1);
  reachable=true;const recovering=f.app.retryJobStatus();
  assert.equal(f.doc.querySelector('.operation-error'),null);
  assert.equal(f.doc.querySelector('[data-notice-code="analysis-status-unavailable"]'),null);
  assert.equal(guardToasts(f.doc).length,0,'a snapshot removes adjacent resolved status and guard toasts');
  assert.equal(f.app.state.job.status,'running');await f.app.runAnalysis();assert.equal(f.posts().length,1);
  await blockedProjectSwitch(f,/分析仍在运行/);
  resumed.resolve(response({id:'guard-retry',status:'done',result:{kind:'microscopy',rows:[{mean:2}],summary:{mean_intensity:2}}}));
  await recovering;
  assert.equal(f.doc.querySelector('.operation-error'),null);assert.equal(guardToasts(f.doc).length,0);
});

test('confirmed cancellation clears only its switch guard and retains an unrelated save error banner and toast', async t => {
  const queried=deferred();
  const f=fixture(t,call=>{
    if(call.method==='POST')return response({id:'guard-cancel',status:'queued'});
    if(call.method==='DELETE')return response({id:'guard-cancel',status:'cancelled'});
    return queried.promise;
  });
  const running=f.app.runAnalysis();await until(()=>f.app.state.job?.id==='guard-cancel','job is tracked');
  await blockedProjectSwitch(f,/分析仍在运行/);
  const message='保存失败：另存项目的目标目录不可写';
  f.app.notify(message,'error');
  await f.app.cancelJob();queried.resolve(response({id:'guard-cancel',status:'running'}));await running;
  assert.equal(f.app.state.job.status,'cancelled');assert.equal(guardToasts(f.doc).length,0);
  assert.equal(f.app.state.lastError,message);assert.match(f.doc.querySelector('.operation-error').textContent,/目标目录不可写/);
  assert.ok([...f.doc.querySelectorAll('#toast-region .toast.error')].some(toast=>toast.textContent===message));
  assert.equal(f.doc.querySelector('[data-action="run-analysis"]').disabled,false);
});

test('submitting and completing a new analysis do not clear an unrelated analysis error', async t => {
  let params;const posted=deferred();
  const f=fixture(t,(call,app)=>{
    if(call.method==='POST'){params=call.body.params;return posted.promise;}
    if(call.path==='/api/project')return response(completedProject(app,'unrelated-error',params));
    return response({id:'unrelated-error',status:'done',result:{kind:'microscopy',rows:[{mean:2}],summary:{mean_intensity:2}}});
  });
  const message='分析未完成：另一份数据表缺少内参基因';f.app.notify(message,'error');
  const running=f.app.runAnalysis();assert.equal(f.app.state.lastError,message);
  await until(()=>f.posts().length===1,'new analysis reaches POST');
  posted.resolve(response({id:'unrelated-error',status:'queued'}));await running;
  assert.equal(f.app.state.job.status,'done');assert.equal(f.app.state.lastError,message);
  assert.match(f.doc.querySelector('.operation-error').textContent,/另一份数据表缺少内参基因/);
  assert.ok([...f.doc.querySelectorAll('#toast-region .toast.error')].some(toast=>toast.textContent===message));
});

test('a real computation error replaces the obsolete guard and only its deliberate same-input retry clears it', async t => {
  const queried=deferred();let attempts=0,params;
  const f=fixture(t,(call,app)=>{
    if(call.method==='POST'){params=call.body.params;return response({id:`guard-error-${++attempts}`,status:'queued'});}
    if(call.path==='/api/project')return response(completedProject(app,`guard-error-${attempts}`,params));
    if(attempts===1)return queried.promise;
    return response({id:`guard-error-${attempts}`,status:'done',result:{kind:'microscopy',rows:[{mean:2}],summary:{mean_intensity:2}}});
  });
  const running=f.app.runAnalysis();await until(()=>f.app.state.job?.id==='guard-error-1','job is tracked');
  await blockedProjectSwitch(f,/分析仍在运行/);
  queried.resolve(response({id:'guard-error-1',status:'error',error:'specimen quality invalid'}));await running;
  assert.equal(guardToasts(f.doc).length,0);assert.match(f.doc.querySelector('.operation-error').textContent,/specimen quality invalid/);
  assert.equal(f.app.state.lastErrorScope.code,'analysis-task-error');
  f.app.state.analysisParams.method='nuclei';
  await f.app.runAnalysis();assert.equal(f.app.state.job.status,'done');
  assert.match(f.doc.querySelector('.operation-error').textContent,/specimen quality invalid/,'running different input does not dismiss the previous analysis error');
  f.app.state.analysisParams.method='intensity';
  await f.app.runAnalysis();assert.equal(f.app.state.job.status,'done');
  assert.equal(f.doc.querySelector('.operation-error'),null);
  assert.equal(f.doc.querySelector('[data-notice-code="analysis-task-error"]'),null);
});

test('keyboard and file-open project guards retain scope, and a late guard rejection cannot revive a resolved notice', async t => {
  const posted=deferred(),queried=deferred();
  const f=fixture(t,call=>{
    if(call.method==='POST')return posted.promise;
    if(call.method==='DELETE')return response({id:'guard-input',status:'cancelled'});
    return queried.promise;
  });
  f.doc.querySelector('[data-action="project-menu"]').click();await until(()=>f.app.state.modal?.type==='project','project menu opens');
  f.doc.querySelector('.modal [data-action="new-dialog"]').click();await until(()=>f.app.state.modal?.type==='new','new-project dialog opens');
  const running=f.app.runAnalysis();await until(()=>f.posts().length===1,'submission is pending');
  const title=f.doc.getElementById('new-project-title');title.focus();
  title.dispatchEvent(new f.window.KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true,cancelable:true}));
  await until(()=>f.app.state.lastErrorScope?.code==='analysis-switch-submission','keyboard async rejection retains its scope');
  const expiredScope=f.app.state.lastErrorScope;
  const data=new f.window.DataTransfer();data.items.add(new f.window.File(['test'],'synthetic.figwork'));
  f.doc.getElementById('open-project').files=data.files;
  f.doc.getElementById('open-project').dispatchEvent(new f.window.Event('change',{bubbles:true}));
  await until(()=>f.app.state.lastErrorScope!==expiredScope,'file-open rejection retains a new scoped notice');
  assert.equal(f.app.state.lastErrorScope.code,'analysis-switch-submission');assert.equal(guardToasts(f.doc).length,1);
  posted.resolve(response({id:'guard-input',status:'queued'}));await until(()=>f.app.state.job?.id==='guard-input','job is accepted');
  await f.app.cancelJob();queried.resolve(response({id:'guard-input',status:'running'}));await running;
  assert.equal(f.doc.querySelector('.operation-error'),null);assert.equal(guardToasts(f.doc).length,0);
  f.app.notifyError(Object.assign(new Error('late rejected switch'),{noticeScope:expiredScope}));
  assert.equal(f.doc.querySelector('.operation-error'),null);assert.equal(guardToasts(f.doc).length,0);
});

function gelPixelsResponse() {
  return new Response(new Float32Array(100*100).fill(.9).buffer,{status:200,headers:{
    'X-Image-Width':'100','X-Image-Height':'100','X-Source-Width':'100','X-Source-Height':'100','X-Bit-Depth':'16'}});
}
const gelLocalization = () => ({lane_centers:[25,75],polarity:'dark',algorithm:'controlled-worker-result',
  bands:[25,75].flatMap(y=>[0,1].map(lane_index=>({lane_index,x:25+lane_index*50,y,height_px:5,score:12,bbox:{x:15+lane_index*50,y:y-2,width:20,height:5}}))),marker_candidates:[]});
async function startControlledGel(f) {
  let worker;
  f.window.Worker=class {
    constructor(){worker=this;}
    postMessage(message){this.message=message;}
    terminate(){this.terminated=true;}
  };
  f.doc.querySelector('[data-action="tab"][data-tab="gel"]').click();
  await until(()=>f.app.state.tab==='gel','gel workspace opens');
  f.doc.querySelector('[data-action="detect-gel"]').click();
  await until(()=>Boolean(worker?.message),'production detector reaches its worker');
  assert.equal(f.app.state.gelBusy,true);assert.equal(f.app.state.busy,null);
  return worker;
}
async function closeProjectMenu(f) {
  f.doc.querySelector('.modal [data-action="close-modal"]').click();
  await until(()=>!f.app.state.modal,'project menu closes');
}
async function startReviewedWb(f) {
  const edit=defaultEdit(f.app.state.project.assets[0]);
  edit.gel={...edit.gel,...gelLocalization()};f.app.state.project.settings.asset_edits['raw-a']=edit;
  Object.assign(f.app.state.channelDescriptors.get('raw-a:0:0'),{width_px:100,height_px:100});
  f.doc.querySelector('[data-action="tab"][data-tab="gel"]').click();await until(()=>f.app.state.tab==='gel','gel workspace opens');
  f.doc.querySelector('[data-action="open-wb-workflow"]').click();
  await until(()=>f.app.state.modal?.type==='wb-workflow','WB review opens from saved gel localization');
  const change=(selector,value)=>{
    const element=f.doc.querySelector(selector);assert.ok(element,selector);
    if(element.type==='checkbox')element.checked=value;else element.value=value;
    element.dispatchEvent(new f.window.Event('change',{bubbles:true}));
  };
  const rows=f.app.state.modal.draft.rows.map(row=>row.id);
  for(const [index,role] of ['target','control'].entries()){
    change(`[data-wb-row="${rows[index]}"][data-wb-row-field="role"]`,role);
    change(`[data-wb-row="${rows[index]}"][data-wb-row-field="protein"]`,role==='target'?'Target':'ACTB');
  }
  change('[data-wb-draft-field="experiment_id"]','Synthetic experiment');
  for(const index of [0,1])for(const [field,value] of Object.entries({sample_id:`S${index}`,group:'Control',biological_replicate:`B${index}`}))change(`[data-wb-lane="${index}"][data-wb-lane-field="${field}"]`,value);
  change('[data-wb-draft-field="confirmed"]',true);
  f.doc.querySelector('[data-action="wb-create-draft"]').click();
  await until(()=>f.app.state.wbWorking&&f.app.state.job?.status==='queued','WB generation reaches its quantitative task');
  assert.equal(f.app.state.busy,null);assert.equal(f.app.state.analysisSubmission,null);
}

test('gel task notice resolves after the actual detector accepts its worker result', async t => {
  const f=fixture(t,(call,app)=>call.path.includes('pixels.bin')?gelPixelsResponse():response({...app.state.project,revision:app.state.project.revision+1}));
  const worker=await startControlledGel(f);await blockedProjectSwitch(f,/条带识别仍在运行/);
  worker.onmessage({data:{id:worker.message.id,result:gelLocalization()}});
  await until(()=>!f.app.state.gelBusy,'gel detector finishes');
  assert.equal(f.app.state.gelSuggestions.lane_centers.length,2);
  assert.equal(Boolean(f.doc.querySelector('.operation-error')),false,`resolved guard remained: ${f.app.state.lastError}`);
  assert.equal(guardToasts(f.doc).length,0);
  assert.ok(![...f.doc.querySelectorAll('#toast-region .toast')].some(toast=>/条带识别仍在运行/.test(toast.textContent)));
});

test('gel task notice clears on the actual cancellation action and leaves an unrelated save error intact', async t => {
  const f=fixture(t,()=>gelPixelsResponse());const worker=await startControlledGel(f);
  await blockedProjectSwitch(f,/条带识别仍在运行/);await closeProjectMenu(f);
  const error='保存失败：备份目录无法写入';f.app.notify(error,'error');
  f.doc.querySelector('[data-action="cancel-gel"]').click();await until(()=>!f.app.state.gelBusy,'gel cancellation finishes');
  assert.equal(worker.terminated,true);assert.equal(f.app.state.lastError,error);
  assert.match(f.doc.querySelector('.operation-error').textContent,/备份目录无法写入/);
  assert.equal(guardToasts(f.doc).length,0);
  assert.ok(![...f.doc.querySelectorAll('#toast-region .toast')].some(toast=>/条带识别仍在运行/.test(toast.textContent)));
});

test('WB generation notice resolves when completed quantification has no usable groups and therefore skips chart generation', async t => {
  const queried=deferred();let params;
  const result={kind:'wb',rows:[],pairs:[],groups:[],summary:{}};
  const f=fixture(t,(call,app)=>{
    if(call.method==='PUT')return response({...call.body,revision:app.state.project.revision+1});
    if(call.method==='POST'){params=call.body.params;return response({id:'wb-empty-groups',status:'queued'});}
    if(call.path==='/api/project'){
      const project=structuredClone(app.state.project),sourceRevision=app.state.job.requestContext.sourceRevision;
      project.revision=sourceRevision+1;project.measurements.push({id:'wb-empty-groups',kind:'wb',params,source_asset_ids:['raw-a'],source_project_revision:sourceRevision,result});return response(project);
    }
    return queried.promise;
  });
  await startReviewedWb(f);await blockedProjectSwitch(f,/WB 图组仍在生成/);
  queried.resolve(response({id:'wb-empty-groups',status:'done',result}));
  await until(()=>!f.app.state.wbWorking,'WB generation finishes without a chart');
  assert.equal(f.app.state.job.status,'done');assert.equal(f.app.state.project.panels.length,2);
  assert.equal(f.calls.filter(call=>call.path==='/api/charts').length,0);
  assert.equal(Boolean(f.doc.querySelector('.operation-error')),false,`resolved guard remained: ${f.app.state.lastError}`);
  assert.equal(guardToasts(f.doc).length,0);
  assert.ok(![...f.doc.querySelectorAll('#toast-region .toast')].some(toast=>/WB 图组仍在生成/.test(toast.textContent)));
});

test('WB generation notice clears after cancelling its real quantitative task and preserves an unrelated analysis error', async t => {
  const queried=deferred();
  const f=fixture(t,(call,app)=>{
    if(call.method==='PUT')return response({...call.body,revision:app.state.project.revision+1});
    if(call.method==='POST')return response({id:'wb-cancelled',status:'queued'});
    if(call.method==='DELETE')return response({id:'wb-cancelled',status:'cancelled'});
    return queried.promise;
  });
  await startReviewedWb(f);await blockedProjectSwitch(f,/WB 图组仍在生成/);await closeProjectMenu(f);
  const error='分析未完成：另一原图的输入信息不完整';f.app.notify(error,'error');
  f.doc.querySelector('[data-action="cancel-job"]').click();await until(()=>f.app.state.job.status==='cancelled','quantitative task is cancelled');
  queried.resolve(response({id:'wb-cancelled',status:'running'}));await until(()=>!f.app.state.wbWorking,'WB generation releases its lifecycle');
  assert.equal(f.app.state.lastError,error);assert.match(f.doc.querySelector('.operation-error').textContent,/另一原图的输入信息不完整/);
  assert.equal(f.app.state.project.panels.length,2);assert.equal(guardToasts(f.doc).length,0);
  assert.ok(![...f.doc.querySelectorAll('#toast-region .toast')].some(toast=>/WB 图组仍在生成/.test(toast.textContent)));
});
