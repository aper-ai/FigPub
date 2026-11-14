import assert from 'node:assert/strict';
import test from 'node:test';
import {Window} from 'happy-dom';
import {
  retainedSegments,sourceXInPanel,effectivePpi,normalizeRoi,figureFont,
  panelFromAsset,ensureProject,scaleBarDraft,scaleBarGeometry,updateScaleBar,isAnalysisChartType,
} from '../web/modules/domain.js';
import {
  state,captureRecipe,restoreRecipe,mutate,undo,redo,commitGesture,
} from '../web/modules/state.js';
import {renderSourceCanvas} from '../web/modules/source-view.js';

function sourceAsset() {return {id:'source',width_px:600,height_px:300,kind:'image'};}
function gelPanel() {
  return {
    id:'panel',asset_id:'source',x_mm:10,y_mm:10,width_mm:100,height_mm:50,
    crop:{x:0,y:0,width:600,height:300},
    gel:{lane_centers:[100,300,500],lane_names:['A','B','C'],hidden_lanes:[1]},
  };
}
function projectFixture() {
  return ensureProject({
    id:'project',title:'Before',revision:9,assets:[sourceAsset()],panels:[gelPanel()],
    annotations:[],measurements:[{id:'analysis',result:{objects:[{id:1,contour:[[1,2],[3,4]]}]}}],
    audit:[{action:'original_import'}],figure:{width_mm:180,height_mm:120},settings:{},
  });
}
function installState(t) {
  // Mutations schedule autosave in production. Unit tests never make an HTTP request.
  t.mock.method(globalThis,'setTimeout',()=>0);
  state.project=projectFixture();state.history=[];state.future=[];state.mutationSequence=0;
  state.dirty=false;state.saveState='saved';
  return state.project;
}
function sourceViewState(t) {
  const project=installState(t),previous=Object.getOwnPropertyDescriptor(globalThis,'location');
  Object.defineProperty(globalThis,'location',{configurable:true,value:{origin:'http://localhost'}});
  t.after(()=>{if(previous)Object.defineProperty(globalThis,'location',previous);else delete globalThis.location;});
  state.selectedAssetId='source';state.selectedPanelId=null;state.sourceRoi=null;state.tab='source';
  return project;
}

test('ROI dragging beyond any source edge clips both endpoints in source pixels',()=>{
  const asset=sourceAsset();
  assert.deepEqual(normalizeRoi({x:100,y:100},{x:-100,y:-50},asset),{x:0,y:0,width:100,height:100});
  assert.deepEqual(normalizeRoi({x:500,y:200},{x:900,y:500},asset),{x:500,y:200,width:100,height:100});
  assert.deepEqual(normalizeRoi({x:150,y:90},{x:30,y:20},asset),{x:30,y:20,width:120,height:70});
});

test('WB source overlays keep the same ROI numbers as the global measurement table',t=>{
  const project=sourceViewState(t);
  project.settings.analysis_rois=[
    {id:'other-roi',asset_id:'other',x:1,y:1,width:10,height:10},
    {id:'visible-roi',asset_id:'source',x:5,y:5,width:20,height:10},
  ];
  const markup=renderSourceCanvas();
  assert.match(markup,/>R2<\/text>/);assert.doesNotMatch(markup,/>R1<\/text>/);
});

test('microscopy ROI actions run regional analysis instead of creating a WB region',t=>{
  sourceViewState(t);state.tab='analysis';state.analysisKind='microscopy';
  state.sourceRoi={x:10,y:10,width:50,height:40};
  const window=new Window();t.after(()=>window.happyDOM.abort());
  window.document.body.innerHTML=renderSourceCanvas({analysis:true});
  const run=window.document.querySelector('button[data-action="run-analysis"]');
  assert.ok(run);assert.equal(run.textContent,'运行区域分析');assert.equal(run.disabled,false);
  assert.equal(window.document.querySelector('[data-action="add-roi"]'),null);
});

test('disclosed lane removal contains only kept source intervals and a 2.5% physical gap',()=>{
  const segments=retainedSegments(gelPanel(),sourceAsset());
  assert.equal(segments.length,2);
  assert.deepEqual(segments.map(segment=>segment.crop),[
    {x:0,y:0,width:200,height:300},{x:400,y:0,width:200,height:300},
  ]);
  assert.ok(Math.abs(segments[0].fraction-.4875)<1e-12);
  assert.ok(Math.abs(segments[1].offset-segments[0].fraction-.025)<1e-12);
  assert.ok(Math.abs(segments.at(-1).offset+segments.at(-1).fraction-1)<1e-12);
});

test('source-linked annotation mapping omits removed pixels and follows horizontal flips',()=>{
  const panel=gelPanel(),asset=sourceAsset();
  assert.equal(sourceXInPanel(panel,asset,300),null);
  const left=sourceXInPanel(panel,asset,100),right=sourceXInPanel(panel,asset,500);
  panel.flip_h=true;
  assert.equal(sourceXInPanel(panel,asset,100),1-left);
  assert.equal(sourceXInPanel(panel,asset,500),1-right);
  assert.equal(sourceXInPanel(panel,asset,300),null);
});

test('effective PPI uses retained original pixels and actual physical segment widths',()=>{
  const panel=gelPanel(),asset=sourceAsset();
  assert.ok(Math.abs(effectivePpi(panel,asset)-200/48.75*25.4)<1e-10);
  assert.equal(effectivePpi(panel,{...asset,kind:'svg'}),null);
  assert.equal(effectivePpi(panel,{...asset,kind:'pdf'}),null);
});

test('font choice applies to a complete mixed-script text run',()=>{
  assert.equal(figureFont('蛋白 ABC'),'Noto Sans CJK SC');
  assert.equal(figureFont('ABC µm'),'DejaVu Sans');
  assert.equal(figureFont('日本語 42'),'Noto Sans CJK SC');
  assert.equal(figureFont('한국어'),'Noto Sans CJK SC');
});

test('new panel preserves its crop aspect ratio while fitting the physical canvas',()=>{
  const project=projectFixture(),asset={...sourceAsset(),width_px:1000,height_px:10000};
  const panel=panelFromAsset(project,asset);
  assert.ok(Math.abs(panel.width_mm/panel.height_mm-.1)<.001);
  assert.ok(panel.x_mm>=0&&panel.y_mm>=0);
  assert.ok(panel.x_mm+panel.width_mm<=project.figure.width_mm);
  assert.ok(panel.y_mm+panel.height_mm<=project.figure.height_mm);
});

test('scale bars use calibrated source pixels through crop and physical panel resizing',()=>{
  const asset=sourceAsset(),panel=gelPanel(),bar={um_per_px:.5,length_um:50,color:'#ffffff'};
  panel.gel.hidden_lanes=[];
  let geometry=scaleBarGeometry(panel,asset,bar);
  assert.equal(geometry.valid,true);assert.equal(geometry.source_length_px,100);
  assert.ok(Math.abs(geometry.length_mm-100/600*100)<1e-10);
  panel.crop={x:150,y:0,width:300,height:300};
  geometry=scaleBarGeometry(panel,asset,bar);
  assert.ok(Math.abs(geometry.length_mm-100/300*100)<1e-10);
  panel.width_mm=200;
  assert.ok(Math.abs(scaleBarGeometry(panel,asset,bar).length_mm-100/300*200)<1e-10);
});

test('unknown or invalid calibration cannot become an active scale bar',()=>{
  const asset=sourceAsset(),panel=gelPanel();
  assert.deepEqual(scaleBarDraft(panel),{um_per_px:null,length_um:null,color:'#ffffff'});
  for(const value of [null,0,-1,Infinity,NaN,'0.5']){
    const validation=updateScaleBar(panel,asset,{um_per_px:value,length_um:50},true);
    assert.equal(validation.valid,false);assert.equal('scale_bar' in panel,false);
  }
  assert.equal(scaleBarGeometry(panel,{...asset,kind:'svg'},{um_per_px:1,length_um:50}).valid,false);
  assert.equal(scaleBarGeometry(panel,asset,{um_per_px:.5,length_um:271}).valid,false);
  assert.equal(scaleBarGeometry(panel,asset,{um_per_px:.5,length_um:270}).valid,true);
});

test('scale bar visibility preserves its calibration draft without exporting a disabled bar',()=>{
  const asset=sourceAsset(),panel=gelPanel();
  updateScaleBar(panel,asset,{um_per_px:.5});
  updateScaleBar(panel,asset,{length_um:50,color:'#111111'});
  assert.equal('scale_bar' in panel,false);
  updateScaleBar(panel,asset,{},true);
  assert.deepEqual(panel.scale_bar,{um_per_px:.5,length_um:50,color:'#111111'});
  updateScaleBar(panel,asset,{},false);
  assert.equal('scale_bar' in panel,false);assert.equal(scaleBarDraft(panel).um_per_px,.5);
  updateScaleBar(panel,asset,{},true);updateScaleBar(panel,asset,{length_um:null});
  assert.equal('scale_bar' in panel,false);assert.equal(scaleBarDraft(panel).um_per_px,.5);
});

test('chart selection accepts exactly the three supported analysis chart types',()=>{
  for(const type of ['scatter','bar','box'])assert.equal(isAnalysisChartType(type),true);
  for(const type of ['pie','heatmap','',null,undefined])assert.equal(isAnalysisChartType(type),false);
});

test('recipe snapshots exclude immutable assets, analysis results, and audit history',t=>{
  const project=installState(t),assets=project.assets,measurements=project.measurements,audit=project.audit;
  const recipe=captureRecipe();
  assert.deepEqual(Object.keys(recipe).sort(),['annotations','figure','panels','settings','title']);
  assert.notEqual(recipe.panels,project.panels);
  project.title='Changed';project.revision=12;restoreRecipe(recipe);
  assert.equal(project.title,'Before');assert.equal(project.revision,12);
  assert.equal(project.assets,assets);assert.equal(project.measurements,measurements);assert.equal(project.audit,audit);
});

test('undo and redo preserve the latest revision and subsequently appended analysis results',t=>{
  const project=installState(t),assets=project.assets,measurements=project.measurements;
  mutate(current=>{current.title='After';},'Rename');
  measurements.push({id:'later_result',result:{rows:[{value:42}]}});project.revision=10;
  undo();assert.equal(state.project.title,'Before');assert.equal(state.project.revision,10);
  assert.equal(state.project.assets,assets);assert.equal(state.project.measurements,measurements);
  assert.equal(state.project.measurements.length,2);
  redo();assert.equal(state.project.title,'After');assert.equal(state.project.revision,10);
  assert.equal(state.project.measurements,measurements);
});

test('a drag commits one recipe history entry and a click without movement commits none',t=>{
  installState(t);const before=captureRecipe();commitGesture(before);assert.equal(state.history.length,0);
  state.project.panels[0].x_mm+=5;commitGesture(before,'Move panel');
  assert.equal(state.history.length,1);assert.ok('recipe' in state.history[0]);
  assert.ok(!('project' in state.history[0]));undo();assert.equal(state.project.panels[0].x_mm,10);
});
