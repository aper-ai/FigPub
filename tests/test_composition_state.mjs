import test from 'node:test';
import assert from 'node:assert/strict';
import {ensureProject} from '../web/modules/domain.js';
import {state,mutate,captureRecipe,selectedPanels,selectPanel,undo,redo} from '../web/modules/state.js';
import {addAssetsToComposition,arrangeComposition,groupComposition,duplicateComposition,alignComposition,resizeComposition} from '../web/modules/composition.js';

function install(t) {
  t.mock.method(globalThis,'setTimeout',()=>0);
  state.project=ensureProject({id:'composition_test',title:'Synthetic',assets:Array.from({length:6},(_,index)=>({id:`asset_${index}`,kind:'image',name:`Synthetic ${index}`,width_px:1200+index*80,height_px:800})),panels:[],annotations:[],measurements:[],audit:[],settings:{},figure:{width_mm:180,height_mm:140}});
  Object.assign(state,{history:[],future:[],selectedPanelId:null,selectedPanelIds:[],selectedAssetId:null,selectedAnnotationId:null,dirty:false,saveState:'saved',tab:'compose'});
  return state.project;
}

test('a failing composition command restores its recipe without consuming undo or immutable sources',t=>{
  const project=install(t),assets=project.assets,before=captureRecipe();
  state.future=[{recipe:before,label:'Existing redo'}];
  assert.throws(()=>mutate(current=>{current.title='Partial';current.figure.width_mm=89;throw new Error('Invalid layout');}),/Invalid layout/);
  assert.deepEqual(captureRecipe(),before);assert.equal(project.assets,assets);
  assert.equal(state.history.length,0);assert.equal(state.future.length,1);assert.equal(state.dirty,false);
});

test('a whole import batch is one undo step and manual labels survive subsequent layout',t=>{
  const project=install(t);addAssetsToComposition(project.assets.map(asset=>asset.id));
  assert.equal(project.panels.length,6);assert.equal(state.history.length,1);assert.equal(selectedPanels().length,6);
  assert.equal(new Set(project.panels.map(panel=>panel.label)).size,6);
  mutate(current=>{current.panels[2].label='Control';current.panels[2].label_mode='manual';});
  arrangeComposition({columns:3,rows:2});assert.equal(project.panels[2].label,'Control');
  const before=captureRecipe(),history=state.history.length;
  assert.throws(()=>arrangeComposition({columns:2,rows:2}));assert.deepEqual(captureRecipe(),before);assert.equal(state.history.length,history);
  undo();assert.equal(project.panels[2].label,'Control');redo();assert.equal(project.settings.composition.layout.columns,3);
});

test('group selection, duplication and canvas adaptation preserve membership and relative placement',t=>{
  const project=install(t);addAssetsToComposition(project.assets.map(asset=>asset.id));
  const [first,second]=project.panels;selectPanel(first.id);selectPanel(second.id,{toggle:true});groupComposition();
  const group=first.group_id;assert.equal(second.group_id,group);assert.equal(selectedPanels().length,2);
  selectPanel(project.panels[5].id);selectPanel(first.id);assert.equal(selectedPanels().length,2);
  const offset={x:second.x_mm-first.x_mm,y:second.y_mm-first.y_mm};alignComposition('center');
  assert.ok(Math.abs(second.x_mm-first.x_mm-offset.x)<.002);assert.ok(Math.abs(second.y_mm-first.y_mm-offset.y)<.002);
  const before=captureRecipe(),history=state.history.length;assert.throws(()=>arrangeComposition(),/解组/);
  assert.deepEqual(captureRecipe(),before);assert.equal(state.history.length,history);
  duplicateComposition();const copies=selectedPanels();assert.equal(copies.length,2);assert.notEqual(copies[0].group_id,group);assert.equal(copies[0].group_id,copies[1].group_id);
  assert.equal(new Set(project.panels.map(panel=>panel.label)).size,8);
  const crops=project.panels.map(panel=>structuredClone(panel.crop));resizeComposition(89,140,'fit');
  assert.equal(project.figure.width_mm,89);assert.deepEqual(project.panels.map(panel=>panel.crop),crops);
  undo();assert.equal(project.figure.width_mm,180);assert.equal(project.panels.length,8);
  undo();assert.equal(project.panels.length,6);assert.equal(project.panels[0].group_id,group);
});

test('a cancelled canvas-only resize keeps its dimensions and undo history',t=>{
  const project=install(t);addAssetsToComposition(project.assets.map(asset=>asset.id));
  const before=captureRecipe(),history=state.history.length;
  assert.throws(()=>resizeComposition(20,20,'canvas'));
  assert.deepEqual(captureRecipe(),before);assert.equal(state.history.length,history);
});
