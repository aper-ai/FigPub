import test from 'node:test';
import assert from 'node:assert/strict';
import {Window} from 'happy-dom';
import {ensureProject} from '../web/modules/domain.js';
import {state,captureRecipe,undo,redo} from '../web/modules/state.js';
import {addAssetsToComposition,arrangeComposition,applyJournalPreset,changeCompositionOption,renderCompositionControls,resizeComposition} from '../web/modules/composition.js';

function fixture(t,{panels=true}={}) {
  t.mock.method(globalThis,'setTimeout',()=>0);
  const window=new Window(),previous=Object.getOwnPropertyDescriptor(globalThis,'document');
  Object.defineProperty(globalThis,'document',{configurable:true,value:window.document});
  t.after(()=>{if(previous)Object.defineProperty(globalThis,'document',previous);else delete globalThis.document;});
  const project=ensureProject({id:'auto_apply_desktop',title:'Synthetic geometry',
    assets:Array.from({length:6},(_,index)=>({id:`source_${index}`,name:`Synthetic ${index}`,kind:'image',width_px:1200,height_px:800})),
    panels:[],annotations:[],measurements:[],settings:{composition:{resize_mode:'fit'}},figure:{width_mm:180,height_mm:140}});
  Object.assign(state,{project,workspaceStatus:null,history:[],future:[],selectedPanelId:null,selectedPanelIds:[],selectedAssetId:null,selectedAnnotationId:null,
    dirty:false,saveState:'saved',tab:'compose',compositionPresetDraft:null,mutationSequence:0});
  if(panels){addAssetsToComposition(project.assets.map(asset=>asset.id));arrangeComposition({columns:3,rows:2});}
  state.history=[];state.future=[];state.dirty=false;
  const control=(attribute,key,value,{type='number',checked=false}={})=>{
    const element=document.createElement(type==='select'?'select':'input');
    element.setAttribute(attribute,key);
    if(type==='select'){const option=document.createElement('option');option.value=String(value);element.append(option);}
    else element.type=type;
    element.value=String(value);element.checked=checked;
    return element;
  };
  const controls=()=>{const div=document.createElement('div');div.innerHTML=renderCompositionControls(project);return div;};
  return {project,control,controls};
}

const geometry=project=>project.panels.map(panel=>[panel.x_mm,panel.y_mm,panel.width_mm,panel.height_mm]);

test('journal choice immediately adapts dimensions, numbering and fonts in one reversible edit',t=>{
  const {project,controls}=fixture(t),before=captureRecipe(),crops=project.panels.map(panel=>structuredClone(panel.crop));
  const select=controls().querySelector('#journal-preset');select.value='nature-single';
  assert.equal(changeCompositionOption(select),true);
  assert.equal(project.figure.width_mm,89);
  assert.equal(project.settings.composition.journal_preset_id,'nature-single');
  assert.equal(project.settings.composition.journal_preset_snapshot.id,'nature-single');
  assert.equal(project.settings.composition.numbering.style,'lower');
  assert.ok(project.panels.every(panel=>panel.label_font_size_pt===8));
  assert.deepEqual(project.panels.map(panel=>panel.label),['a','b','c','d','e','f']);
  assert.deepEqual(project.panels.map(panel=>panel.crop),crops);
  assert.equal(state.history.length,1);
  const after=captureRecipe();undo();assert.deepEqual(captureRecipe(),before);redo();assert.deepEqual(captureRecipe(),after);
});

test('layout parameter changes update geometry and settings together without an Apply action',t=>{
  const {project,control}=fixture(t),before=captureRecipe(),originals=project.assets,crops=project.panels.map(panel=>structuredClone(panel.crop));
  changeCompositionOption(control('data-layout-option','gap_x_mm',11.125));
  assert.equal(project.settings.composition.layout.gap_x_mm,11.125);
  assert.notDeepEqual(geometry(project),geometry(before));
  assert.deepEqual(project.panels.map(panel=>panel.crop),crops);
  assert.equal(project.assets,originals);assert.equal(state.history.length,1);
  const after=captureRecipe();undo();assert.deepEqual(captureRecipe(),before);redo();assert.deepEqual(captureRecipe(),after);
});

test('empty canvas keeps chosen layout and uses it for the first import',t=>{
  const {project,control}=fixture(t,{panels:false});
  changeCompositionOption(control('data-layout-option','columns',3));
  changeCompositionOption(control('data-layout-option','gap_x_mm',8));
  assert.equal(project.panels.length,0);assert.equal(project.settings.composition.layout.columns,3);
  addAssetsToComposition(project.assets.map(asset=>asset.id));
  assert.equal(project.panels.length,6);assert.equal(project.settings.composition.layout.gap_x_mm,8);
  const rows=new Map();for(const panel of project.panels)rows.set(panel.y_mm,(rows.get(panel.y_mm)||0)+1);
  assert.deepEqual([...rows.values()],[3,3]);
});

test('invalid values and impossible capacity leave geometry, history and input at the committed value',t=>{
  const {project,control}=fixture(t),before=captureRecipe();
  state.future=[{recipe:before,label:'Existing redo'}];
  for(const [key,value] of [['columns',''],['rows',1.5],['columns',-1],['gap_x_mm',1001],['columns',2],['margin_mm',1000]]){
    const element=control('data-layout-option',key,value);
    assert.throws(()=>changeCompositionOption(element));
    assert.deepEqual(captureRecipe(),before);assert.equal(state.history.length,0);assert.equal(state.future.length,1);
    assert.equal(element.value,String(project.settings.composition.layout[key]));
  }
});

test('grouped panels block automatic reflow but allow independent numbering and resize behavior edits',t=>{
  const {project,control,controls}=fixture(t);
  project.panels[0].group_id='group';project.panels[1].group_id='group';
  const before=captureRecipe(),layoutControl=control('data-layout-option','gap_x_mm',12);
  assert.throws(()=>changeCompositionOption(layoutControl),/解组/);
  assert.deepEqual(captureRecipe(),before);assert.equal(state.history.length,0);assert.equal(layoutControl.value,'3');
  const numbering=controls().querySelector('#numbering-style');numbering.value='numeric';changeCompositionOption(numbering);
  assert.deepEqual(geometry(project),geometry(before));assert.deepEqual(project.panels.map(panel=>panel.label),['1','2','3','4','5','6']);
  const mode=controls().querySelector('#composition-resize-mode');mode.value='canvas';changeCompositionOption(mode);
  assert.deepEqual(geometry(project),geometry(before));assert.equal(project.settings.composition.resize_mode,'canvas');
});

test('unchanged parameters consume no undo and keep redo intact',t=>{
  const {project,control,controls}=fixture(t),before=captureRecipe();state.future=[{recipe:before,label:'Existing redo'}];
  changeCompositionOption(control('data-layout-option','gap_x_mm',3));
  changeCompositionOption(controls().querySelector('#numbering-style'));
  changeCompositionOption(controls().querySelector('#composition-resize-mode'));
  resizeComposition(project.figure.width_mm,project.figure.height_mm);
  assert.deepEqual(captureRecipe(),before);assert.equal(state.history.length,0);assert.equal(state.future.length,1);
});

test('failed preset choice restores the selected preset and all geometry without retaining a draft',t=>{
  const {project,controls}=fixture(t);applyJournalPreset('nature-double');
  project.settings.composition.resize_mode='canvas';state.history=[];
  const before=captureRecipe(),select=controls().querySelector('#journal-preset');select.value='nature-single';
  state.compositionPresetDraft='nature-single';
  assert.throws(()=>changeCompositionOption(select),/裁掉/);
  assert.deepEqual(captureRecipe(),before);assert.equal(state.history.length,0);
  assert.equal(select.value,'nature-double');assert.equal(state.compositionPresetDraft,null);
});

test('repeated preset value preserves custom numbering and creates no extra resize',t=>{
  const {project}=fixture(t);applyJournalPreset('nature-single');
  project.settings.composition.numbering.style='numeric';project.panels.forEach((panel,index)=>{panel.label=String(index+1);panel.label_font_size_pt=7;});
  const before=captureRecipe(),history=state.history.length;applyJournalPreset('nature-single');
  assert.deepEqual(captureRecipe(),before);assert.equal(state.history.length,history);
});

test('custom canvas selection clears the preset policy while keeping content and dimensions',t=>{
  const {project,controls}=fixture(t);applyJournalPreset('nature-single');
  const before=captureRecipe(),select=controls().querySelector('#journal-preset');select.value='';
  changeCompositionOption(select);
  assert.equal(project.settings.composition.journal_preset_id,null);assert.equal(project.settings.composition.journal_preset_snapshot,null);
  assert.deepEqual(project.figure,before.figure);assert.deepEqual(project.panels,before.panels);
  undo();assert.deepEqual(captureRecipe(),before);
});

test('controls show the committed selection and contain no secondary Apply buttons',t=>{
  const {controls}=fixture(t);state.compositionPresetDraft='nature-single';
  const rendered=controls();assert.equal(rendered.querySelector('#journal-preset').value,'');
  assert.equal(rendered.querySelectorAll('[data-action="apply-layout"], [data-action="apply-journal-preset"]').length,0);
  assert.equal(rendered.querySelector('#layout-gap_x_mm').step,'any');
});
