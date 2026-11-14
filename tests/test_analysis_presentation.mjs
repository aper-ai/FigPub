/** Production markup and stylesheet cascade; physical icon bounds run in browser QA. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {Window} from 'happy-dom';
import {state,replaceProject,analysisSelection} from '../web/modules/state.js';
import {renderAnalysis} from '../web/modules/analysis.js';

const index=await readFile(new URL('../web/index.html',import.meta.url),'utf8');
const stylesheetPaths=[...index.matchAll(/<link\s+rel="stylesheet"\s+href="\/([^"]+)"/g)].map(match=>match[1]);
const styles=await Promise.all(stylesheetPaths.map(path=>readFile(new URL(`../web/${path}`,import.meta.url),'utf8')));

function fixture(t) {
  const window=new Window({width:1440,height:900,settings:{disableCSSFileLoading:true,disableJavaScriptFileLoading:true}});
  t.after(()=>window.happyDOM.abort());
  t.mock.method(globalThis,'setTimeout',()=>0);
  state.tab='analysis';state.analysisKind='qpcr';
  replaceProject({id:'presentation-project',title:'Synthetic qPCR',revision:1,
    assets:[{id:'source-table',kind:'table',name:'synthetic.tsv'}],panels:[],annotations:[],measurements:[],
    figure:{width_mm:180,height_mm:120},settings:{analysis_params:{reference_gene:'ACTB',control_group:'Control'}}});
  const document=window.document;
  for(const css of styles){const style=document.createElement('style');style.textContent=css;document.head.append(style);}
  function render(){document.body.innerHTML=`<main class="app-shell">${renderAnalysis()}</main>`;}
  return {window,document,render};
}

test('qPCR introduction positions only its heading icon, leaving the template-download icon in button flow',t=>{
  const {window,document,render}=fixture(t);render();
  const introIcon=document.querySelector('.qpcr-intro>div>.icon');
  const download=document.querySelector('.qpcr-intro [data-action="qpcr-template"]');
  const downloadIcon=download.querySelector('.icon');
  assert.equal(window.getComputedStyle(introIcon).position,'absolute');
  assert.equal(window.getComputedStyle(introIcon.parentElement).position,'relative');
  assert.equal(window.getComputedStyle(download).display,'inline-flex');
  assert.equal(downloadIcon.parentElement,download);
  assert.ok(!['absolute','fixed'].includes(window.getComputedStyle(downloadIcon).position),
    'the template icon must remain in its button instead of anchoring to the page');
});

test('qPCR result headings use a readable method name while provenance retains the exact scientific method',t=>{
  const {document,render}=fixture(t);
  const input=analysisSelection().input;
  assert.equal(input.error,null);assert.equal(input.params.method,'delta_delta_ct');
  const record={id:'measurement-qpcr',kind:'qpcr',params:structuredClone(input.params),source_asset_ids:['source-table'],
    result:{kind:'qpcr',rows:[{sample:'S1',target:'MYC',mean_ct:18.123456789,fold_change:3.67194}],groups:[],
      method:{name:'delta_delta_ct',precision:'float64_without_intermediate_rounding'}}};
  state.project.measurements.push(record);
  const before=structuredClone(state.project);render();
  assert.equal(document.querySelector('#analysis-results .card-heading .eyebrow').textContent,
    '已完成计算 · ΔΔCt 相对定量');
  const details=JSON.parse(document.querySelector('#result-method-details pre').textContent);
  assert.deepEqual(details.params,record.params);
  assert.deepEqual(details.method,record.result.method);
  assert.equal(document.querySelector('#result-method-details').open,false);
  assert.deepEqual(state.project,before,'presentation must not rewrite methods or numerical data');
});
