import test from 'node:test';
import assert from 'node:assert/strict';
import {ensureProject} from '../web/modules/domain.js';
import {state,replaceProject,selectAsset,analysisSelection,mutate,undo,redo,emit} from '../web/modules/state.js';
import {initializeQpcrWorkspaces,qpcrParameters,editableQpcrParameters,selectQpcrWorksheet,bindQpcrWorksheet,qpcrTableMatches} from '../web/modules/qpcr-workspace.js';
import {renderAnalysisInspector} from '../web/modules/analysis-forms.js';

function fixture() {
  return ensureProject({id:'qPCR-scopes',assets:[{id:'A',kind:'table',name:'A.xlsx'},{id:'B',kind:'table',name:'B.xlsx'}],
    settings:{analysis_params:{reference_gene:'ACTB',reference_genes:['ACTB'],control_group:'Control',sheet:'A-results',group_mapping:{'experiment-1::S1':'Manual-A'},excluded_source_rows:[3]}}});
}

test('legacy qPCR settings bind only to the prior current asset, never all tables',()=>{
  const project=fixture();assert.equal(initializeQpcrWorkspaces(project,'A'),true);
  assert.equal(qpcrParameters(project,'A').sheet,'A-results');
  assert.deepEqual(qpcrParameters(project,'A').excluded_source_rows,[3]);
  assert.deepEqual(qpcrParameters(project,'B').group_mapping,{});
  assert.deepEqual(qpcrParameters(project,'B').excluded_source_rows,[]);
  assert.equal(qpcrParameters(project,'B').sheet,null);
  assert.equal(qpcrParameters(project,'B').reference_gene,'');
  assert.equal(initializeQpcrWorkspaces(project,'B'),false);
  assert.equal(qpcrParameters(project,'B').sheet,null);
  const unrelated=fixture();initializeQpcrWorkspaces(unrelated,'not-a-table');
  assert.deepEqual(unrelated.settings.qpcr_workspaces,{});
});

test('each file and worksheet retains its own complete recipe, including repeated sample/row IDs',()=>{
  const project=fixture();initializeQpcrWorkspaces(project,'A');
  selectQpcrWorksheet(project,'A','A-other');
  assert.deepEqual(qpcrParameters(project,'A').excluded_source_rows,[]);
  assert.deepEqual(qpcrParameters(project,'A').group_mapping,{});
  Object.assign(editableQpcrParameters(project,'A'),{excluded_source_rows:[5],group_mapping:{'experiment-1::S1':'Other-A'}});
  selectQpcrWorksheet(project,'B','B-results');
  Object.assign(editableQpcrParameters(project,'B'),{reference_gene:'GAPDH',excluded_source_rows:[7]});
  selectQpcrWorksheet(project,'A','A-results');
  assert.deepEqual(qpcrParameters(project,'A').excluded_source_rows,[3]);
  assert.equal(qpcrParameters(project,'A').group_mapping['experiment-1::S1'],'Manual-A');
  selectQpcrWorksheet(project,'A','A-other');
  assert.deepEqual(qpcrParameters(project,'A').excluded_source_rows,[5]);
  assert.deepEqual(qpcrParameters(project,'B').excluded_source_rows,[7]);
  const detached=qpcrParameters(project,'B');detached.excluded_source_rows.push(999);
  assert.deepEqual(qpcrParameters(project,'B').excluded_source_rows,[7]);
});

test('auto worksheet discovery reuses confirmed sheet edits and cannot overwrite a later selection',()=>{
  const project=fixture();initializeQpcrWorkspaces(project,'A');
  selectQpcrWorksheet(project,'A',null);
  assert.equal(bindQpcrWorksheet(project,'A',null,'A-results'),true);
  assert.deepEqual(qpcrParameters(project,'A').excluded_source_rows,[3]);
  selectQpcrWorksheet(project,'A','A-other');
  assert.equal(bindQpcrWorksheet(project,'A',null,'A-results'),false);
  assert.equal(qpcrParameters(project,'A').sheet,'A-other');
});

test('all source selection paths resolve current qPCR input, with scope-aware undo and redo',t=>{
  t.mock.method(globalThis,'setTimeout',()=>0);
  replaceProject(fixture());state.analysisKind='qpcr';state.tab='analysis';
  selectAsset('B');
  assert.equal(state.analysisParams.sheet,null);
  assert.deepEqual(state.analysisParams.excluded_source_rows,[]);
  mutate(project=>Object.assign(editableQpcrParameters(project,'B'),{reference_gene:'ACTB',reference_genes:['ACTB'],control_group:'Control'}),'Confirm B identity');
  assert.deepEqual(analysisSelection().input.params.excluded_source_rows,[]);
  assert.deepEqual(analysisSelection().input.params.group_mapping,{});
  state.selectedAssetId='A';emit();
  assert.equal(state.analysisParams.sheet,'A-results');
  assert.deepEqual(analysisSelection().input.params.excluded_source_rows,[3]);
  mutate(project=>selectQpcrWorksheet(project,'A','A-other'),'Select second sheet');
  assert.equal(state.analysisParams.sheet,'A-other');
  assert.deepEqual(state.analysisParams.excluded_source_rows,[]);
  undo();assert.equal(state.analysisParams.sheet,'A-results');assert.deepEqual(state.analysisParams.excluded_source_rows,[3]);
  redo();assert.equal(state.analysisParams.sheet,'A-other');
  state.selectedAssetId='B';
  // Direct assignments used by import/task/job routing must also be safe before emit.
  assert.deepEqual(analysisSelection().input.params.excluded_source_rows,[]);
  assert.equal(analysisSelection().input.params.sheet,null);
});

test('failed worksheet keeps recovery selector and stale preview cannot authorize analysis',t=>{
  t.mock.method(globalThis,'setTimeout',()=>0);
  replaceProject(fixture());state.analysisKind='qpcr';state.tab='analysis';
  state.qpcrSheetNames.set('A',['A-results','A-other','Notes']);
  mutate(project=>selectQpcrWorksheet(project,'A','Notes'),'Select non-Ct sheet');
  state.table=null;state.tableError='未找到 Ct 表头';
  const markup=renderAnalysisInspector();
  assert.match(markup,/id="analysis-sheet"/);
  assert.match(markup,/<option value=""\s*>自动识别有效 Ct 工作表<\/option>/);
  assert.match(markup,/<option value="A-results"/);
  assert.match(markup,/<option value="Notes" selected/);
  assert.equal(qpcrTableMatches({canonical:true,asset_id:'B',sheet:'Notes'},'A',state.analysisParams),false);
  assert.equal(qpcrTableMatches({canonical:true,asset_id:'A',sheet:'A-results'},'A',state.analysisParams),false);
  assert.equal(qpcrTableMatches(null,'A',state.analysisParams),false);
});
