import assert from 'node:assert/strict';
import test from 'node:test';
import {ensureProject} from '../web/modules/domain.js';
import {
  defaultAnalysisParams,restoredAnalysisParams,buildAnalysisParams,analysisInputSignature,
  analysisSourceAssetIds,resolveAnalysisResult,buildAnalysisChartRequest,
} from '../web/modules/analysis-domain.js';
import {state,replaceProject,analysisSelection,mutate,undo,redo,emit} from '../web/modules/state.js';

function fixture() {
  return ensureProject({id:'project',title:'Analysis',revision:12,
    assets:[
      {id:'image-a',kind:'image',width_px:600,height_px:300,channels:1},
      {id:'image-b',kind:'image',width_px:600,height_px:300,channels:1},
      {id:'table-a',kind:'table'},{id:'table-b',kind:'table'},{id:'table-c',kind:'table'},
    ],
    panels:[],annotations:[],measurements:[],audit:[],
    settings:{analysis_params:{reference_gene:'GAPDH',control_group:'Control'},analysis_rois:[
      {id:'roi-a',asset_id:'image-a',x:10,y:10,width:20,height:10,role:'target',sample_id:'A',biological_replicate:'1'},
    ]},figure:{width_mm:180,height_mm:120},
  });
}
function paramsFor(project,kind,assetId,values={},roi=null) {
  return buildAnalysisParams({kind,asset:project.assets.find(asset=>asset.id===assetId),rois:project.settings.analysis_rois,values:{...restoredAnalysisParams(project.settings),...values},roi});
}
function measurement(project,id,kind,assetId,values={},roi=null) {
  const params=paramsFor(project,kind,assetId,values,roi);
  const record={id,kind,params,source_asset_ids:analysisSourceAssetIds(kind,params),source_project_revision:project.revision,result:{kind,rows:[{id,value:id}],groups:[{mean:1,values:[1]}]}};
  project.measurements.push(record);return record;
}
function selection(project,kind,assetId,values={},extra={}) {
  return resolveAnalysisResult({project,kind,assetId,values:{...restoredAnalysisParams(project.settings),...values},...extra});
}
function install(t,project=fixture(),kind='wb',assetId='image-a') {
  t.mock.method(globalThis,'setTimeout',()=>0);
  replaceProject(project);state.analysisKind=kind;state.selectedAssetId=assetId;emit();return project;
}

test('qPCR results and chart requests bind the displayed source record, not latest kind',()=>{
  const project=fixture(),a=measurement(project,'run-a','qpcr','table-a');
  measurement(project,'run-b','qpcr','table-b');
  const displayed=selection(project,'qpcr','table-a');
  assert.equal(displayed.record,a);assert.equal(displayed.stale,false);
  assert.deepEqual(buildAnalysisChartRequest(displayed,'box'),{measurement_id:'run-a',chart_type:'box',width_mm:80,height_mm:60});
  assert.equal(selection(project,'qpcr','table-b').record.id,'run-b');
  assert.equal(selection(project,'qpcr','table-c').result,null);
});

test('same-source history selects the exact parameter match before a newer different run',()=>{
  const project=fixture(),earlier=measurement(project,'run-reference-a','qpcr','table-a',{reference_gene:'ACTB'});
  measurement(project,'run-reference-b','qpcr','table-a',{reference_gene:'GAPDH'});
  const displayed=selection(project,'qpcr','table-a',{reference_gene:'ACTB'});
  assert.equal(displayed.record,earlier);assert.equal(displayed.stale,false);
  assert.equal(buildAnalysisChartRequest(displayed).measurement_id,earlier.id);
  const changed=selection(project,'qpcr','table-a',{reference_gene:'RPLP0'});
  assert.equal(changed.stale,true);assert.throws(()=>buildAnalysisChartRequest(changed),/重新分析/);
});

test('microscopy selection respects source image, method, and the measured source ROI',()=>{
  const project=fixture(),roi={x:10,y:20,width:120,height:80};
  const nuclei=measurement(project,'nuclei-a','microscopy','image-a',{method:'nuclei'},roi);
  measurement(project,'intensity-a','microscopy','image-a',{method:'intensity'});
  measurement(project,'nuclei-b','microscopy','image-b',{method:'nuclei'});
  assert.equal(selection(project,'microscopy','image-a',{method:'nuclei'},{roi}).record,nuclei);
  assert.equal(selection(project,'microscopy','image-a',{method:'nuclei'},{roi}).stale,false);
  assert.equal(selection(project,'microscopy','image-a',{method:'nuclei'},{roi:{...roi,width:121}}).stale,true);
  assert.equal(selection(project,'microscopy','image-b',{method:'nuclei'}).record.id,'nuclei-b');
});

test('WB geometry, sample identity, and background edits invalidate scientific results',()=>{
  const project=fixture();measurement(project,'wb-a','wb','image-a');
  assert.equal(selection(project,'wb','image-a').stale,false);
  assert.equal(selection(project,'wb','image-b').result,null);
  const roi=project.settings.analysis_rois[0];roi.width++;
  assert.equal(selection(project,'wb','image-a').stale,true);roi.width--;
  roi.biological_replicate='2';assert.equal(selection(project,'wb','image-a').stale,true);roi.biological_replicate='1';
  assert.equal(selection(project,'wb','image-a',{background:'none'}).stale,true);
});

test('layout, chart type, unrelated analysis fields, and project revision do not expire results',t=>{
  const project=fixture();measurement(project,'wb-a','wb','image-a');install(t,project);
  mutate(current=>{current.figure.width_mm=200;current.settings.analysis_chart_type='bar';current.settings.analysis_params.reference_gene='ACTB';current.panels.push({id:'panel',asset_id:'image-a',x_mm:50,y_mm:20,width_mm:72,height_mm:36});});
  project.revision=42;emit();
  assert.equal(analysisSelection().record.id,'wb-a');assert.equal(state.resultStale,false);
  assert.equal(buildAnalysisChartRequest(analysisSelection(),'bar').measurement_id,'wb-a');
});

test('undo and redo synchronize analysis parameters and exact result validity',t=>{
  const project=fixture();measurement(project,'wb-a','wb','image-a');install(t,project);
  mutate(current=>{current.settings.analysis_params.background='none';});
  assert.equal(state.analysisParams.background,'none');assert.equal(state.resultStale,true);
  undo();assert.equal(state.analysisParams.background,'border');assert.equal(state.resultStale,false);
  redo();assert.equal(state.analysisParams.background,'none');assert.equal(state.resultStale,true);
  undo();mutate(current=>{current.settings.analysis_rois[0].width++;});
  assert.equal(state.resultStale,true);undo();assert.equal(state.resultStale,false);
});

test('new and reopened projects reset unsaved analysis defaults instead of leaking prior settings',t=>{
  const project=fixture();project.settings.analysis_params={reference_gene:'ACTB',control_group:'Old',threshold:87,um_per_px:.4};
  install(t,project);state.resultRecord={id:'old-result'};state.job={id:'old-job'};
  const fresh=fixture();fresh.id='new-project';delete fresh.settings.analysis_params;
  replaceProject(fresh);
  assert.deepEqual(state.analysisParams,defaultAnalysisParams());assert.equal(state.resultRecord,null);assert.equal(state.job,null);
  const reopened=fixture();reopened.settings.analysis_params={reference_gene:'GAPDH'};replaceProject(reopened);
  assert.equal(state.analysisParams.reference_gene,'GAPDH');assert.equal(state.analysisParams.control_group,'');
  assert.equal(state.analysisParams.threshold,'');assert.equal(state.analysisParams.um_per_px,'');
});

test('unattached completion remains distinct from an expired or persisted measurement',()=>{
  const project=fixture(),params=paramsFor(project,'qpcr','table-a');
  const transient={id:'unattached',project_id:project.id,kind:'qpcr',params,source_asset_ids:['table-a'],result:{kind:'qpcr',groups:[{mean:1}]}};
  const displayed=selection(project,'qpcr','table-a',{}, {transientRecord:transient});
  assert.equal(displayed.record,transient);assert.equal(displayed.stale,false);assert.equal(displayed.persisted,false);
  assert.throws(()=>buildAnalysisChartRequest(displayed),/尚未保存/);
  assert.equal(selection(project,'qpcr','table-a',{}, {transientRecord:{...transient,project_id:'other'}}).result,null);
  assert.equal(resolveAnalysisResult({project:null,kind:'wb',assetId:null}).result,null);
});

test('input identity normalizes key ordering and numeric form values without including display state',()=>{
  const project=fixture(),asset=project.assets[0];
  const numeric=buildAnalysisParams({kind:'microscopy',asset,values:{channel:0,min_area:20,threshold:100,um_per_px:.5}});
  const form=buildAnalysisParams({kind:'microscopy',asset,values:{channel:'0',min_area:'20',threshold:'100',um_per_px:'0.5'}});
  assert.equal(analysisInputSignature('microscopy',numeric),analysisInputSignature('microscopy',Object.fromEntries(Object.entries(form).reverse())));
  assert.throws(()=>buildAnalysisParams({kind:'microscopy',asset,values:{um_per_px:0}}),/必须大于 0/);
  assert.throws(()=>buildAnalysisParams({kind:'microscopy',asset,values:{channel:2}}),/通道/);
});

test('backend-inferred channel axis matches while an older omitted rolling radius keeps its actual default',()=>{
  const project=fixture(),params=paramsFor(project,'microscopy','image-a');
  assert.equal(analysisInputSignature('microscopy',params),analysisInputSignature('microscopy',{...params,channel_axis:-1}));
  assert.notEqual(analysisInputSignature('microscopy',params),analysisInputSignature('microscopy',{...params,channel_axis:0}));
  const wb=paramsFor(project,'wb','image-a',{background:'rolling_ball'}),older={...wb};delete older.radius;
  assert.equal(wb.radius,50);
  assert.notEqual(analysisInputSignature('wb',wb),analysisInputSignature('wb',older));
  assert.equal(analysisInputSignature('wb',{...wb,radius:25}),analysisInputSignature('wb',older));
});
