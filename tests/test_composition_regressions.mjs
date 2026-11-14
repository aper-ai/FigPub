import assert from 'node:assert/strict';
import test from 'node:test';
import {ensureProject} from '../web/modules/domain.js';
import {
  state,selectPanel,selectedPanels,mutate,undo,replaceProject,captureRecipe,
  restoreRecipe,persistProject,commitGesture,emit,
} from '../web/modules/state.js';
import {
  groupComposition,applyJournalPreset,renderCompositionControls,
} from '../web/modules/composition.js';
import {panelBounds} from '../web/modules/layout-engine.js';
import {renderComposer} from '../web/modules/composer-view.js';
import {bindInteractions} from '../web/modules/interaction.js';
import {bindGestures} from '../web/modules/gestures.js';

function temporaryGlobal(t,name,value) {
  const original=Object.getOwnPropertyDescriptor(globalThis,name);
  Object.defineProperty(globalThis,name,{value,writable:true,configurable:true});
  t.after(()=>{if(original)Object.defineProperty(globalThis,name,original);else delete globalThis[name];});
}

function fixture(t) {
  t.mock.method(globalThis,'setTimeout',()=>0);
  const events=new Map(),toast={children:[],append(){}};
  temporaryGlobal(t,'document',{
    addEventListener(type,callback){if(!events.has(type))events.set(type,[]);events.get(type).push(callback);},
    getElementById(id){return id==='toast-region'?toast:{addEventListener(){}};},
    createElement(){return {remove(){}};},
    querySelectorAll(){return [];},
  });
  temporaryGlobal(t,'location',{origin:'http://localhost'});
  temporaryGlobal(t,'window',{innerWidth:1280,innerHeight:900,devicePixelRatio:1,addEventListener(){}});
  const assetId='a'.repeat(64);
  const project=ensureProject({
    id:'composition_regressions',schema_version:1,title:'Synthetic',revision:1,
    created_at:'2026-01-01',updated_at:'2026-01-01',
    assets:[{id:assetId,sha256:assetId,kind:'image',name:'Synthetic.png',width_px:100,height_px:100,channels:1}],
    panels:[
      {id:'first',asset_id:assetId,x_mm:10,y_mm:20,width_mm:20,height_mm:20,label:'A',label_mode:'auto'},
      {id:'second',asset_id:assetId,x_mm:50,y_mm:20,width_mm:20,height_mm:20,label:'B',label_mode:'auto'},
    ],
    annotations:[],settings:{},figure:{width_mm:180,height_mm:120,background:'#ffffff'},
  });
  Object.assign(state,{project,history:[],future:[],selectedPanelId:null,selectedPanelIds:[],
    selectedAssetId:null,selectedAnnotationId:null,dirty:false,saveState:'saved',tab:'compose',
    modal:null,busy:null,interactionLock:false,compositionPresetDraft:null,mutationSequence:0,
    preflight:null,sourceRoi:null,sourcePointerIdentity:null,formCommitPending:false,formPointerPending:false});
  return {project,events};
}

function pointerSurface(t,events) {
  const element=()=>({attributes:{},setAttribute(key,value){this.attributes[key]=value;}});
  const panelElements=new Map(state.project.panels.map(panel=>[panel.id,element()]));
  const outline=element();outline.querySelector=()=>element();
  const svg={setPointerCapture(){},getScreenCTM:()=>({inverse:()=>({})}),querySelector(selector){
    if(selector==='#snap-guides')return {innerHTML:''};
    if(selector==='[data-selection-outline]')return outline;
    return panelElements.get(selector.match(/data-panel-id="([^"]+)"/)?.[1]) || null;
  }};
  temporaryGlobal(t,'DOMPoint',class {constructor(x,y){this.x=x;this.y=y;}matrixTransform(){return {x:this.x,y:this.y};}});
  const getElementById=document.getElementById;
  document.getElementById=id=>id==='figure-svg'?svg:getElementById(id);
  bindGestures();
  return function dispatch(type,{panelId='first',handle=null,x=30,y=30,...modifiers}={}) {
    const target={dataset:handle?{selectionHandle:handle}:{},closest(selector){
      if(selector==='#figure-svg')return svg;
      if(selector==='[data-panel-id]'&&panelId)return {dataset:{panelId}};
      return null;
    }};
    const event={target,button:0,pointerId:1,clientX:x,clientY:y,preventDefault(){},...modifiers};
    for(const listener of events.get(type) || [])listener(event);
  };
}

test('undoing ungroup expands a partial selection before the next group drag',t=>{
  const {project}=fixture(t);
  selectPanel('first');selectPanel('second',{toggle:true});groupComposition();
  const group=project.panels[0].group_id;
  groupComposition(true);selectPanel('first');
  assert.deepEqual(selectedPanels().map(panel=>panel.id),['first']);
  undo();selectPanel('first',{preserve:true});
  assert.ok(group);
  assert.ok(project.panels.every(panel=>panel.group_id===group));
  assert.deepEqual(selectedPanels().map(panel=>panel.id),['first','second']);
  const chosen=new Set(selectedPanels().map(panel=>panel.id));
  mutate(current=>{for(const panel of current.panels)if(chosen.has(panel.id))panel.x_mm+=3;});
  assert.deepEqual(project.panels.map(panel=>panel.x_mm),[13,53]);
});

test('journal font and numbering changes are included in the fitting transaction',t=>{
  const {project}=fixture(t);
  project.figure.width_mm=89;project.panels=project.panels.slice(0,1);
  Object.assign(project.panels[0],{y_mm:4.3,label_font_size_pt:6});
  assert.ok(panelBounds(project.panels[0]).top>=0);
  state.compositionPresetDraft='nature-single';
  const before=captureRecipe();applyJournalPreset();
  const bounds=panelBounds(project.panels[0]);
  assert.equal(project.figure.width_mm,89);
  assert.equal(project.panels[0].label_font_size_pt,8);
  assert.equal(project.panels[0].label,'a');
  assert.ok(bounds.top>=-1e-5,`Caption top ${bounds.top} mm is clipped after preset adaptation`);
  assert.ok(bounds.right<=project.figure.width_mm+1e-5);
  assert.ok(bounds.bottom<=project.figure.height_mm+1e-5);
  assert.equal(project.settings.composition.journal_preset_id,'nature-single');
  assert.equal(project.settings.composition.journal_preset_snapshot.id,'nature-single');
  assert.equal(state.history.length,1);
  undo();assert.deepEqual(captureRecipe(),before);
});

for(const cancelled of [true,false])test(`saving waits for a ${cancelled?'cancelled':'committed'} gesture before taking its snapshot`,async t=>{
  const {project}=fixture(t),writes=[];
  t.mock.method(globalThis,'fetch',async(path,options)=>{
    assert.equal(path,'/api/project');
    const payload=JSON.parse(options.body);writes.push(payload);
    return {ok:true,headers:{get:()=> 'application/json'},json:async()=>({...payload,revision:payload.revision+1})};
  });
  // A preceding edit has an outstanding autosave when the pointer gesture starts.
  state.dirty=true;state.saveState='pending';
  const before=captureRecipe();state.interactionLock=true;
  project.panels[0].x_mm=70;
  const pending=persistProject();
  await Promise.resolve();
  const writesDuringGesture=writes.length;
  if(cancelled)restoreRecipe(before);
  state.interactionLock=false;
  if(cancelled)emit();else commitGesture(before,'Drag panel');
  await pending;
  assert.equal(writesDuringGesture,0,'The server must never receive uncommitted pointer geometry');
  assert.equal(writes.length,1);
  assert.equal(writes[0].panels[0].x_mm,cancelled?10:70);
  assert.equal(project.panels[0].x_mm,writes[0].panels[0].x_mm);
  assert.equal(state.dirty,false);
  assert.equal(state.saveState,'saved');
  assert.equal(state.history.length,cancelled?0:1);
});

test('opening a project clears an unrelated journal selection draft',t=>{
  fixture(t);state.compositionPresetDraft='nature-single';
  const other=ensureProject({id:'other_project',title:'Other',panels:[],assets:[],annotations:[],
    figure:{width_mm:85,height_mm:120},settings:{composition:{journal_preset_id:'cell-single'}}});
  replaceProject(other);
  assert.ok(!state.compositionPresetDraft);
  assert.equal(state.project.settings.composition.journal_preset_id,'cell-single');
  const html=renderCompositionControls(state.project);
  assert.equal(html.match(/<option value="([^"]+)" selected>/)?.[1],'cell-single');
});

test('zero and signed line deltas render literally and remain editable',t=>{
  const {project,events}=fixture(t);project.panels=[];
  project.annotations=[
    {id:'horizontal',type:'line',x_mm:12,y_mm:12,width_mm:25,height_mm:0,color:'#222222'},
    {id:'vertical',type:'line',x_mm:45,y_mm:12,width_mm:0,height_mm:20,color:'#222222'},
    {id:'reverse',type:'line',x_mm:80,y_mm:40,width_mm:-20,height_mm:-10,color:'#222222'},
  ];
  const html=renderComposer();
  assert.match(html,/<line x1="0" y1="0" x2="25" y2="0"/);
  assert.match(html,/<line x1="0" y1="0" x2="0" y2="20"/);
  assert.match(html,/<line x1="0" y1="0" x2="-20" y2="-10"/);
  state.selectedAnnotationId='reverse';
  bindInteractions();
  const field=(key,value)=>({type:'number',tagName:'INPUT',value:String(value),
    dataset:{scope:'annotation',field:key},matches:selector=>selector==='[data-field]'});
  for(const [key,value] of [['width_mm',0],['height_mm',-15]]) {
    const element=field(key,value);
    for(const listener of events.get('change') || [])listener({target:element});
    assert.equal(project.annotations[2][key],value);
  }
});

test('a pointer click without movement leaves recipe, history and save state untouched',t=>{
  const {events}=fixture(t),dispatch=pointerSurface(t,events),before=captureRecipe();
  dispatch('pointerdown');dispatch('pointerup');
  assert.deepEqual(captureRecipe(),before);
  assert.equal(state.history.length,0);assert.equal(state.dirty,false);
  assert.equal(state.interactionLock,false);
});

test('plain click narrows an existing multiselection only on release, while modifier clicks toggle once',t=>{
  const {events}=fixture(t),dispatch=pointerSurface(t,events);
  selectPanel('first');selectPanel('second',{toggle:true});
  const before=captureRecipe();
  dispatch('pointerdown');
  assert.deepEqual(state.selectedPanelIds,['first','second'],'a pending drag retains the whole selection');
  dispatch('pointerup');
  assert.deepEqual(state.selectedPanelIds,['first']);
  for(const modifier of ['shiftKey','ctrlKey','metaKey']) {
    dispatch('pointerdown',{panelId:'second',[modifier]:true});
    dispatch('pointerup',{panelId:'second',[modifier]:true});
    assert.deepEqual(state.selectedPanelIds,['first','second']);
    dispatch('pointerdown',{panelId:'second',[modifier]:true});
    dispatch('pointerup',{panelId:'second',[modifier]:true});
    assert.deepEqual(state.selectedPanelIds,['first']);
  }
  assert.deepEqual(captureRecipe(),before);
  assert.equal(state.history.length,0);assert.equal(state.dirty,false);
});

test('dragging a selected object keeps an ungrouped multiselection and moves both objects',t=>{
  const {project,events}=fixture(t),dispatch=pointerSurface(t,events);
  selectPanel('first');selectPanel('second',{toggle:true});
  dispatch('pointerdown',{x:30,y:30});dispatch('pointerup',{x:38,y:35});
  assert.deepEqual(state.selectedPanelIds,['first','second']);
  assert.deepEqual(project.panels.map(panel=>[panel.x_mm,panel.y_mm]),[[18,25],[58,25]]);
  assert.equal(state.history.length,1);
});

test('group pointer movement and corner scaling each commit one undoable transaction',t=>{
  const {project,events}=fixture(t);
  for(const panel of project.panels)panel.group_id='test_group';
  selectPanel('first');
  const dispatch=pointerSurface(t,events),before=captureRecipe();
  dispatch('pointerdown',{x:30,y:30});dispatch('pointerup',{x:38,y:35});
  assert.deepEqual(project.panels.map(panel=>[panel.x_mm,panel.y_mm]),[[18,25],[58,25]]);
  assert.equal(state.history.length,1);undo();assert.deepEqual(captureRecipe(),before);
  dispatch('pointerdown',{panelId:null,handle:'se',x:70,y:40});
  dispatch('pointerup',{panelId:null,handle:'se',x:90,y:45});
  const scales=project.panels.map((panel,index)=>panel.width_mm/before.panels[index].width_mm);
  assert.ok(scales[0]>1);assert.ok(Math.abs(scales[0]-scales[1])<.001);
  assert.equal(state.history.length,1);undo();assert.deepEqual(captureRecipe(),before);
  assert.equal(state.interactionLock,false);
});
