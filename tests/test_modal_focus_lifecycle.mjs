/** Production dialog rendering and keyboard handlers; DOM emulation only.
 * Actual native Tab traversal and rendered geometry are covered by Chromium.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {join} from 'node:path';
import {build} from 'esbuild';
import {Window} from 'happy-dom';

const root=fileURLToPath(new URL('../',import.meta.url));
const bundled=await build({stdin:{contents:`
  import {state,replaceProject,subscribe,mutate,persistProject,emit} from './web/modules/state.js';
  import {renderApp} from './web/modules/view.js';
  import {bindInteractions} from './web/modules/interaction.js';
  subscribe(renderApp);bindInteractions();
  window.workbench={state,replaceProject,mutate,persistProject,emit};
`,resolveDir:root,sourcefile:'modal-focus-test-entry.js'},bundle:true,write:false,format:'iife',platform:'browser'});
const index=(await readFile(join(root,'web/index.html'),'utf8')).replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'');

function fixture(t) {
  const window=new Window({url:'http://127.0.0.1:8765',settings:{
    disableCSSFileLoading:true,disableJavaScriptFileLoading:true,
    disableIframePageLoading:true,disableComputedStyleRendering:true,
  }});
  t.after(()=>window.happyDOM.abort());
  window.structuredClone=globalThis.structuredClone;
  // happy-dom has no layout. Mark connected controls as visible only for the
  // production trap's visibility filter; do not emulate native Tab traversal.
  const prototype=window.HTMLElement.prototype,offsetParent=Object.getOwnPropertyDescriptor(prototype,'offsetParent');
  Object.defineProperty(prototype,'offsetParent',{configurable:true,get(){return this.isConnected?this.parentElement:null;}});
  t.after(()=>{if(offsetParent)Object.defineProperty(prototype,'offsetParent',offsetParent);else delete prototype.offsetParent;});
  const saves=[];
  window.fetch=(url,options)=>{
    assert.equal(url,'/api/project');assert.equal(options.method,'PUT');
    const project=JSON.parse(options.body);
    return new Promise(resolve=>saves.push(()=>resolve(new Response(JSON.stringify({...project,revision:project.revision+1}),
      {status:200,headers:{'content-type':'application/json'}}))));
  };
  window.document.write(index);window.eval(bundled.outputFiles[0].text);
  const app=window.workbench;
  app.replaceProject({schema_version:1,id:'modal_focus',title:'Synthetic',revision:0,
    assets:[],panels:[],annotations:[],measurements:[],settings:{qpcr_workspaces:{}},
    figure:{width_mm:180,height_mm:120,background:'#fff'}});
  app.state.modal={type:'help'};app.emit();
  const doc=window.document;
  const focused=()=>doc.activeElement;
  return {window,doc,app,saves,focused};
}

test('a completed save keeps the current modal action focused despite replacing its old DOM',async t=>{
  const {doc,app,saves,focused}=fixture(t);
  app.mutate(project=>{project.title='Save while choosing a task';},'Edit title');
  const saving=app.persistProject();
  assert.equal(saves.length,1);
  const previousDialog=doc.querySelector('[role="dialog"]');
  doc.querySelector('.quick-start [data-task="qpcr"]').focus();
  assert.equal(focused().dataset.task,'qpcr');
  saves[0]();await saving;
  const currentDialog=doc.querySelector('[role="dialog"]');
  assert.equal(previousDialog.isConnected,false);
  assert.equal(previousDialog.contains(focused()),false,'a stale dialog reference cannot measure current focus');
  assert.equal(currentDialog.isConnected,true);
  assert.equal(currentDialog.contains(focused()),true);
  assert.equal(focused().dataset.task,'qpcr','save completion must not reset keyboard progress to the footer');
  assert.equal(app.state.dirty,false);assert.equal(app.state.saveState,'saved');
  for(let index=0;index<6;index++)app.emit();
  assert.equal(focused().dataset.task,'qpcr','additional font/status redraws preserve the same action');
});

test('modal Tab boundaries wrap and a detached/outside focus is recovered inside the current dialog',t=>{
  const {window,doc,app,focused}=fixture(t);
  function tab(shiftKey=false) {
    const event=new window.KeyboardEvent('keydown',{key:'Tab',shiftKey,bubbles:true,cancelable:true});
    focused().dispatchEvent(event);
    assert.equal(event.defaultPrevented,true,'the trap handles this boundary itself');
  }
  const buttons=[...doc.querySelectorAll('.modal button')];
  buttons.at(-1).focus();tab();assert.equal(focused(),buttons[0]);
  tab(true);assert.equal(focused(),buttons.at(-1));
  doc.body.tabIndex=-1;doc.body.focus();tab();assert.equal(focused(),buttons[0]);
  doc.body.focus();tab(true);assert.equal(focused(),buttons.at(-1));
  app.state.busy='Saving';app.state.busyController=new AbortController();app.emit();
  doc.querySelector('[data-action="cancel-busy"]').focus();
  const event=new window.KeyboardEvent('keydown',{key:'Tab',bubbles:true,cancelable:true});
  focused().dispatchEvent(event);
  assert.equal(event.defaultPrevented,false,'an inert busy dialog must not trap focus away from cancellation');
});

test('a different modal receives its own initial focus instead of inheriting a former button index',t=>{
  const {doc,app,focused}=fixture(t);
  doc.querySelector('.quick-start [data-task="qpcr"]').focus();
  app.state.modal={type:'new'};app.emit();
  assert.equal(focused().id,'new-project-title');
  assert.equal(doc.querySelector('[role="dialog"]').contains(focused()),true);
});

test('PPTX export rejects unsupported dimensions before submission and recovers after a valid canvas resize', t => {
  const {window,doc,app,saves}=fixture(t);
  app.state.project.annotations=[{id:'caption',type:'text',x_mm:10,y_mm:10,width_mm:40,height_mm:8,text:'Synthetic',font_size_pt:8}];
  app.state.project.figure.width_mm=2000;
  app.state.modal={type:'export',format:'pdf',dpi:300};app.emit();
  assert.equal(doc.querySelector('#export-format-pptx').disabled,true);
  assert.equal(doc.querySelector('#export-format-svg').disabled,false);
  assert.equal(doc.querySelector('[data-action="export-file"]').disabled,false,'PDF supports the full project canvas');
  assert.ok(doc.querySelector('#export-format-hint').textContent.includes('25.4–1422.4'));
  const unavailable=doc.querySelector('#export-format-pptx');
  unavailable.disabled=false;
  unavailable.dispatchEvent(new window.Event('change',{bubbles:true}));
  assert.equal(app.state.modal.format,'pdf','the handler also rejects an unsupported option');
  app.state.modal.format='pptx';app.emit();
  const submit=doc.querySelector('[data-action="export-file"]');
  assert.equal(submit.disabled,true);
  submit.disabled=false;submit.click();
  assert.equal(saves.length,0);
  assert.equal(app.state.busy,null);
  assert.equal(app.state.modal.type,'export');
  for (const [width,height,enabled] of [[25.4,1422.4,true],[25.399,120,false],[180,1422.401,false],[180,120,true]]) {
    app.state.project.figure.width_mm=width;app.state.project.figure.height_mm=height;app.emit();
    assert.equal(doc.querySelector('#export-format-pptx').disabled,!enabled);
    assert.equal(doc.querySelector('[data-action="export-file"]').disabled,!enabled);
  }
});
