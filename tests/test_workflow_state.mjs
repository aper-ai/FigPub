import assert from 'node:assert/strict';
import test from 'node:test';
import {analysisInputSignature,analysisSourceAssetIds,buildAnalysisParams,defaultAnalysisParams,wbScopedRois} from '../web/modules/analysis-domain.js';
import {ensureProject} from '../web/modules/domain.js';
import {replaceProject,state} from '../web/modules/state.js';
import {renderAnalysis} from '../web/modules/analysis.js';

const asset={id:'target-original',kind:'image',width_px:320,height_px:200,channels:3};
const source=(asset_id,channel=0)=>({asset_id,series:0,frame:0,channel});
const channels=[{id:'structure',source:source(asset.id)},{id:'nuclei',source:source(asset.id,1)}];

test('scoping one WB draft retains its explicitly paired cross-file control only',()=>{
  const rois=[
    {id:'target',asset_id:asset.id,source:source(asset.id),wb_draft_id:'target-draft',control_roi_id:'control',x:1,y:1,width:20,height:10},
    {id:'control',asset_id:'control-original',source:source('control-original'),wb_draft_id:'control-draft',x:1,y:1,width:20,height:10},
    {id:'unrelated',asset_id:'other',source:source('other'),wb_draft_id:'unrelated-draft'},
  ];
  assert.deepEqual(wbScopedRois(rois,'target-draft').map(row=>row.id),['target','control']);
  const request=buildAnalysisParams({kind:'wb',asset,rois,values:{...defaultAnalysisParams(),wb_draft_scope_id:'target-draft',adc_bits:'12',black_level:'50'}});
  assert.deepEqual(analysisSourceAssetIds('wb',request),['control-original','target-original']);
  assert.equal(request.adc_bits,12);assert.equal(request.black_level,50);
  assert.notEqual(analysisInputSignature('wb',request),analysisInputSignature('wb',{...request,black_level:0}));
  request.rois[0].x=99;assert.equal(rois[0].x,1);
});

test('fiber measurements preserve dedicated thresholds, biological channels, and scale in one request',()=>{
  const request=buildAnalysisParams({kind:'microscopy',asset,channels,values:{...defaultAnalysisParams(),method:'myotube',fiber_channel_id:'structure',nucleus_channel_id:'nuclei',fiber_min_area:80,fiber_threshold_method:'manual',fiber_threshold:320,polarity:'dark',fiber_polarity:'bright',um_per_px:.5}});
  assert.equal(request.min_area,80);assert.equal(request.threshold,320);assert.equal(request.polarity,'bright');
  assert.equal(request.um_per_px,.5);assert.equal(request.nucleus_channel_id,'nuclei');
  assert.deepEqual(request.channels,channels);
  assert.notEqual(analysisInputSignature('microscopy',request),analysisInputSignature('microscopy',{...request,min_area:81}));
});

test('advanced IHC uses the RGB frame and source-local geometry while a locked profile is reusable',()=>{
  const profile={version:1,measurement_basis:'natural_log_OD',grade_thresholds:[.2,.5,.8],stain_vectors:[[.65,.704,.286],[.268,.57,.776]],rgb_white_level:255};
  const values={...defaultAnalysisParams(),method:'ihc_advanced',locked_profile:profile};
  const request=buildAnalysisParams({kind:'microscopy',asset,channels,source:{...source(asset.id,2),frame:3},values,geometry:{roi:{x:1,y:2,width:40,height:30}}});
  assert.equal(request.frame,3);assert.equal(request.channel,undefined);assert.equal(request.channels,undefined);
  assert.deepEqual(request.locked_profile,profile);assert.equal(request.stain_method,undefined);
  const next=buildAnalysisParams({kind:'microscopy',asset:{...asset,id:'new-original'},source:source('new-original'),values});
  assert.equal(next.roi,undefined);assert.deepEqual(next.exclude_rois,[]);assert.deepEqual(next.locked_profile,profile);
  assert.notEqual(analysisInputSignature('microscopy',request),analysisInputSignature('microscopy',next));
});

test('entering microscopy before the first result renders an empty workspace safely',t=>{
  t.mock.method(globalThis,'setTimeout',()=>0);
  replaceProject(ensureProject({id:'fresh',revision:0,assets:[],panels:[],annotations:[],measurements:[],settings:{},figure:{width_mm:180,height_mm:120}}));
  state.analysisKind='microscopy';state.selectedAssetId=null;
  assert.match(renderAnalysis(),/显微图像/);
});
