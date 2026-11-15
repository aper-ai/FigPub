/** Browser-studio DOM integration, not a real rendering-engine acceptance test.
 * Production handlers, geometry, validation, archives and SVG export run intact.
 * Image decoding, font loading, storage and window layout are explicitly emulated.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {join} from 'node:path';
import {webcrypto} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';
import {build} from 'esbuild';
import {Window} from 'happy-dom';
import {unzipVerified} from '../web/browser/archive.js';
import {resizeFigure} from '../web/modules/layout-engine.js';

const root=fileURLToPath(new URL('../',import.meta.url));
const bundled=await build({entryPoints:[join(root,'web/browser-studio.js')],bundle:true,write:false,format:'esm',platform:'browser',plugins:[{
  name:'emulated-browser-persistence',setup(builder){
    builder.onResolve({filter:/browser\/persistence\.js$/},()=>({path:'persistence',namespace:'emulated'}));
    builder.onLoad({filter:/.*/,namespace:'emulated'},()=>({contents:`
      export async function getWorkspaceStatus(){return {has_recovery:false,stored_originals:0};}
      export async function loadWorkspace(){return null;}
      export async function saveWorkspace(project){window.testSavedWorkspace=structuredClone(project);}
      export async function clearWorkspace(){window.testSavedWorkspace=null;}
    `}));
  },
}]});
const index=(await readFile(join(root,'web/browser-studio.html'),'utf8')).replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'');
async function until(predicate,message){for(let count=0;count<160;count++){if(predicate())return;await delay(10);}assert.fail(message);}

async function fixture(t){
  const window=new Window({url:'http://127.0.0.1:8765',settings:{disableCSSFileLoading:true,disableJavaScriptFileLoading:true,disableIframePageLoading:true,disableComputedStyleRendering:true}});
  t.after(()=>window.happyDOM.abort());
  window.structuredClone=structuredClone;Object.defineProperty(window,'crypto',{value:webcrypto});
  window.FW_STUDIO_FONTS={normal:'AA==',bold:'AA==',cjk:'AA=='};
  window.FontFace=class{async load(){return this;}};
  Object.defineProperty(window.document,'fonts',{value:{add(){}}});
  window.ResizeObserver=class{observe(){} disconnect(){}};
  window.Image=class{
    naturalWidth=600;naturalHeight=400;
    set src(value){this.value=value;if(value)queueMicrotask(()=>this.onload?.());}
    get src(){return this.value;}
  };
  window.confirm=()=>true;
  window.fetch=()=>{throw new Error('Unexpected network request in DOM integration');};
  const blobs=new Map(),downloads=[];
  window.URL.createObjectURL=blob=>{const url=`blob:fixture-${blobs.size}`;blobs.set(url,blob);return url;};
  window.URL.revokeObjectURL=url=>blobs.delete(url);
  window.HTMLAnchorElement.prototype.click=function(){if(this.download)downloads.push({name:this.download,blob:blobs.get(this.href)});};
  window.document.write(index);
  window.eval(`window.testStudioReady=(async()=>{${bundled.outputFiles[0].text}\n})();`);
  await window.testStudioReady;
  const doc=window.document,$=selector=>doc.querySelector(selector);
  const input=$('#image-files'),files=Array.from({length:4},(_,i)=>new window.File([
    `<svg xmlns="http://www.w3.org/2000/svg" width="600" height="400"><rect width="600" height="400" fill="${['#245a37','#3567a0','#ab7623','#834d8c'][i]}"/><circle cx="300" cy="200" r="80" fill="#eeeeee"/></svg>`,
  ],`SYNTHETIC_GEOMETRY_${i}.svg`,{type:'image/svg+xml'}));
  Object.defineProperty(input,'files',{configurable:true,value:files});
  input.dispatchEvent(new window.Event('change',{bubbles:true}));
  await until(()=>$('#panel-count').textContent==='4'&&!doc.body.classList.contains('busy'),'synthetic sources failed to import');
  $('[data-grid="2,2"]').click();
  function edit(selector,value,{change=false}={}){
    const control=$(selector);assert.ok(control,selector);control.focus();
    if(control.type==='checkbox')control.checked=Boolean(value);else control.value=String(value);
    control.dispatchEvent(new window.InputEvent('input',{bubbles:true,inputType:'insertText',data:String(value)}));
    if(change||control.tagName==='SELECT'||control.type==='checkbox')control.dispatchEvent(new window.Event('change',{bubbles:true}));
    return control;
  }
  async function click(action){const button=$(`[data-action="${action}"]`);assert.ok(button,action);button.click();await delay(1);await until(()=>!doc.body.classList.contains('busy'),`${action} stayed busy`);}
  async function save(){const count=downloads.length;await click('save');await until(()=>downloads.length>count,'project download missing');const bytes=new Uint8Array(await downloads.at(-1).blob.arrayBuffer()),archive=await unzipVerified(bytes);return JSON.parse(new TextDecoder().decode(archive.get('project.json')));}
  function geometry(){return [...doc.querySelectorAll('#studio-svg > [data-object]')].filter(group=>group.querySelector('image')).map(group=>({id:group.dataset.object,transform:group.getAttribute('transform'),width:Number(group.querySelector('clipPath rect').getAttribute('width')),height:Number(group.querySelector('clipPath rect').getAttribute('height'))}));}
  function select(i=0){doc.querySelectorAll('.asset-row')[i].click();}
  function key(selector,key){$(selector).dispatchEvent(new window.KeyboardEvent('keydown',{bubbles:true,cancelable:true,key}));}
  return {window,doc,$,downloads,edit,click,save,geometry,select,key};
}

test('a canvas image receives pointer selection and a committed drag after clearing the selection',async t=>{
  const {window,doc,$,geometry}=await fixture(t);
  window.DOMPoint=class {constructor(x,y){this.x=x;this.y=y;}matrixTransform(){return {x:this.x,y:this.y};}};
  window.SVGSVGElement.prototype.getScreenCTM=()=>({a:1,b:0,inverse:()=>({})});
  const pointer=(target,type,x,y)=>target.dispatchEvent(new window.PointerEvent(type,{bubbles:true,button:0,buttons:type==='pointerup'?0:1,pointerId:7,clientX:x,clientY:y,altKey:true}));
  pointer($('#studio-svg > rect'),'pointerdown',2,2);
  pointer(window,'pointerup',2,2);
  assert.equal($('#selection-info').textContent,'未选中对象');
  const panel=doc.querySelectorAll('#studio-svg > [data-object]')[2],before=geometry()[2];
  const coordinates=before.transform.match(/translate\(([-\d.]+) ([-\d.]+)\)/),x=Number(coordinates[1])+10,y=Number(coordinates[2])+10;
  pointer(panel.querySelector('image'),'pointerdown',x,y);
  assert.equal($('#selection-info').textContent,'已选择 1 个对象');
  assert.equal($('#studio-svg .selection-outline')?.parentElement.dataset.object,before.id);
  pointer(window,'pointermove',x+8,y+5);
  await until(()=>geometry()[2].transform!==before.transform,'canvas image did not move');
  pointer(window,'pointerup',x+8,y+5);
  assert.notEqual(geometry()[2].transform,before.transform);
  assert.equal($('#snap-guides').childElementCount,0);
});

test('browser preset and layout selects apply immediately and no parameter Apply button remains',async t=>{
  const {$,edit,click,save,select}=await fixture(t);
  assert.equal($('#canvas-form button[type="submit"]'),null);assert.equal($('#layout-form button[type="submit"]'),null);
  select();assert.equal($('#object-form button[type="submit"]'),null);
  edit('#journal-preset','nature-single',{change:true});
  assert.equal($('#studio-svg').getAttribute('viewBox'),'0 0 89 120');
  assert.equal($('.asset-label').textContent,'a');
  await click('undo');assert.equal($('#canvas-width').value,'180');assert.equal($('#journal-preset').value,'');
  await click('redo');assert.equal($('#journal-preset').value,'nature-single');
  edit('#layout-preset','hero-left',{change:true});
  const project=await save();assert.equal(project.settings.composition.layout.preset,'hero-left');
  assert.ok(project.panels[0].width_mm>project.panels[1].width_mm);
});

test('browser debounced edits retain input focus, coalesce history and keep redo on a no-op',async t=>{
  const {doc,$,edit,click}=await fixture(t);
  edit('#canvas-width','90');const field=edit('#canvas-width','150');
  await until(()=>$('#canvas-width').value==='150'&&$('#studio-svg').getAttribute('viewBox')==='0 0 150 120','width did not apply');
  assert.equal(doc.activeElement,field);
  edit('#canvas-width','150',{change:true});await click('undo');
  assert.equal($('#canvas-width').value,'180');assert.equal($('[data-action="redo"]').disabled,false);
  edit('#canvas-width','180',{change:true});assert.equal($('[data-action="redo"]').disabled,false);
  await click('redo');assert.equal($('#canvas-width').value,'150');
});

test('continuous canvas width and height edits compute one fit from their original geometry',async t=>{
  const {$,edit,save}=await fixture(t),before=await save();
  edit('#canvas-width','150');await until(()=>$('#studio-svg').getAttribute('viewBox')==='0 0 150 120','first dimension did not apply');
  edit('#canvas-height','90');await until(()=>$('#studio-svg').getAttribute('viewBox')==='0 0 150 90','second dimension did not apply');
  const after=await save(),expected=structuredClone(before);resizeFigure(expected,150,90,{mode:'fit'});
  assert.deepEqual(after.panels,expected.panels,'sequential editing must not accumulate fit shrink');
});

test('invalid numeric drafts leave committed geometry untouched and block save until Escape',async t=>{
  const {$,downloads,edit,click,geometry,key,save}=await fixture(t),before=geometry();
  edit('#canvas-width','');await delay(420);assert.deepEqual(geometry(),before);
  assert.equal($('#canvas-width').getAttribute('aria-invalid'),'true');
  edit('#canvas-width','2',{change:true});assert.deepEqual(geometry(),before);
  const count=downloads.length;await click('save');assert.equal(downloads.length,count);
  key('#canvas-width','Escape');assert.equal($('#canvas-width').value,'180');
  edit('#layout-form [name="columns"]','1.5',{change:true});assert.deepEqual(geometry(),before);
  key('#layout-form [name="columns"]','Escape');
  edit('#layout-form [name="margin_mm"]','60',{change:true});assert.deepEqual(geometry(),before);
  assert.match($('#layout-form .parameter-feedback').textContent,/未应用/);
  key('#layout-form [name="margin_mm"]','Escape');
  const project=await save();assert.equal(project.settings.composition.layout.margin_mm,6);
});

test('browser automatic text edits preserve the control, caret and IME draft',async t=>{
  const {window,doc,$,edit,select}=await fixture(t);select();
  const field=edit('#object-form [name="title"]','Alpha');field.setSelectionRange(2,2);
  await until(()=>$('#studio-svg').textContent.includes('Alpha'),'title did not apply');
  assert.equal($('#object-form [name="title"]'),field);assert.equal(doc.activeElement,field);assert.equal(field.selectionStart,2);
  field.dispatchEvent(new window.Event('compositionstart',{bubbles:true}));
  field.value='中文标题';field.setSelectionRange(4,4);
  field.dispatchEvent(new window.InputEvent('input',{bubbles:true,isComposing:true,data:'中文标题',inputType:'insertCompositionText'}));
  await delay(420);assert.ok($('#studio-svg').textContent.includes('Alpha'));assert.equal(field.value,'中文标题');
  field.dispatchEvent(new window.Event('compositionend',{bubbles:true}));
  await until(()=>$('#studio-svg').textContent.includes('中文标题'),'completed Chinese title did not apply');
  assert.equal(doc.activeElement,field);assert.equal(field.selectionStart,4);
});

test('browser unlocked aspect and expanded crop controls survive commits; numbering does not reflow',async t=>{
  const {window,$,edit,geometry,select,save}=await fixture(t);select();
  const before=geometry()[0];edit('#object-form [name="lock_aspect"]',false,{change:true});
  edit('#object-form [name="width_mm"]',before.width*.8,{change:true});
  assert.ok(Math.abs(geometry()[0].height-before.height)<.001);assert.equal($('#object-form [name="lock_aspect"]').checked,false);
  const details=$('#object-form details');details.open=true;details.dispatchEvent(new window.Event('toggle'));
  edit('#object-form [name="crop_width"]',300,{change:true});
  assert.equal($('#object-form details'),details);assert.equal(details.open,true);assert.equal($('#object-form [name="lock_aspect"]').checked,false);
  const positions=geometry();edit('#number-style','lower',{change:true});assert.deepEqual(geometry(),positions);
  edit('#number-order','source',{change:true});assert.deepEqual(geometry(),positions);
  edit('#journal-preset','nature-single',{change:true});edit('#number-style','upper',{change:true});
  edit('#canvas-width',100,{change:true});assert.equal($('#number-style').value,'upper');
  const project=await save();assert.equal(project.panels[0].crop.width,300);
});

test('relocking aspect within one property session uses the newly chosen dimensions',async t=>{
  const {$,edit,geometry,select}=await fixture(t);select();
  assert.equal($('#object-form [name="lock_aspect"]').checked,true);
  edit('#object-form [name="width_mm"]',60,{change:true});
  assert.ok(Math.abs(geometry()[0].height-40)<.001);
  edit('#object-form [name="lock_aspect"]',false,{change:true});
  edit('#object-form [name="height_mm"]',20,{change:true});
  edit('#object-form [name="lock_aspect"]',true,{change:true});
  edit('#object-form [name="width_mm"]',30,{change:true});
  assert.ok(Math.abs(geometry()[0].height-10)<.001,'relocking must retain the new 3:1 aspect, not the original 3:2 aspect');
  assert.equal($('#object-form [name="lock_aspect"]').checked,true);
});

test('toolbar and body undo/redo cancel invalid drafts without replaying a stale resize baseline',async t=>{
  const {window,doc,$,edit,save}=await fixture(t);
  edit('#canvas-width',150,{change:true});const stable=await save();
  edit('#canvas-width',120,{change:true});edit('#canvas-height',80,{change:true});await save();
  edit('#canvas-width','',{change:true});
  const toolbar=$('[data-action="fit"]');toolbar.focus();
  toolbar.dispatchEvent(new window.KeyboardEvent('keydown',{bubbles:true,cancelable:true,key:'z',ctrlKey:true}));
  assert.equal($('#studio-svg').getAttribute('viewBox'),'0 0 150 120');
  assert.equal($('#canvas-width').value,'150');
  edit('#canvas-height','',{change:true});doc.body.tabIndex=-1;doc.body.focus();
  doc.body.dispatchEvent(new window.KeyboardEvent('keydown',{bubbles:true,cancelable:true,key:'y',ctrlKey:true}));
  assert.equal($('#studio-svg').getAttribute('viewBox'),'0 0 120 80');
  assert.equal($('#canvas-height').value,'80');
  edit('#canvas-height','',{change:true});doc.body.focus();
  doc.body.dispatchEvent(new window.KeyboardEvent('keydown',{bubbles:true,cancelable:true,key:'z',ctrlKey:true}));
  assert.equal($('#studio-svg').getAttribute('viewBox'),'0 0 150 120');
  edit('#canvas-width',100,{change:true});
  const after=await save(),expected=structuredClone(stable);resizeFigure(expected,100,120,{mode:'fit'});
  assert.deepEqual(after.panels,expected.panels,'the next resize must start from the restored undo state');
});

test('beforeunload commits a valid pending edit and warns even when the edited canvas has no images',async t=>{
  const {window,$,edit,click,save}=await fixture(t);await save();
  const clean=new window.Event('beforeunload',{cancelable:true});window.dispatchEvent(clean);
  assert.equal(clean.defaultPrevented,false,'an already backed-up project does not need a warning');
  edit('#canvas-width',150);
  assert.equal($('#studio-svg').getAttribute('viewBox'),'0 0 180 120','the debounce has not yet elapsed');
  const pending=new window.Event('beforeunload',{cancelable:true});window.dispatchEvent(pending);
  assert.equal($('#studio-svg').getAttribute('viewBox'),'0 0 150 120');assert.equal(pending.defaultPrevented,true);
  assert.equal((await save()).figure.width_mm,150);
  await click('new');
  const empty=new window.Event('beforeunload',{cancelable:true});window.dispatchEvent(empty);
  assert.equal(empty.defaultPrevented,false);
  edit('#canvas-height',90);
  const editedEmpty=new window.Event('beforeunload',{cancelable:true});window.dispatchEvent(editedEmpty);
  assert.equal($('#studio-svg').getAttribute('viewBox'),'0 0 180 90');assert.equal(editedEmpty.defaultPrevented,true);
  const saved=await save();assert.equal(saved.panels.length,0);assert.equal(saved.figure.height_mm,90);
});

test('browser immediate project save and SVG export flush pending valid properties',async t=>{
  const {$,downloads,edit,click,save,select}=await fixture(t);select();
  edit('#object-form [name="title"]','Pending save');assert.equal((await save()).panels[0].title,'Pending save');
  edit('#canvas-width',150);assert.equal((await save()).figure.width_mm,150);
  select();edit('#object-form [name="title"]','Pending export');await click('export');
  edit('#export-format','svg',{change:true});const count=downloads.length;$('#export-confirm').click();
  await until(()=>downloads.length>count,'SVG export missing');const svg=await downloads.at(-1).blob.text();
  assert.match(svg,/Pending export/);assert.match(svg,/width="150mm"/);
});

test('browser pending input cannot leak into another selected panel or a new workspace',async t=>{
  const {$,edit,click,save,select}=await fixture(t);select();
  edit('#object-form [name="title"]','First panel only');select(1);await delay(420);
  assert.equal($('#object-form [name="title"]').value,'');
  const project=await save();assert.equal(project.panels[0].title,'First panel only');assert.equal(project.panels[1].title,'');
  edit('#object-form [name="title"]','Discard with project');await click('new');await delay(420);
  // Happy DOM's textContent setter represents numeric zero as an empty string.
  assert.equal(Number($('#panel-count').textContent),0);assert.equal((await save()).panels.length,0);
});
