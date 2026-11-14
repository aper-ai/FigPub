import assert from 'node:assert/strict';
import test from 'node:test';
import {ensureProject} from '../web/modules/domain.js';
import {buildAnalysisParams,defaultAnalysisParams} from '../web/modules/analysis-domain.js';
import {state} from '../web/modules/state.js';
import {renderAnalysis} from '../web/modules/analysis.js';
import {renderAnalysisInspector} from '../web/modules/analysis-forms.js';
import {renderHome,resolveTaskImport,taskForWorkspace,acceptsTaskAsset,hasProjectContent,workspaceGuidance} from '../web/modules/workflows.js';

const image={id:'raw-image',kind:'image',name:'原图.tif',channels:1,width_px:100,height_px:100};
const table={id:'ct-table',kind:'table',name:'真实 Ct.csv'};
const vector={id:'plot-pdf',kind:'pdf',name:'图表.pdf'};
function project(assets=[]) {return ensureProject({id:'workflow',title:'Saved <figure>',assets,panels:[],annotations:[],measurements:[],settings:{},figure:{width_mm:180,height_mm:120}});}

test('qPCR task import opens analysis and chooses a new table rather than an old image',()=>{
  const route=resolveTaskImport('qpcr',[image,table]);
  assert.equal(route.assetId,table.id);assert.equal(route.tab,'analysis');assert.equal(route.analysisKind,'qpcr');
  assert.equal(resolveTaskImport(null,[table]).task,'qpcr');
  assert.equal(resolveTaskImport('qpcr',[image]).assetId,null);
});
test('scientific image tasks select actual image assets and never a rendered PDF or SVG',()=>{
  for(const task of ['wb','microscopy']){
    assert.equal(acceptsTaskAsset(task,vector),false);
    assert.equal(resolveTaskImport(task,[vector,table,image]).assetId,image.id);
    assert.equal(resolveTaskImport(task,[vector]).assetId,null);
  }
  for(const kind of ['wb','microscopy'])assert.throws(()=>buildAnalysisParams({kind,asset:vector,values:defaultAnalysisParams()}),/原始图像/);
});
test('compose routes preserve uploaded tables as library data without treating them as picture panels',()=>{
  const sources=[image,table,vector],before=JSON.stringify(sources),route=resolveTaskImport('compose',sources);
  assert.equal(route.tab,'compose');assert.deepEqual(route.compositionAssetIds,[image.id,vector.id]);
  assert.equal(JSON.stringify(sources),before);
  assert.equal(taskForWorkspace('analysis','qpcr'),'qpcr');assert.equal(taskForWorkspace('channels','wb'),'microscopy');
});
test('home resume and project switching recognize annotation-only and analysis-only work',()=>{
  const empty=project();assert.equal(hasProjectContent(empty),false);
  empty.annotations.push({id:'a',type:'text',text:'Important note'});assert.equal(hasProjectContent(empty),true);
  empty.annotations=[];empty.measurements.push({id:'measurement'});assert.equal(hasProjectContent(empty),true);
  const markup=renderHome({project:empty});
  assert.match(markup,/data-action="resume-workspace"/);assert.match(markup,/Saved &lt;figure&gt;/);
  for(const task of ['wb','microscopy','qpcr','compose'])assert.match(markup,new RegExp(`data-task="${task}"`));
});
test('next-action guidance does not offer analysis for a missing input or silently skip library sources',()=>{
  const base={project:project([table,image]),selectedAssetId:null,tab:'compose'};
  assert.equal(workspaceGuidance(base).action,'add-all-panels');
  assert.equal(workspaceGuidance({...base,project:project([])}).action,'import');
  const qpcr={...base,tab:'analysis',analysisKind:'qpcr'};
  assert.equal(workspaceGuidance(qpcr).action,'import');
  assert.equal(workspaceGuidance({...qpcr,selectedAssetId:table.id}).action,'focus-analysis-settings');
  const failed=workspaceGuidance({...qpcr,selectedAssetId:table.id,tableError:'Missing Ct column'});
  assert.equal(failed.action,'load-table');assert.match(failed.title,/未完成/);
});
test('unready qPCR presents the missing settings before submission; empty inspector offers import',()=>{
  Object.assign(state,{project:project([table]),selectedAssetId:table.id,selectedPanelId:null,tab:'analysis',analysisKind:'qpcr',analysisParams:defaultAnalysisParams(),table:null,tableLoading:false,tableError:null,job:null,resultRecord:null});
  const markup=renderAnalysis();assert.match(markup,/id="analysis-readiness"/);assert.match(markup,/内参基因和对照组/);assert.match(markup,/data-action="run-analysis" disabled/);
  state.selectedAssetId=null;
  const inspector=renderAnalysisInspector();assert.match(inspector,/data-action="import" data-task="qpcr"/);assert.doesNotMatch(inspector,/id="analysis-control_group"/);
});
test('qPCR excluded source row zero is rejected before the API, while original row one remains valid',()=>{
  const values={...defaultAnalysisParams(),reference_gene:'ACTB',control_group:'control',excluded_source_rows:[0]};
  assert.throws(()=>buildAnalysisParams({kind:'qpcr',asset:table,values}),/原始行号/);
  values.excluded_source_rows=[1,3,3];
  assert.deepEqual(buildAnalysisParams({kind:'qpcr',asset:table,values}).excluded_source_rows,[1,3]);
});
