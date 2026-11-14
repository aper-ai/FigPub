/** Production rendering and event handlers, with geometry emulated explicitly.
 * Native Enter/Space activation and actual viewport layout run in browser QA.
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
  import {state,replaceProject,subscribe,emit,analysisSelection,selectAsset} from './web/modules/state.js';
  import {analysisSourceAssetIds} from './web/modules/analysis-domain.js';
  import {renderApp} from './web/modules/view.js';
  import {bindInteractions} from './web/modules/interaction.js';
  subscribe(renderApp);bindInteractions();
  window.workbench={state,replaceProject,emit,analysisSelection,selectAsset,analysisSourceAssetIds};
`,resolveDir:root,sourcefile:'result-navigation-entry.js'},bundle:true,write:false,format:'iife',platform:'browser'});
const index=(await readFile(join(root,'web/index.html'),'utf8')).replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'');

function fixture(t,kind='qpcr') {
  const window=new Window({url:'http://127.0.0.1:8765',settings:{
    disableCSSFileLoading:true,disableJavaScriptFileLoading:true,
    disableIframePageLoading:true,disableComputedStyleRendering:true,
  }});
  t.after(()=>window.happyDOM.abort());
  window.structuredClone=globalThis.structuredClone;
  const requests=[],scrolls=[],errors=[];
  window.fetch=async(url,options)=>{requests.push({url,options});throw new Error('Navigation must not request data');};
  window.addEventListener('error',event=>errors.push(event.message));
  const prototype=window.HTMLElement.prototype;
  prototype.scrollTo=function(options){scrolls.push({element:this,options});this.scrollTop=options.top;};
  const nativeRect=prototype.getBoundingClientRect;
  prototype.getBoundingClientRect=function(){
    if(this.classList.contains('analysis-workspace'))return {top:210};
    if(this.id==='analysis-results')return {top:930-this.closest('.analysis-workspace').scrollTop};
    return nativeRect.call(this);
  };
  window.document.write(index);window.eval(bundled.outputFiles[0].text);
  const app=window.workbench,image={id:'raw-image',name:'Synthetic.tif',kind:'image',width_px:200,height_px:100,channels:1,bit_depth:16};
  const tables=[{id:'A',name:'A.xlsx',kind:'table'},{id:'B',name:'B.xlsx',kind:'table'}];
  const recipe={reference_gene:'ACTB',reference_genes:['ACTB'],control_group:'Control',method:'intensity'};
  app.state.tab='analysis';app.state.analysisKind=kind;
  app.replaceProject({id:`navigation-${kind}`,title:'Synthetic navigation',revision:4,
    assets:kind==='qpcr'?[...tables,image]:[image,...tables],panels:[],annotations:[],measurements:[],
    figure:{width_mm:180,height_mm:120},settings:{analysis_params:recipe,
      qpcr_workspaces:Object.fromEntries(tables.map(table=>[table.id,{sheet:null,worksheets:{null:recipe}}])),
      analysis_rois:[{id:'region',asset_id:image.id,source:{asset_id:image.id,series:0,frame:0,channel:0},
        x:10,y:10,width:20,height:10,role:'target',sample_id:'S1',group:'Control',protein:'MYC',biological_replicate:'B1'}]}});
  function record(id='result-current') {
    const selection=app.analysisSelection();assert.equal(selection.input.error,null);
    return {id,kind,project_id:app.state.project.id,params:structuredClone(selection.input.params),
      source_asset_ids:app.analysisSourceAssetIds(kind,selection.input.params),
      result:{kind,rows:[],summary:kind==='microscopy'?{pixel_count:20000,mean_intensity:30}:undefined,
        groups:kind==='microscopy'?[]:[{group:'Control',mean:1,n_biological:3}]}};
  }
  const doc=window.document;
  async function click(element){element.click();await Promise.resolve();await Promise.resolve();}
  return {window,doc,app,record,requests,scrolls,errors,click};
}

for(const kind of ['qpcr','wb','microscopy'])test(`${kind}: completion leaves the viewport unchanged; explicit result navigation preserves data and current focus across redraws`,async t=>{
  const {doc,app,record,requests,scrolls,errors,click}=fixture(t,kind);
  assert.equal(doc.querySelector('[data-action="view-analysis-results"]'),null);
  assert.equal(doc.getElementById('analysis-results'),null);
  doc.querySelector('.analysis-workspace').scrollTop=36;
  doc.getElementById('project-title').focus();
  app.state.project.measurements.push(record());app.emit();
  assert.equal(doc.querySelector('.analysis-workspace').scrollTop,36);
  assert.equal(doc.activeElement.id,'project-title','new results do not capture focus');
  assert.equal(scrolls.length,0,'rendering a completed result does not navigate');
  const button=doc.querySelector('[data-action="view-analysis-results"]');
  assert.equal(button.getAttribute('aria-controls'),'analysis-results');
  assert.equal(button.dataset.measurementId,'result-current');
  assert.equal(button.textContent.trim(),'查看结果');
  const before=JSON.stringify(app.state.project),history=app.state.history.length,requestCount=requests.length;
  await click(button);
  assert.equal(scrolls.length,1);
  assert.equal(scrolls[0].element,doc.querySelector('.analysis-workspace'));
  assert.equal(scrolls[0].options.top,708);
  assert.equal(scrolls[0].options.behavior,'auto');
  assert.equal(doc.activeElement.id,'analysis-results-title');
  assert.equal(doc.activeElement.dataset.measurementId,'result-current');
  app.emit();
  assert.equal(doc.activeElement.id,'analysis-results-title','a later status/save redraw preserves explicit navigation');
  assert.equal(doc.querySelector('.analysis-workspace').scrollTop,708);
  assert.equal(scrolls.length,1);
  assert.equal(JSON.stringify(app.state.project),before);
  assert.equal(app.state.history.length,history);
  assert.equal(requests.length,requestCount,'navigation adds no requests to the initial image metadata read');
  app.state.project.measurements.push(record('result-new'));app.emit();
  assert.notEqual(doc.activeElement.id,'analysis-results-title','a new measurement does not inherit the old result focus');
  assert.equal(scrolls.length,1);
  assert.deepEqual(errors,[]);
});

test('result navigation distinguishes saved, running, stale and unsaved records without lifting plot restrictions',async t=>{
  const {doc,app,record,requests,scrolls,click}=fixture(t);
  const measurement=record();app.state.project.measurements.push(measurement);app.emit();
  const label=()=>doc.getElementById('analysis-results-link')?.textContent.trim();
  assert.equal(label(),'查看结果');
  assert.equal(doc.querySelector('[data-action="plot-results"]').disabled,false);
  app.state.job={id:'running',kind:'qpcr',status:'running',startedAt:Date.now()};app.emit();
  assert.equal(label(),'查看已有结果');
  app.state.job=null;
  app.state.project.settings.qpcr_workspaces.A.worksheets.null.control_group='Treatment';app.emit();
  assert.equal(label(),'查看上次结果');
  assert.equal(doc.querySelector('[data-action="plot-results"]').disabled,true);
  await click(doc.getElementById('analysis-results-link'));
  assert.equal(doc.activeElement.id,'analysis-results-title');
  assert.ok(doc.querySelector('#analysis-results .notice.warning'));
  app.state.project.measurements=[];app.state.resultRecord=measurement;app.emit();
  assert.equal(label(),'查看未保存结果');
  assert.equal(doc.querySelector('[data-action="plot-results"]').disabled,true);
  const navigations=scrolls.length;
  app.selectAsset('B');
  assert.equal(label(),undefined,'a completed job from another file does not expose its navigation');
  assert.equal(doc.getElementById('analysis-results'),null);
  assert.equal(scrolls.length,navigations);
  assert.equal(requests.length,0,JSON.stringify(requests.map(item=>item.url)));
});

test('a result replaced between render and activation is never reached through an obsolete navigation button',async t=>{
  const {doc,app,record,requests,scrolls,click}=fixture(t);
  app.state.project.measurements.push(record('old'));app.emit();
  const obsolete=doc.getElementById('analysis-results-link');
  app.state.project.measurements=[record('new')];
  await click(obsolete);
  assert.equal(scrolls.length,0);
  assert.equal(requests.length,0,JSON.stringify(requests.map(item=>item.url)));
  app.emit();
  await click(doc.getElementById('analysis-results-link'));
  assert.equal(scrolls.length,1);
  assert.equal(doc.activeElement.dataset.measurementId,'new');
});

test('native result-button keyboard activation is not intercepted by source drawing shortcuts',async t=>{
  const {window,doc,app,record,requests,click}=fixture(t,'microscopy');
  app.state.project.measurements.push(record());app.state.sourceTool='polygon';app.emit();
  const button=doc.getElementById('analysis-results-link');
  const requestCount=requests.length;
  assert.equal(button.tagName,'BUTTON');assert.equal(button.disabled,false);
  button.focus();assert.equal(doc.activeElement,button);
  for(const [key,code] of [['Enter','Enter'],[' ','Space']]){
    const event=new window.KeyboardEvent('keydown',{key,code,bubbles:true,cancelable:true});
    button.dispatchEvent(event);
    assert.equal(event.defaultPrevented,false,'leave activation to the browser native button');
    assert.equal(app.state.spacePressed,false);
  }
  // DOM emulation does not synthesize trusted keyboard activation; exercise its
  // resulting click separately. Real Enter/Space traversal remains browser QA.
  await click(button);
  const heading=doc.getElementById('analysis-results-title');
  assert.equal(doc.activeElement,heading);assert.equal(heading.tabIndex,-1);
  const next=doc.querySelector('#analysis-results [data-action="download-results"]');
  assert.ok(heading.compareDocumentPosition(next)&window.Node.DOCUMENT_POSITION_FOLLOWING);
  assert.equal(requests.length,requestCount,'native activation adds no API calls');
});
