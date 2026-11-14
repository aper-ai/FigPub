/** Reproduce asynchronous review failures through the actual UI handlers.
 * DOM emulation is used here; this is not native/WebView visual acceptance.
 */
import test, {after} from 'node:test';
import assert from 'node:assert/strict';
import {deflateSync} from 'node:zlib';
import {setTimeout as delay} from 'node:timers/promises';
import {Window} from 'happy-dom';
import {bindInstanceReview,renderInstanceReview} from '../web/modules/instance-review.js';
import {state,flushSave} from '../web/modules/state.js';
import {ensureProject} from '../web/modules/domain.js';
import {defaultAnalysisParams} from '../web/modules/analysis-domain.js';
import {openExposureReview,handleWbAction,handleWbInput} from '../web/modules/wb-workflow.js';

const window=new Window({url:'http://127.0.0.1:8765'}),originalDocument=globalThis.document;
globalThis.document=window.document;
after(async()=>{await window.happyDOM.abort();globalThis.document=originalDocument;});
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
async function until(predicate) {for(let i=0;i<100;i++){if(predicate())return;await delay(5);}assert.fail('Expected asynchronous review state was not reached');}

function mask(values,sha256) {
  const pixels=new Uint32Array(values);
  return {encoding:'uint32-le-zlib-base64-v1',shape:[2,2],source_shape:[2,2],origin:[0,0],nbytes:16,
    sha256,data:deflateSync(new Uint8Array(pixels.buffer)).toString('base64')};
}
function reviewFixture(projectId,{invalid=false}={}) {
  const base={kind:'microscopy',count:1,instance_mask:mask([1,1,1,1],'base'),
    objects:[{id:1,area_px:4,mean_intensity:37,integrated_intensity:148,contour:[[0,0],[2,0],[2,2],[0,2]]}]};
  const recipe={base_mask_sha256:invalid?'wrong-base':'base',operations:[{id:'delete-1',type:'delete',ids:[1]}],cursor:1};
  const project={id:projectId,revision:2,settings:{instance_reviews:{measurement:recipe}}};
  const selection={record:{id:'measurement',kind:'microscopy',params:{method:'nuclei',source:{asset_id:'asset',series:0,frame:0,channel:0}}},
    result:base,stale:false,persisted:true};
  const requests=[],downloads=[],pending=[];
  let flush=async()=>{};
  const emit=()=>{document.body.innerHTML=renderInstanceReview(selection);};
  bindInstanceReview({state:{project},currentSelection:()=>selection,emit,flushSave:()=>flush(),
    request:async(path,options)=>{requests.push({path,body:options.body});const next=deferred();pending.push(next);
      options.signal?.addEventListener('abort',()=>next.reject(new DOMException('Cancelled','AbortError')),{once:true});return next.promise;},
    mutate:change=>{change(project);emit();},saveBlob:(blob,name)=>downloads.push({blob,name})});
  emit();
  return {base,recipe,project,selection,emit,requests,downloads,pending,setFlush:operation=>{flush=operation;}};
}

test('restored instance edits cannot export automatic results while replay is pending or failed',async()=>{
  const fixture=reviewFixture('saved-review');
  document.querySelector('[data-instance-action="toggle"]').click();
  // Check the first render too: replay has only been queued in a microtask.
  assert.ok(document.querySelector('[data-instance-action="csv"]').disabled);
  assert.match(document.body.textContent,/重新测量完成后/);
  await until(()=>fixture.requests.length===1);
  fixture.pending[0].reject(new Error('temporary replay failure'));
  await until(()=>document.body.textContent.includes('temporary replay failure'));
  const blocked=document.querySelector('[data-instance-action="csv"]');
  assert.ok(blocked.disabled);
  blocked.disabled=false;blocked.click(); // handler must enforce the guard too
  assert.equal(fixture.downloads.length,0);
  document.querySelector('[data-instance-action="retry"]').click();
  await until(()=>fixture.requests.length===2);
  fixture.pending[1].resolve({result:{...fixture.base,count:0,objects:[],instance_mask:mask([0,0,0,0],'reviewed'),
    instance_review:{base_mask_sha256:'base',cursor:1,operations:[{id:'delete-1',type:'delete',ids:[1]}],original_count:1}}});
  await until(()=>!document.querySelector('[data-instance-action="csv"]')?.disabled);
  document.querySelector('[data-instance-action="csv"]').click();
  assert.equal(fixture.downloads.length,1);
  assert.equal(await fixture.downloads[0].blob.text(),'');
  assert.equal(fixture.selection.result.count,1,'automatic base remains unchanged');
});

test('correcting an invalid saved review releases the error state without reloading the project',()=>{
  const fixture=reviewFixture('corrected-review',{invalid:true});
  assert.ok(document.querySelector('[data-instance-action="toggle"]').disabled);
  fixture.project.settings.instance_reviews.measurement={base_mask_sha256:'base',operations:[],cursor:0};
  fixture.emit();
  assert.equal(document.querySelector('[data-instance-action="toggle"]').disabled,false);
});

test('cancelled saved instance restoration remains paused until an explicit retry',async()=>{
  const fixture=reviewFixture('cancelled-restoration');
  document.querySelector('[data-instance-action="toggle"]').click();
  await until(()=>fixture.requests.length===1);
  document.querySelector('[data-instance-action="cancel"]').click();
  await until(()=>!document.querySelector('[data-instance-action="cancel"]'));
  fixture.emit();await delay(20);
  assert.equal(fixture.requests.length,1,'rendering after cancellation must not restart the request');
  assert.match(document.body.textContent,/恢复已暂停/);
  assert.ok(document.querySelector('[data-instance-action="csv"]').disabled);
  assert.equal(fixture.project.settings.instance_reviews.measurement.cursor,1,'saved edits remain intact');
  document.querySelector('[data-instance-action="retry"]').click();
  await until(()=>fixture.requests.length===2);
  fixture.pending[1].resolve({result:{...fixture.base,count:0,objects:[],instance_mask:mask([0,0,0,0],'retried'),
    instance_review:{base_mask_sha256:'base',cursor:1,operations:fixture.recipe.operations,original_count:1}}});
  await until(()=>document.querySelector('[data-instance-action="csv"]')?.disabled===false);
  assert.equal(fixture.requests.length,2);
});

test('cancelling instance restoration while saving does not submit a new request afterward',async()=>{
  const fixture=reviewFixture('cancelled-before-request'),save=deferred();fixture.setFlush(()=>save.promise);
  document.querySelector('[data-instance-action="toggle"]').click();
  await until(()=>document.querySelector('[data-instance-action="cancel"]'));
  document.querySelector('[data-instance-action="cancel"]').click();save.resolve();
  await until(()=>!document.querySelector('[data-instance-action="cancel"]'));
  assert.equal(fixture.requests.length,0);assert.match(document.body.textContent,/恢复已暂停/);
});

for(const phase of ['save','compute'])test(`instance edit preserves newer local undo during ${phase}`,async()=>{
  const fixture=reviewFixture(`undo-during-${phase}`);
  fixture.recipe.operations.push({id:'add-2',type:'add',region:{type:'rectangle',x:0,y:0,width:1,height:1}});
  fixture.emit();document.querySelector('[data-instance-action="toggle"]').click();
  await until(()=>fixture.requests.length===1);
  fixture.pending[0].resolve({result:{...fixture.base,count:0,objects:[],instance_mask:mask([0,0,0,0],'before-redo'),
    instance_review:{base_mask_sha256:'base',cursor:1,operations:fixture.recipe.operations.slice(0,1),original_count:1}}});
  await until(()=>document.querySelector('[data-instance-action="redo"]')?.disabled===false);
  const save=deferred();if(phase==='save')fixture.setFlush(()=>save.promise);
  document.querySelector('[data-instance-action="redo"]').click();
  await until(()=>phase==='save'?document.querySelector('[data-instance-action="cancel"]'):fixture.requests.length===2);
  const revision=fixture.project.revision,undone={base_mask_sha256:'base',operations:[],cursor:0};
  fixture.project.settings.instance_reviews.measurement=undone;fixture.emit();
  if(phase==='save')save.resolve();
  else fixture.pending[1].resolve({result:{...fixture.base,instance_mask:mask([2,0,0,0],'obsolete-redo'),
    instance_review:{base_mask_sha256:'base',cursor:2,operations:fixture.recipe.operations,original_count:1}}});
  await until(()=>document.body.textContent.includes('复核记录已改变'));
  assert.equal(fixture.project.revision,revision,'local undo races before autosave updates the revision');
  assert.deepEqual(fixture.project.settings.instance_reviews.measurement,undone);
  assert.equal(fixture.requests.length,phase==='save'?1:2);
  assert.equal(fixture.selection.result.instance_mask.sha256,'base','automatic source is immutable');
});

function setupExposure() {
  state.project=ensureProject({id:'exposure-project',revision:1,title:'Synthetic exposure test',
    assets:[{id:'asset',kind:'image',width_px:40,height_px:50}],settings:{analysis_rois:[
      {id:'target',asset_id:'asset',source:{asset_id:'asset',series:0,frame:0,channel:0},x:3,y:5,width:20,height:10,
        role:'target',protein:'Target',sample_id:'s1',group:'Control',biological_replicate:'b1',technical_replicate:'1',experiment_id:'E',exposure_id:'short'}]}});
  state.analysisParams=defaultAnalysisParams();state.analysisKind='wb';state.selectedAssetId='asset';
  state.selectedPanelId=null;state.selectedPanelIds=[];state.sourcePointerIdentity=null;state.sourceRoi=null;
  state.dirty=false;state.history=[];state.future=[];state.resultRecord=null;
  document.body.innerHTML='<div id="toast-region"></div>';
}

for(const field of ['sample','adc'])test(`late exposure response preserves ${field} edits and cannot be applied`,async t=>{
  setupExposure();const response=deferred();let requestBody;
  t.mock.method(globalThis,'fetch',async(path,options)=>{
    if(path==='/api/project'){const body=JSON.parse(options.body);return new Response(JSON.stringify({...body,revision:body.revision+1}),{headers:{'Content-Type':'application/json'}});}
    if(path==='/api/wb/exposure-review'){requestBody=JSON.parse(options.body);return response.promise;}
    throw new Error(`Unexpected request: ${path}`);
  });
  openExposureReview();const modal=state.modal,work=handleWbAction('wb-review-exposures');
  await until(()=>requestBody);
  const input=document.createElement('input');
  if(field==='sample'){input.setAttribute('data-wb-exposure-row','0');input.setAttribute('data-wb-exposure-key','sample_id');input.value='corrected-sample';}
  else{input.setAttribute('data-wb-exposure-setting','adc_bits');input.value='12';}
  handleWbInput(input);
  response.resolve(new Response(JSON.stringify({groups:[{id:'cohort',recommended_pair_id:'short'}],rows:[],roi_decisions:[],method:{}}),{headers:{'Content-Type':'application/json'}}));
  await work;
  assert.equal(modal.review,null);
  assert.deepEqual(modal.selected_groups,[]);
  assert.match(modal.error,/复核输入已改变/);
  assert.equal(field==='sample'?modal.rois[0].sample_id:modal.adc_bits,field==='sample'?'corrected-sample':'12');
  assert.equal(state.project.settings.analysis_rois[0].sample_id,'s1','unsaved modal edits are not silently committed');
  await handleWbAction('wb-apply-exposures');
  assert.equal(state.modal,modal,'stale review cannot close or apply');
  await flushSave();state.modal=null;
});

test('changing detector range after an exposure review prevents applying its outdated recommendation',async t=>{
  setupExposure();
  t.mock.method(globalThis,'fetch',async(_path,options)=>{
    const body=JSON.parse(options.body);return new Response(JSON.stringify({...body,revision:body.revision+1}),{headers:{'Content-Type':'application/json'}});
  });
  openExposureReview();const modal=state.modal;
  modal.review={groups:[],roi_decisions:[],method:{}};modal.selected_groups=['cohort'];
  state.analysisParams.adc_bits=12;
  await handleWbAction('wb-apply-exposures');
  assert.equal(state.modal,modal);assert.match(modal.error,/探测器范围已经改变/);
  await flushSave();state.modal=null;
});
