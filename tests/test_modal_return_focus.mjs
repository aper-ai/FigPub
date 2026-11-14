/** Dialog focus lifecycle through production rendering and delegated actions. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {join} from 'node:path';
import {build} from 'esbuild';
import {Window} from 'happy-dom';

const root=fileURLToPath(new URL('../',import.meta.url));
const bundled=await build({stdin:{contents:`
  import {state,replaceProject,subscribe,emit} from './web/modules/state.js';
  import {renderApp} from './web/modules/view.js';
  import {bindInteractions} from './web/modules/interaction.js';
  subscribe(renderApp);bindInteractions();
  window.workbench={state,replaceProject,emit};
`,resolveDir:root,sourcefile:'modal-return-focus-test-entry.js'},bundle:true,write:false,format:'iife',platform:'browser'});
const index=(await readFile(join(root,'web/index.html'),'utf8')).replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'');

function fixture(t) {
  const window=new Window({url:'http://127.0.0.1:8765',settings:{
    disableCSSFileLoading:true,disableJavaScriptFileLoading:true,
    disableIframePageLoading:true,disableComputedStyleRendering:true,
  }});
  t.after(()=>window.happyDOM.abort());window.structuredClone=globalThis.structuredClone;
  window.fetch=async()=>{throw new Error('Dialog focus navigation must not request data');};
  window.document.write(index);window.eval(bundled.outputFiles[0].text);
  const app=window.workbench;
  app.replaceProject({id:'modal-return-project',title:'Synthetic focus',revision:0,assets:[],panels:[],annotations:[],measurements:[],settings:{},figure:{width_mm:180,height_mm:120}});
  const doc=window.document;
  async function click(selector){
    const element=typeof selector==='string'?doc.querySelector(selector):selector;
    assert.ok(element,selector);element.focus();element.click();await Promise.resolve();await Promise.resolve();
  }
  async function openHelp(){await click('.home-heading [data-action="help"]');assert.equal(app.state.modal.type,'help');assert.ok(doc.activeElement.closest('.modal'));}
  function escape(){doc.activeElement.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Escape',code:'Escape',bubbles:true,cancelable:true}));}
  return {window,doc,app,click,openHelp,escape};
}

for(const path of ['close button','Escape','backdrop'])test(`closing a redrawn dialog by ${path} returns focus to its exact opener`,async t=>{
  const {doc,app,click,openHelp,escape}=fixture(t);
  const initial=doc.querySelector('.home-heading [data-action="help"]');
  await openHelp();assert.equal(initial.isConnected,false,'rendering replaced the opener DOM');
  doc.querySelector('.quick-start [data-task="qpcr"]').focus();
  app.emit();app.emit();
  assert.equal(doc.activeElement.dataset.task,'qpcr');
  if(path==='close button')await click('.modal footer [data-action="close-modal"]');
  else if(path==='Escape')escape();
  else doc.querySelector('[data-modal-backdrop]').click();
  assert.equal(app.state.modal,null);
  assert.ok(doc.activeElement===doc.querySelector('.home-heading [data-action="help"]'),'the original home help action regains focus');
  assert.ok(doc.activeElement!==doc.querySelector('.workflow-meta [data-action="help"]'),'a same-action button elsewhere is not the opener');
});

test('transitioning between dialogs keeps the original outside opener',async t=>{
  const {doc,app,click,escape}=fixture(t);
  await click('[data-action="project-menu"]');
  await click('.modal [data-action="new-dialog"]');
  assert.equal(app.state.modal.type,'new');assert.equal(doc.activeElement.id,'new-project-title');
  escape();
  assert.equal(app.state.modal,null);
  assert.ok(doc.activeElement===doc.querySelector('[data-action="project-menu"]'),'the outside project menu regains focus');
});

test('closing a dialog after changing project or workspace does not focus a stale opener',async t=>{
  const {doc,app,openHelp,escape}=fixture(t);
  await openHelp();app.state.tab='compose';app.emit();escape();
  assert.equal(app.state.modal,null);
  assert.ok(doc.activeElement===doc.body,'workspace navigation does not reuse the old home control');
  app.state.tab='home';app.emit();await openHelp();
  app.state.project.id='a-different-project';app.emit();escape();
  assert.ok(doc.activeElement===doc.body,'a different project does not inherit the former dialog origin');
});

test('a removed task-specific opener never falls back to an unrelated same-action control',async t=>{
  const {doc,app,click,escape}=fixture(t);
  app.state.tab='compose';app.state.showGuide=false;app.emit();
  const extra=doc.createElement('section');extra.className='temporary-tool';
  extra.innerHTML='<button data-action="help">Temporary help</button>';
  doc.querySelector('.workspace-main').append(extra);
  await click(extra.querySelector('button'));
  assert.equal(extra.isConnected,false);
  escape();
  assert.equal(app.state.modal,null);
  assert.ok(doc.activeElement===doc.body,'a removed opener does not produce a fallback focus');
  assert.ok(doc.activeElement!==doc.querySelector('.workflow-meta [data-action="help"]'));
});

test('a busy operation defers focus return until the workspace is available',async t=>{
  const {doc,app,openHelp}=fixture(t);
  await openHelp();app.state.busy='Saving';app.state.modal=null;app.emit();
  assert.ok(doc.activeElement===doc.body,'the inert workspace does not regain focus');
  assert.equal(doc.querySelector('.app-shell').hasAttribute('inert'),true);
  app.state.busy=null;app.emit();
  assert.ok(doc.activeElement===doc.querySelector('.home-heading [data-action="help"]'),'the opener regains focus after the busy operation');
});
