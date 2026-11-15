/** Production startup, dialogs and cleanup transitions; synthetic metadata only. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {build} from 'esbuild';
import {Window} from 'happy-dom';

const root=fileURLToPath(new URL('../',import.meta.url));
const bundled=await build({stdin:{contents:`
  import './web/app.js';
  import {state,replaceProject,mutate,emit} from './web/modules/state.js';
  import {channelPreviews,loadChannelDescriptor} from './web/modules/channels.js';
  window.workbench={state,replaceProject,mutate,emit,channelPreviews,loadChannelDescriptor};
`,resolveDir:root,sourcefile:'workspace-privacy-entry.js'},bundle:true,write:false,format:'iife',platform:'browser'});
const index=(await readFile(join(root,'web/index.html'),'utf8')).replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'');
function project(id='empty',assets=[]) {return {schema_version:1,id,title:id,revision:0,assets,panels:[],annotations:[],measurements:[],settings:{qpcr_workspaces:{}},figure:{width_mm:180,height_mm:120,background:'#fff'}};}
const synthetic={id:'fixture_image',kind:'image',name:'SYNTHETIC_ONLY.png',width_px:24,height_px:12,channels:1,bit_depth:8};
const json=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{'content-type':'application/json'}});
async function until(predicate,message) {for(let i=0;i<100;i++){if(predicate())return;await delay(5);}assert.fail(message);}
async function fixture(t,{status={has_recovery:false},current=project(),handler=()=>null}={}) {
  const window=new Window({url:'http://127.0.0.1:8765',settings:{disableCSSFileLoading:true,disableJavaScriptFileLoading:true,disableIframePageLoading:true,disableComputedStyleRendering:true}});
  t.after(()=>window.happyDOM.abort());window.structuredClone=structuredClone;
  const calls=[],revoked=[];window.URL.revokeObjectURL=url=>revoked.push(url);
  window.fetch=async (path,options={})=>{
    const call={path,method:options.method||'GET',body:options.body?JSON.parse(options.body):null};calls.push(call);
    const override=handler(call);if(override)return override;
    if(path==='/api/health')return json({version:'fixture'});
    if(path==='/api/workspace/status')return json(status);
    if(path==='/api/project'&&call.method==='GET')return json(current);
    if(path==='/api/project'&&call.method==='PUT')return json({...call.body,revision:call.body.revision+1});
    throw new Error(`Unexpected request ${call.method} ${path}`);
  };
  window.document.write(index);window.eval(bundled.outputFiles[0].text);
  const app=window.workbench,doc=window.document;
  await until(()=>app.state.project&&doc.querySelector('.app-shell'),'startup did not finish');
  async function click(action) {
    const button=doc.querySelector(`[data-action="${action}"]`);assert.ok(button,`missing ${action}`);button.click();
    await delay(1);await until(()=>!app.state.busy,`${action} remained busy`);
  }
  return {window,doc,app,calls,revoked,click};
}

test('startup keeps previous image metadata out of the workspace until explicit restore',async t=>{
  const recovered=project('restored',[synthetic]);
  const {window,doc,app,calls,click}=await fixture(t,{status:{has_recovery:true,recovery:{title:'PRIVATE_DO_NOT_SHOW',asset_count:1}},handler:call=>call.path==='/api/workspace/restore'?json(recovered):null});
  assert.equal(app.state.project.assets.length,0);assert.equal(app.state.modal.type,'workspace-recovery');
  assert.doesNotMatch(doc.body.textContent,/PRIVATE_DO_NOT_SHOW|SYNTHETIC_ONLY/);
  assert.equal(doc.querySelectorAll('.asset-thumb img').length,0);
  assert.equal(calls.some(call=>call.path==='/api/workspace/restore'),false);
  doc.body.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
  assert.equal(app.state.modal.type,'workspace-recovery','Escape cannot silently discard recovery or reveal old images');
  await click('restore-workspace');assert.equal(app.state.project.assets.length,1);assert.equal(app.state.modal,null);
  assert.equal(app.state.workspaceStatus.has_recovery,false);
  assert.equal(calls.filter(call=>call.path==='/api/workspace/restore').length,1);
});

test('explicitly confirmed cleanup releases undo, source descriptors and preview URLs without saving another old copy',async t=>{
  const {app,doc,calls,revoked,click}=await fixture(t,{current:project('active',[synthetic]),handler:call=>call.path==='/api/workspace/clear'?json({project:project('clean'),cleared:true,requires_restart:true}):null});
  app.state.channelDescriptors.set('old',{private:true});app.state.channelDescriptorErrors.set('old','fixture');
  app.channelPreviews.entries.set('old',{url:'blob:fixture',bytes:0,controller:null});
  app.mutate(value=>{value.annotations.push({id:'note',type:'text',text:'discarded',x_mm:0,y_mm:0,width_mm:20,height_mm:10});});
  assert.equal(app.state.dirty,true);
  await click('clear-workspace-dialog');assert.equal(calls.some(call=>call.path==='/api/workspace/clear'),false);
  await click('clear-workspace');
  assert.deepEqual(calls.find(call=>call.path==='/api/workspace/clear').body,{confirm:true});
  assert.equal(calls.some(call=>call.path==='/api/project'&&call.method==='PUT'),false);
  assert.equal(app.state.project.assets.length,0);assert.equal(app.state.history.length,0);assert.equal(app.state.future.length,0);
  assert.equal(app.state.channelDescriptors.size,0);assert.equal(app.state.channelDescriptorErrors.size,0);assert.deepEqual(revoked,['blob:fixture']);
  assert.equal(app.state.modal.type,'workspace-cleanup-exit');assert.match(doc.querySelector('.modal').textContent,/退出桌面客户端/);
  assert.doesNotMatch(doc.querySelector('.modal').textContent,/全部.*清除成功/);
});

test('cleanup failure preserves the current project and exposes a retry instead of reporting success',async t=>{
  const {app,doc,click}=await fixture(t,{current:project('keep',[synthetic]),handler:call=>call.path==='/api/workspace/clear'?json({detail:{code:'workspace_busy',message:'模拟：任务未结束，请重试'}},409):null});
  await click('clear-workspace-dialog');await click('clear-workspace');
  assert.equal(app.state.project.assets.length,1);assert.equal(app.state.modal.type,'clear-workspace');
  assert.match(app.state.lastError,/任务未结束/);assert.doesNotMatch(doc.getElementById('toast-region').textContent,/副本已删除|副本已清除/);
});

test('partial cleanup failure hides remaining image UI, requires retry, and never claims completion',async t=>{
  let failed=false;
  const {app,doc,click}=await fixture(t,{current:project('partial',[synthetic]),handler:call=>{
    if(call.path==='/api/workspace/clear'){failed=true;return json({detail:'模拟：原件被占用'},503);}
    if(failed&&call.path==='/api/workspace/status')return json({cleanup_pending:true,has_recovery:false});
    if(failed&&call.path==='/api/project'&&call.method==='GET')return json(project('safe-empty'));
    return null;
  }});
  await click('clear-workspace-dialog');await click('clear-workspace');
  assert.equal(app.state.project.assets.length,0);assert.equal(app.state.modal.type,'workspace-cleanup');
  assert.match(doc.querySelector('.modal').textContent,/清理未完成/);assert.match(doc.querySelector('.modal').textContent,/重试清理/);
  assert.equal(doc.querySelector('.modal [data-action="restore-workspace"]'),null);
  assert.doesNotMatch(doc.getElementById('toast-region').textContent,/副本已删除|副本已清除/);
});

test('production navigation has no generated sample action on the home page or empty workspaces',async t=>{
  const {app,doc,click}=await fixture(t);
  assert.equal(doc.querySelector('[data-action="demo"]'),null);assert.equal(doc.querySelector('[data-action="load-demo"]'),null);
  assert.ok(doc.querySelector('[data-action="clear-workspace-dialog"]'));
  for(const tab of ['source','gel','channels','analysis','compose']){
    app.state.tab=tab;app.emit();assert.equal(doc.querySelector('[data-action="demo"]'),null,tab);
  }
  await click('project-menu');assert.ok(doc.querySelector('.modal [data-action="clear-workspace-dialog"]'));
});

test('a late source descriptor response cannot repopulate the cleared workspace cache',async t=>{
  let respond;
  const {app,click}=await fixture(t,{current:project('old-source',[synthetic]),handler:call=>{
    if(call.path.includes('/channels?'))return new Promise(resolve=>{respond=resolve;});
    if(call.path==='/api/workspace/clear')return json({project:project('fresh'),cleared:true,requires_restart:false});
    return null;
  }});
  const loading=app.loadChannelDescriptor({asset_id:synthetic.id,series:0,frame:0});
  await until(()=>typeof respond==='function','descriptor request did not start');
  await click('clear-workspace-dialog');await click('clear-workspace');
  respond(json({source_name:'SYNTHETIC_OLD_DESCRIPTOR'}));await loading;
  assert.equal(app.state.channelDescriptors.size,0);assert.equal(app.state.channelDescriptorErrors.size,0);
  assert.equal(app.state.project.id,'fresh');
});
