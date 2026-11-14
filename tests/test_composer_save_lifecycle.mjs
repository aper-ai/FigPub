/** Production DOM/render/gesture handlers with a deliberately held save response.
 * DOM geometry and pointer capture are emulated; Windows browser acceptance
 * separately exercises the real SVG layout and native pointer events.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {build} from 'esbuild';
import {Window} from 'happy-dom';

const root=fileURLToPath(new URL('../',import.meta.url));
const entry=await build({stdin:{contents:`
  import {state,replaceProject,subscribe,mutate,persistProject,selectPanel,emit} from './web/modules/state.js';
  import {renderApp} from './web/modules/view.js';
  import {bindInteractions} from './web/modules/interaction.js';
  subscribe(renderApp);bindInteractions();
  window.workbench={state,replaceProject,mutate,persistProject,selectPanel,emit};
`,resolveDir:root,sourcefile:'composer-save-test-entry.js'},bundle:true,write:false,format:'iife',platform:'browser'});
const index=(await readFile(join(root,'web/index.html'),'utf8')).replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'');

async function fixture(t) {
  const window=new Window({url:'http://127.0.0.1:8765',settings:{
    disableCSSFileLoading:true,disableJavaScriptFileLoading:true,
    disableIframePageLoading:true,disableComputedStyleRendering:true,
  }});
  t.after(()=>window.happyDOM.abort());
  window.structuredClone=globalThis.structuredClone;
  window.DOMPoint=class {constructor(x,y){this.x=x;this.y=y;}matrixTransform(){return {x:this.x,y:this.y};}};
  window.SVGSVGElement.prototype.getScreenCTM=()=>({inverse:()=>({})});
  const captures=[];
  window.SVGSVGElement.prototype.setPointerCapture=function(pointerId){captures.push({element:this,pointerId});};
  const saves=[],errors=[];
  window.addEventListener('error',event=>errors.push(event.message));
  window.fetch=(url,options)=>{
    assert.equal(url,'/api/project');assert.equal(options.method,'PUT');
    const project=JSON.parse(options.body);
    return new Promise(resolve=>saves.push({project,complete:()=>resolve(new Response(
      JSON.stringify({...project,revision:project.revision+1}),{status:200,headers:{'content-type':'application/json'}}))}));
  };
  window.document.write(index);
  window.eval(entry.outputFiles[0].text);
  const app=window.workbench,assetId='a'.repeat(64);
  app.state.tab='compose';
  app.replaceProject({schema_version:1,id:'save_during_gesture',title:'Synthetic',revision:0,
    assets:[{id:assetId,sha256:assetId,kind:'image',name:'Synthetic.png',width_px:100,height_px:100,channels:1}],
    panels:[{id:'first',asset_id:assetId,x_mm:10,y_mm:20,width_mm:20,height_mm:20,label:'A'}],
    annotations:[],measurements:[],settings:{qpcr_workspaces:{}},figure:{width_mm:180,height_mm:120,background:'#fff'}});
  app.selectPanel('first');
  async function until(predicate,description) {
    const deadline=Date.now()+3000;
    while(Date.now()<deadline){if(predicate())return;await delay(5);}
    throw new Error(`${description}: ${errors.join('; ')}`);
  }
  function pointer(target,type,x,y) {
    target.dispatchEvent(new window.PointerEvent(type,{bubbles:true,button:0,pointerId:7,clientX:x,clientY:y}));
  }
  return {window,app,saves,errors,captures,until,pointer};
}

test('desktop layout fields preserve full geometry on unchanged change and accept a deliberate rounded-value edit', async t => {
  const {window,app,errors,until}=await fixture(t);
  const panel=app.state.project.panels[0];
  panel.x_mm=45.89866666666667;
  panel.width_mm=23.333333333333332;
  panel.height_mm=15.555555555555555;
  app.emit();
  const precise=JSON.stringify({x:panel.x_mm,width:panel.width_mm,height:panel.height_mm});
  const width=window.document.getElementById('panel-width_mm');
  assert.equal(width.value,'23.333');
  assert.equal(width.step,'any');
  width.focus();
  width.dispatchEvent(new window.Event('change',{bubbles:true}));
  width.blur();
  assert.equal(JSON.stringify({x:panel.x_mm,width:panel.width_mm,height:panel.height_mm}),precise);
  assert.equal(app.state.history.length,0,'an unchanged numeric field is not an edit');

  const x=window.document.getElementById('panel-x_mm');
  x.focus();
  x.value='45.899';
  x.dispatchEvent(new window.InputEvent('input',{bubbles:true,inputType:'insertText',data:'45.899'}));
  app.emit();
  assert.equal(window.document.getElementById('panel-x_mm'),x,'a deferred redraw retains the pending user edit');
  x.dispatchEvent(new window.Event('change',{bubbles:true}));
  x.blur();
  await until(()=>!app.state.formCommitPending,'numeric edit committed');
  assert.equal(app.state.project.panels[0].x_mm,45.899);
  assert.equal(app.state.project.panels[0].width_mm,23.333333333333332);
  assert.equal(app.state.project.panels[0].height_mm,15.555555555555555);
  assert.equal(app.state.history.length,1);
  assert.deepEqual(errors,[]);
});

for(const mode of ['move','resize','cancel'])test(`a prior save response arriving during ${mode} preserves the active SVG and edit transaction`,async t=>{
  const {window,app,saves,errors,captures,until,pointer}=await fixture(t);
  const geometry=()=>JSON.parse(JSON.stringify(app.state.project.panels.map(panel=>({x:panel.x_mm,y:panel.y_mm,w:panel.width_mm,h:panel.height_mm}))));
  app.mutate(project=>{project.title='Title saved before the gesture';},'Edit title');
  const original=geometry(),beforeSave=app.persistProject();
  await until(()=>saves.length===1,'first save was submitted');
  const svg=window.document.getElementById('figure-svg');
  const target=svg.querySelector(mode==='resize'?'[data-handle="se"]':'.panel-hit');
  pointer(target,'pointerdown',30,30);
  assert.equal(app.state.interactionLock,true);
  assert.equal(captures.at(-1).element,svg);
  pointer(svg,'pointermove',38,35);
  await until(()=>JSON.stringify(geometry())!==JSON.stringify(original),'actual live drag geometry');
  const live=geometry();
  // Resize redraws the panel's inner handle as its dimensions change; pointer
  // capture belongs to the enclosing SVG, which must remain attached throughout.
  const liveTarget=mode==='resize'?svg.querySelector('[data-handle="se"]'):target;
  assert.equal(liveTarget.isConnected,true);
  saves[0].complete();
  await beforeSave;
  assert.equal(app.state.interactionLock,true);
  assert.equal(window.document.getElementById('figure-svg'),svg,'a save completion must not replace a pointer capture target');
  assert.equal(svg.isConnected,true);assert.equal(liveTarget.isConnected,true);
  assert.deepEqual(geometry(),live,'older saved geometry must not overwrite the in-progress edit');
  assert.equal(saves[0].project.panels[0].x_mm,original[0].x);
  assert.equal(saves[0].project.panels[0].width_mm,original[0].w);
  assert.equal(app.state.history.length,1,'a live drag has not yet committed an undo step');

  pointer(svg,mode==='cancel'?'pointercancel':'pointerup',38,35);
  await until(()=>!app.state.interactionLock&&!app.state.formPointerPending,'native pointer transaction ended');
  if(mode==='cancel') {
    assert.deepEqual(geometry(),original);
    assert.equal(app.state.history.length,1);
    assert.equal(app.state.dirty,false);
    assert.equal(saves.length,1);
  } else {
    assert.deepEqual(geometry(),live);
    assert.equal(app.state.history.length,2,'exactly one additional committed gesture');
    assert.equal(app.state.dirty,true);
    const afterSave=app.persistProject();
    await until(()=>saves.length===2,'completed gesture save was submitted');
    assert.equal(saves[1].project.panels[0].x_mm,live[0].x);
    assert.equal(saves[1].project.panels[0].width_mm,live[0].w);
    saves[1].complete();await afterSave;
    assert.equal(app.state.dirty,false);assert.equal(app.state.saveState,'saved');
  }
  assert.deepEqual(errors,[]);
});
