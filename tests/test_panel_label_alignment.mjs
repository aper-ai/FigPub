import test from 'node:test';
import assert from 'node:assert/strict';
import {deflateSync} from 'node:zlib';
import {Window} from 'happy-dom';
import {applyPanelLayout,planPanelLayout,panelLabelPosition,panelBounds,resizeFigure} from '../web/modules/layout-engine.js';
import {panelContentMarkup,renderComposer} from '../web/modules/composer-view.js';
import {state} from '../web/modules/state.js';
import {newProject,panelForAsset,validateBrowserProject} from '../web/browser/model.js';
import {figureMarkup,rasterCanvas} from '../web/browser/render.js';
import {crc32,encodeProject,decodeProject,sha256} from '../web/browser/archive.js';

const near=(a,b,tolerance=2e-5)=>assert.ok(Math.abs(a-b)<tolerance,`${a} != ${b}`);
function browserLocation(t) {
  const descriptor=Object.getOwnPropertyDescriptor(globalThis,'location');
  Object.defineProperty(globalThis,'location',{value:new URL('http://127.0.0.1:8765'),configurable:true,writable:true});
  t.after(()=>{if(descriptor)Object.defineProperty(globalThis,'location',descriptor);else delete globalThis.location;});
}
function chunk(type,data) {
  const result=Buffer.alloc(data.length+12);result.writeUInt32BE(data.length);result.write(type,4);result.set(data,8);result.writeUInt32BE(crc32(result.subarray(4,8+data.length)),8+data.length);return result;
}
function syntheticPng(width,height) {
  const header=Buffer.alloc(13);header.writeUInt32BE(width);header.writeUInt32BE(height,4);header[8]=8;header[9]=2;
  const pixels=Buffer.alloc((width*3+1)*height);
  for(let y=0;y<height;y++)for(let x=0;x<width;x++)pixels.set([(x*7)%256,(y*13)%256,197],y*(width*3+1)+1+x*3);
  return new Uint8Array(Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),chunk('IHDR',header),chunk('IDAT',deflateSync(pixels)),chunk('IEND',Buffer.alloc(0))]));
}
async function fixture() {
  const project=newProject(),sources=new Map();
  for(const [index,[width,height]] of [[32,80],[96,32],[120,24],[48,64]].entries()) {
    const bytes=syntheticPng(width,height),id=await sha256(bytes),asset={id,sha256:id,name:`Synthetic_${index}.png`,kind:'image',width_px:width,height_px:height,bit_depth:8,dtype:'uint8',channels:3,frames:1,metadata:{},preview_url:`blob:synthetic-${index}`};
    project.assets.push(asset);project.panels.push({...panelForAsset(asset),id:`p${index}`,label_font_size_pt:[8,11,10,13][index]});
    sources.set(id,{asset,bytes,mime:'image/png',extension:'.png',url:asset.preview_url});
  }
  return {project,sources};
}
function aligned(project) {
  const anchors=project.panels.map(panel=>panelLabelPosition(panel));
  near(anchors[0].x_mm,anchors[2].x_mm);near(anchors[1].x_mm,anchors[3].x_mm);
  near(anchors[0].y_mm,anchors[1].y_mm);near(anchors[2].y_mm,anchors[3].y_mm);
  return anchors;
}

test('mixed narrow and wide originals use column anchors and row baselines without changing any source recipe',async()=>{
  const {project,sources}=await fixture(),before=structuredClone(project),originals=[...sources.values()].map(source=>source.bytes.slice());
  const planned=planPanelLayout(project,{columns:2,rows:2});assert.deepEqual(project,before);
  const plan=applyPanelLayout(project,{columns:2,rows:2}),anchors=aligned(project);
  assert.deepEqual(plan,planned);assert.ok(project.panels[0].x_mm>project.panels[2].x_mm+5);
  for(const [index,panel] of project.panels.entries()) {
    near(anchors[index].x_mm,plan.cells[index].x_mm);
    near(panel.width_mm/panel.height_mm,panel.crop.width/panel.crop.height);
    assert.deepEqual(panel.crop,before.panels[index].crop);assert.deepEqual(panel.display,before.panels[index].display);
    assert.equal(panel.rotation,before.panels[index].rotation);assert.equal(panel.flip_h,before.panels[index].flip_h);
    assert.deepEqual(sources.get(panel.asset_id).bytes,originals[index]);
    const bounds=panelBounds(panel),cell=plan.cells[index];assert.ok(bounds.left>=cell.x_mm-1e-5&&bounds.right<=cell.x_mm+cell.width_mm+1e-5);assert.ok(bounds.top>=cell.y_mm-1e-5&&bounds.bottom<=cell.y_mm+cell.height_mm+1e-5);
  }
  assert.deepEqual(project.panels.map(panel=>panel.label),['A','B','C','D']);
  const once=structuredClone(project);applyPanelLayout(project,{columns:2,rows:2});assert.deepEqual(project,once);
});

test('caption anchors follow movement and fitting while keeping physical font sizes and aligned reading order',async()=>{
  const {project}=await fixture();applyPanelLayout(project,{columns:2,rows:2});const fonts=project.panels.map(panel=>panel.label_font_size_pt),initial=aligned(project);
  for(const panel of project.panels){panel.x_mm+=4;panel.y_mm+=3;}
  project.panels.forEach((panel,index)=>{const anchor=panelLabelPosition(panel);near(anchor.x_mm,initial[index].x_mm+4);near(anchor.y_mm,initial[index].y_mm+3);});
  resizeFigure(project,100,90);aligned(project);assert.deepEqual(project.panels.map(panel=>panel.label_font_size_pt),fonts);
  resizeFigure(project,160,140,{mode:'reflow'});aligned(project);assert.deepEqual(project.panels.map(panel=>panel.label),['A','B','C','D']);
  assert.deepEqual(project.panels.map(panel=>panel.label_font_size_pt),fonts);
});

test('both SVG previews draw relocated labels at the same cells and keep their original image payload',async t=>{
  browserLocation(t);
  const {project,sources}=await fixture();const plan=applyPanelLayout(project,{columns:2,rows:2});
  const window=new Window();t.after(()=>window.happyDOM.abort());
  window.document.body.innerHTML=figureMarkup(project,sources,{editor:true,embedded:true});
  for(const [index,panel] of project.panels.entries()) {
    const browser=window.document.querySelector(`[data-object="${panel.id}"]`),text=browser.querySelector('text');
    const x=panel.x_mm+Number(text.getAttribute('x')),y=panel.y_mm+Number(text.getAttribute('y'));
    near(x,plan.cells[index].x_mm);near(y,panelLabelPosition(panel).y_mm);
    const desktop=window.document.createElement('div');desktop.innerHTML=`<svg>${panelContentMarkup(panel,project.assets[index])}</svg>`;
    const nativeText=desktop.querySelector('.panel-label');near(Number(nativeText.getAttribute('x')),Number(text.getAttribute('x')));near(Number(nativeText.getAttribute('y')),Number(text.getAttribute('y')));
    const encoded=browser.querySelector('image').getAttribute('href').split(',')[1];assert.deepEqual(new Uint8Array(Buffer.from(encoded,'base64')),sources.get(panel.asset_id).bytes);
  }
});

test('browser canvas export places every label on the same grid baselines used by SVG',async t=>{
  const {project,sources}=await fixture();const plan=applyPanelLayout(project,{columns:2,rows:2});
  const labels=[],draws=[],context={fillRect(){},scale(){},save(){},restore(){},translate(){},rotate(){},drawImage(...args){draws.push(args);},fillText(text,x,y){if(text)labels.push({text,x,y});}};
  t.mock.method(globalThis,'fetch',async()=>({ok:true,arrayBuffer:async()=>new ArrayBuffer(8)}));
  for(const [key,value] of Object.entries({document:{createElement:()=>({getContext:()=>context}),fonts:{add(){}}},FontFace:class {async load(){return this;}},Image:class {set src(value){if(value)queueMicrotask(()=>this.onload());}}})) {
    const descriptor=Object.getOwnPropertyDescriptor(globalThis,key);Object.defineProperty(globalThis,key,{value,configurable:true,writable:true});t.after(()=>{if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];});
  }
  await rasterCanvas(project,sources,150);assert.equal(labels.length,4);assert.equal(draws.length,4);
  labels.forEach((label,index)=>{near(label.x,plan.cells[index].x_mm);near(label.y,panelLabelPosition(project.panels[index]).y_mm);assert.equal(label.text,['A','B','C','D'][index]);assert.deepEqual(draws[index].slice(1,5),[0,0,project.assets[index].width_px,project.assets[index].height_px]);});
  near(labels[0].x,labels[2].x);near(labels[0].y,labels[1].y);
});

test('portable project save and reopen preserve anchors and every synthetic original byte',async()=>{
  const {project,sources}=await fixture();applyPanelLayout(project,{columns:2,rows:2});
  const archived=await encodeProject(project,sources),restored=await decodeProject(new Uint8Array(await archived.arrayBuffer()));
  validateBrowserProject(restored.project);aligned(restored.project);
  assert.deepEqual(restored.project.panels,project.panels);
  for(const original of restored.originals)assert.deepEqual(original.bytes,sources.get(original.asset.id).bytes);
});

test('legacy projects keep existing captions, hidden labels stay hidden, and malformed anchors fail atomically',async t=>{
  browserLocation(t);
  const {project,sources}=await fixture(),panel=project.panels[0];panel.label='Legacy';
  assert.deepEqual(panelLabelPosition(panel),{x_mm:panel.x_mm,y_mm:panel.y_mm-1.8});
  const before=structuredClone(project);validateBrowserProject(project);assert.deepEqual(project.panels,before.panels);
  for(const anchor of [{x_ratio:NaN,y_ratio:0},{x_ratio:0,y_ratio:Infinity},{x_ratio:'1',y_ratio:0},[],{}]) {
    const invalid=structuredClone(project);invalid.panels[0].label_anchor=anchor;const unchanged=structuredClone(invalid);
    assert.throws(()=>applyPanelLayout(invalid),/锚点/);assert.deepEqual(invalid,unchanged);assert.throws(()=>validateBrowserProject(invalid),/锚点/);
  }
  panel.label_mode='none';panel.label_anchor={x_ratio:-.5,y_ratio:-.2};
  const window=new Window();t.after(()=>window.happyDOM.abort());
  window.document.body.innerHTML=figureMarkup(project,sources);assert.ok(!window.document.body.textContent.includes('Legacy'));
  assert.ok(!panelContentMarkup(panel,project.assets[0]).includes('class="panel-label"'));
});

test('desktop snap control shows its state as text and an accessible pressed state',async t=>{
  browserLocation(t);
  const {project}=await fixture(),previous={project:state.project,snap:state.snap,selectedPanelIds:state.selectedPanelIds,selectedPanelId:state.selectedPanelId};
  try {
    Object.assign(state,{project,selectedPanelIds:[],selectedPanelId:null,snap:true});let html=renderComposer();assert.match(html,/data-action="toggle-snap" aria-pressed="true"/);assert.ok(html.includes('对齐吸附</button>'));
    state.snap=false;html=renderComposer();assert.match(html,/data-action="toggle-snap" aria-pressed="false"/);assert.ok(html.includes('吸附已关闭</button>'));
  } finally {Object.assign(state,previous);}
});
