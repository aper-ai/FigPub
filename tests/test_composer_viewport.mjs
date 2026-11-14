/** Real render and zoom handlers; client dimensions/ResizeObserver are supplied
 * because happy-dom has no layout engine. Browser acceptance measures real boxes.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {join} from 'node:path';
import {build} from 'esbuild';
import {Window} from 'happy-dom';

const root=fileURLToPath(new URL('../',import.meta.url));
const entry=await build({stdin:{contents:`
  import {state,replaceProject,subscribe,emit} from './web/modules/state.js';
  import {renderApp} from './web/modules/view.js';
  import {bindInteractions} from './web/modules/interaction.js';
  import {composerPixelsPerMm,composerPreviewSize,syncComposerViewport} from './web/modules/composer-viewport.js';
  window.renderCount=0;
  subscribe(()=>{window.renderCount++;renderApp();});bindInteractions();
  window.workbench={state,replaceProject,emit,composerPixelsPerMm,composerPreviewSize,syncComposerViewport};
`,resolveDir:root,sourcefile:'composer-viewport-test-entry.js'},bundle:true,write:false,format:'iife',platform:'browser'});
const index=(await readFile(join(root,'web/index.html'),'utf8')).replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'');

function fixture(t,{width=760,height=435,figureWidth=180,figureHeight=120}={}) {
  const window=new Window({url:'http://127.0.0.1:8765',settings:{disableCSSFileLoading:true,disableJavaScriptFileLoading:true,disableIframePageLoading:true}});
  t.after(()=>window.happyDOM.abort());
  window.structuredClone=globalThis.structuredClone;
  const size={width,height},frames=new Map(),observers=[];
  let sequence=0;
  window.requestAnimationFrame=callback=>{const id=++sequence;frames.set(id,callback);return id;};
  window.cancelAnimationFrame=id=>frames.delete(id);
  window.ResizeObserver=class {
    constructor(callback){this.callback=callback;observers.push(this);}
    observe(target){this.target=target;this.connected=true;}
    disconnect(){this.connected=false;}
  };
  for(const [property,dimension] of [['clientWidth','width'],['clientHeight','height']]) {
    Object.defineProperty(window.HTMLElement.prototype,property,{configurable:true,get(){
      return this.matches('.artboard-viewport')?size[dimension]:0;
    }});
  }
  window.document.write(index);
  const style=window.document.createElement('style');
  style.textContent='.artboard-viewport{padding:40px 32px}';window.document.head.append(style);
  window.eval(entry.outputFiles[0].text);
  const app=window.workbench,id='a'.repeat(64);
  app.state.tab='compose';
  app.replaceProject({schema_version:1,id:'fit_actual_viewport',title:'Synthetic',revision:0,
    assets:[{id,sha256:id,kind:'image',name:'Synthetic.png',width_px:100,height_px:100,channels:1}],
    panels:[{id:'panel',asset_id:id,x_mm:12,y_mm:18,width_mm:20,height_mm:20,label:'A'}],
    annotations:[],measurements:[],settings:{qpcr_workspaces:{}},
    figure:{width_mm:figureWidth,height_mm:figureHeight,background:'#ffffff'}});
  const svg=()=>window.document.getElementById('figure-svg');
  const viewport=()=>window.document.querySelector('.artboard-viewport');
  function flushFrames() {const pending=[...frames.values()];frames.clear();for(const callback of pending)callback();}
  function expectFit() {
    const expected=Math.min((size.width-64)/figureWidth,(size.height-80)/figureHeight);
    assert.ok(Math.abs(Number(svg().getAttribute('width'))-figureWidth*expected)<1e-8);
    assert.ok(Math.abs(Number(svg().getAttribute('height'))-figureHeight*expected)<1e-8);
    assert.equal(svg().getAttribute('viewBox'),`0 0 ${figureWidth} ${figureHeight}`);
  }
  return {window,app,size,frames,observers,svg,viewport,flushFrames,expectFit};
}

for(const options of [
  {width:760,height:435},
  {width:540,height:360,figureWidth:120,figureHeight:180},
  {width:1080,height:590,figureWidth:2000,figureHeight:2000},
])test(`first mounted ${options.figureWidth || 180} mm canvas fits the actual ${options.width}×${options.height} content box`,t=>{
  const {app,expectFit}=fixture(t,options);
  expectFit();
  assert.equal(app.state.figureZoom,1);
  assert.equal(app.state.history.length,0);
  assert.equal(app.state.dirty,false);
});

test('container-only resizing updates the same SVG without rerendering or changing millimetre coordinates',t=>{
  const {window,app,size,observers,svg,flushFrames,expectFit}=fixture(t);
  const before=JSON.stringify(app.state.project),original=svg(),renders=window.renderCount;
  size.height=320;size.width=600;
  observers.at(-1).callback();observers.at(-1).callback();flushFrames();
  expectFit();
  assert.equal(svg(),original);
  assert.equal(window.renderCount,renders);
  assert.equal(JSON.stringify(app.state.project),before);
  assert.equal(app.state.dirty,false);
});

test('fit action restores zoom and both scroll axes without altering the project',async t=>{
  const {window,app,svg,viewport,expectFit}=fixture(t);
  const before=JSON.stringify(app.state.project);
  app.state.figureZoom=2.4;app.emit();
  assert.ok(Number(svg().getAttribute('height'))>355);
  viewport().scrollTop=140;viewport().scrollLeft=90;
  window.document.querySelector('[data-action="zoom-fit"]').click();
  await Promise.resolve();await Promise.resolve();
  expectFit();
  assert.equal(app.state.figureZoom,1);
  assert.equal(viewport().scrollTop,0);assert.equal(viewport().scrollLeft,0);
  assert.equal(JSON.stringify(app.state.project),before);
  assert.equal(app.state.history.length,0);
});

test('viewport changes wait until a pointer gesture releases its SVG transform',t=>{
  const {app,size,observers,svg,flushFrames,expectFit}=fixture(t);
  const original=svg(),width=original.getAttribute('width'),height=original.getAttribute('height');
  app.state.interactionLock=true;size.height=300;
  observers.at(-1).callback();flushFrames();
  assert.equal(svg(),original);
  assert.equal(svg().getAttribute('width'),width);assert.equal(svg().getAttribute('height'),height);
  app.state.interactionLock=false;app.emit();
  expectFit();
});

test('channel preview resolution uses the mounted SVG scale and device pixel ratio',t=>{
  const {app,svg}=fixture(t);
  const actualScale=Number(svg().getAttribute('height'))/120;
  assert.equal(app.composerPixelsPerMm(),actualScale);
  assert.equal(app.composerPreviewSize(90,60,actualScale,1),512);
  assert.equal(app.composerPreviewSize(90,60,actualScale,2),1024);
  app.state.figureZoom=2;app.emit();
  assert.equal(app.composerPixelsPerMm(),actualScale*2);
  assert.equal(app.composerPreviewSize(90,60),1024);
});

test('stable ResizeObserver callbacks do not schedule a render loop and detached views are disconnected',t=>{
  const {window,app,observers,frames,flushFrames}=fixture(t);
  const renders=window.renderCount,first=observers.at(-1);
  first.callback();first.callback();assert.equal(frames.size,1);flushFrames();
  assert.equal(frames.size,0);assert.equal(window.renderCount,renders);
  app.emit();assert.equal(first.connected,false);
  const current=observers.at(-1);assert.equal(current.connected,true);
  app.state.tab='home';app.emit();assert.equal(current.connected,false);assert.equal(frames.size,0);
});
