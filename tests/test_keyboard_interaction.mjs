/** Production keyboard delegation and rendering. DOM events do not synthesize
 * trusted native clicks; actual Space/Enter activation is also checked in browser QA.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {join} from 'node:path';
import {build} from 'esbuild';
import {Window} from 'happy-dom';

const root=fileURLToPath(new URL('../',import.meta.url));
const bundled=await build({stdin:{contents:`
  import {state,replaceProject,subscribe,emit} from './web/modules/state.js';
  import {renderApp} from './web/modules/view.js';
  import {bindInteractions} from './web/modules/interaction.js';
  subscribe(renderApp);bindInteractions();
  window.workbench={state,replaceProject,emit};
`,resolveDir:root,sourcefile:'keyboard-interaction-test-entry.js'},bundle:true,write:false,format:'iife',platform:'browser'});
const index=(await readFile(join(root,'web/index.html'),'utf8')).replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'');

function fixture(t,handleRequest=null) {
  const window=new Window({url:'http://127.0.0.1:8765',settings:{
    disableCSSFileLoading:true,disableJavaScriptFileLoading:true,
    disableIframePageLoading:true,disableComputedStyleRendering:true,
  }});
  t.after(()=>window.happyDOM.abort());
  window.structuredClone=globalThis.structuredClone;
  const requests=[],errors=[];
  window.fetch=async(url,options={})=>{
    requests.push({url,options});
    if(handleRequest)return handleRequest(url,options);
    if(url==='/api/project'&&options.method==='PUT')return new Response(options.body,{status:200,headers:{'content-type':'application/json'}});
    throw new Error('Keyboard test has no image metadata server');
  };
  window.addEventListener('error',event=>errors.push(event.message));
  window.document.write(index);window.eval(bundled.outputFiles[0].text);
  const app=window.workbench;
  app.state.tab='analysis';app.state.analysisKind='microscopy';
  app.replaceProject({id:'keyboard-workspace',title:'Synthetic keyboard test',revision:0,
    assets:[{id:'raw-image',name:'Synthetic.tif',kind:'image',width_px:200,height_px:100,channels:1,bit_depth:16}],
    panels:[],annotations:[],measurements:[],figure:{width_mm:180,height_mm:120},
    settings:{analysis_params:{method:'intensity'}}});
  const doc=window.document;
  function key(target,keyValue,code=keyValue,type='keydown',options={}) {
    const event=new window.KeyboardEvent(type,{key:keyValue,code,bubbles:true,cancelable:true,...options});
    target.dispatchEvent(event);return event;
  }
  async function click(element){element.click();await Promise.resolve();await Promise.resolve();}
  return {window,doc,app,requests,errors,key,click};
}

test('all visible workbench buttons retain native Enter and Space activation in the image workspace',t=>{
  const {doc,app,key,errors}=fixture(t);
  assert.ok(doc.getElementById('source-svg'));
  app.state.sourceTool='polygon';app.state.sourcePolygon=[[10,10],[40,10],[10,30]];app.emit();
  const before=JSON.stringify(app.state.project),polygon=JSON.stringify(app.state.sourcePolygon);
  const selectors=['[data-action="run-analysis"]','[data-action="zoom-fit"]','[data-action="focus-analysis-settings"]','[data-action="export-dialog"]'];
  for(const selector of selectors){
    const button=doc.querySelector(selector);assert.ok(button,selector);assert.equal(button.disabled,false,selector);
    button.focus();
    for(const [value,code] of [[' ','Space'],['Enter','Enter']]){
      const event=key(button,value,code);
      assert.equal(event.defaultPrevented,false,`${selector}: native ${code} must remain available`);
      assert.equal(app.state.spacePressed,false);
      assert.equal(app.state.sourceTool,'polygon');
      assert.equal(JSON.stringify(app.state.sourcePolygon),polygon);
      assert.equal(JSON.stringify(app.state.project),before);
    }
    const icon=button.querySelector('svg,path');
    if(icon)assert.equal(key(icon,' ','Space').defaultPrevented,false,'a nested icon is still inside its button');
  }
  assert.deepEqual(errors,[]);
});

test('links, disclosure summaries and accessible button controls do not lose activation to drawing shortcuts',t=>{
  const {doc,app,key}=fixture(t);
  app.state.sourceTool='polygon';app.state.sourcePolygon=[[10,10],[40,10],[10,30]];
  const host=doc.createElement('div');
  host.innerHTML='<a href="#help">Help</a><details><summary>Advanced</summary></details><span role="button" tabindex="0"><span>Custom action</span></span>';
  doc.querySelector('#workspace-main').append(host);
  for(const element of host.querySelectorAll('a,summary,[role="button"]')){
    for(const [value,code] of [[' ','Space'],['Enter','Enter']])assert.equal(key(element.firstElementChild||element,value,code).defaultPrevented,false,element.outerHTML);
  }
  assert.equal(app.state.spacePressed,false);
  assert.equal(app.state.sourcePolygon.length,3);
});

test('source canvas still supports Space pan, release cleanup and Enter polygon completion',t=>{
  const {doc,app,key}=fixture(t);
  const canvas=doc.getElementById('source-svg');
  assert.equal(key(canvas,' ','Space').defaultPrevented,true);
  assert.equal(app.state.spacePressed,true);
  assert.equal(canvas.classList.contains('pannable'),true);
  key(canvas,' ','Space','keyup');
  assert.equal(app.state.spacePressed,false);
  assert.equal(canvas.classList.contains('pannable'),false);
  app.state.sourceTool='polygon';app.state.sourcePolygon=[[10,10],[40,10],[10,30]];app.emit();
  assert.equal(key(doc.getElementById('source-svg'),'Enter').defaultPrevented,true);
  assert.equal(app.state.sourceTool,'select');
  assert.equal(app.state.sourcePolygon.length,0);
  assert.equal(app.state.sourceRoi.type,'polygon');
  assert.deepEqual(JSON.parse(JSON.stringify(app.state.sourceRoi.points)),[[10,10],[40,10],[10,30]]);
});

test('text input still commits Enter and ignores the Space pan shortcut',t=>{
  const {window,doc,app,key}=fixture(t);
  const input=doc.getElementById('project-title');
  input.focus();assert.equal(key(input,' ','Space').defaultPrevented,false);
  assert.equal(app.state.spacePressed,false);
  input.value='Edited project title';input.dispatchEvent(new window.Event('input',{bubbles:true}));
  assert.equal(key(input,'Enter').defaultPrevented,true);
  assert.equal(app.state.project.title,'Edited project title');
  assert.notEqual(doc.activeElement,input);
});

test('pending analysis submission prevents project replacement and preserves the current project',async t=>{
  const {doc,app,requests,click}=fixture(t);
  app.state.analysisSubmission={kind:'microscopy',sourceAssetId:'raw-image'};
  const before=JSON.stringify(app.state.project),requestCount=requests.length;
  await click(doc.querySelector('[data-action="project-menu"]'));
  assert.equal(app.state.modal.type,'project');
  await click(doc.querySelector('.modal [data-action="new-dialog"]'));
  assert.equal(app.state.modal.type,'project','the existing project menu remains usable');
  assert.match(app.state.lastError,/分析任务正在提交/);
  assert.equal(JSON.stringify(app.state.project),before);
  assert.equal(requests.length,requestCount);
});

test('viewing a submitting analysis uses its source and kind before a job identifier exists',async t=>{
  const {doc,app,click}=fixture(t);
  app.state.tab='home';app.state.analysisKind='qpcr';app.state.selectedAssetId=null;
  app.state.job={kind:'qpcr',sourceAssetId:'previous-table',status:'done'};
  app.state.analysisSubmission={kind:'microscopy',sourceAssetId:'raw-image'};app.emit();
  const trigger=doc.createElement('button');trigger.dataset.action='show-running-analysis';doc.body.append(trigger);
  await click(trigger);
  assert.equal(app.state.tab,'analysis');
  assert.equal(app.state.analysisKind,'microscopy');
  assert.equal(app.state.selectedAssetId,'raw-image');
  assert.ok(doc.getElementById('source-svg'));
});

test('returning to a running qPCR task clears the other table before loading the task source',async t=>{
  let releaseTable;
  const tableResponse=new Promise(resolve=>{releaseTable=resolve;});
  const {doc,app,requests,click}=fixture(t,(url)=>{
    if(url==='/api/assets/table-a/qpcr?limit=1000')return tableResponse;
    throw new Error(`Unexpected request: ${url}`);
  });
  app.state.tab='home';app.state.analysisKind='qpcr';
  const recipe={reference_gene:'ACTB',reference_genes:['ACTB'],control_group:'Control'};
  app.replaceProject({id:'two-table-task',title:'Two source identities',revision:4,
    assets:[{id:'table-a',kind:'table',name:'Table A.csv'},{id:'table-b',kind:'table',name:'Table B.csv'}],
    panels:[],annotations:[],measurements:[],figure:{width_mm:180,height_mm:120},
    settings:{analysis_params:recipe,qpcr_workspaces:{
      'table-a':{sheet:null,worksheets:{null:recipe}},
      'table-b':{sheet:null,worksheets:{null:{reference_gene:'B_REFERENCE',control_group:'B_GROUP'}}},
    }}});
  app.state.selectedAssetId='table-b';
  app.state.table={canonical:true,asset_id:'table-b',sheet:null,rows:[{source_row:2,sample:'ONLY_TABLE_B',target:'B_TARGET',ct:30}]};
  app.state.job={id:'running-table-a',kind:'qpcr',sourceAssetId:'table-a',status:'running',startedAt:Date.now()};
  app.emit();
  assert.equal(app.state.analysisParams.reference_gene,'B_REFERENCE');
  const settings=JSON.stringify(app.state.project.settings),requestCount=requests.length;
  await click(doc.querySelector('.background-activity [data-action="show-running-analysis"]'));
  assert.equal(app.state.selectedAssetId,'table-a');
  assert.equal(app.state.analysisKind,'qpcr');
  assert.equal(app.state.table,null,'the old source rows are removed before the new request resolves');
  assert.equal(app.state.tableLoading,true);
  assert.equal(doc.querySelector('.table-workspace h2').textContent,'Table A.csv');
  assert.ok(!doc.querySelector('.table-workspace').textContent.includes('ONLY_TABLE_B'));
  assert.equal(app.state.analysisParams.reference_gene,'ACTB');
  assert.equal(requests.length,requestCount+1);
  assert.equal(requests.at(-1).url,'/api/assets/table-a/qpcr?limit=1000');
  releaseTable(new Response(JSON.stringify({sheet:null,sheets:[],rows:[{source_row:2,sample:'ONLY_TABLE_A',target:'MYC',ct:22}],total_rows:1,header_row:1}),
    {status:200,headers:{'content-type':'application/json'}}));
  for(let attempt=0;attempt<100&&app.state.tableLoading;attempt++)await new Promise(resolve=>setTimeout(resolve,1));
  assert.equal(app.state.tableLoading,false);
  assert.equal(app.state.table.asset_id,'table-a');
  assert.ok(doc.querySelector('.table-workspace').textContent.includes('ONLY_TABLE_A'));
  assert.ok(!doc.querySelector('.table-workspace').textContent.includes('ONLY_TABLE_B'));
  assert.equal(app.state.job.id,'running-table-a');
  assert.equal(JSON.stringify(app.state.project.settings),settings,'navigation restores per-source controls without editing scientific parameters');
  assert.ok(!requests.slice(requestCount).some(request=>request.options.method==='POST'));
});
