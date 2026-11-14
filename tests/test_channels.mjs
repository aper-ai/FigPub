import assert from 'node:assert/strict';
import test from 'node:test';
import {setImmediate as settle} from 'node:timers/promises';
import {channelFieldPatch,compositionView,compositionInitialSource,channelLayout,previewGridGeometry,channelPaneViews,viewAtSourcePlane,descriptorKey,sourceIdentityKey} from '../web/modules/channel-domain.js';
import {ChannelPreviewCache,fetchChannelPreview} from '../web/modules/channel-preview.js';
import {ensureProject,effectivePpi,scaleBarGeometry,defaultEdit} from '../web/modules/domain.js';
import {buildAnalysisParams,analysisInputSignature,analysisSourceAssetIds} from '../web/modules/analysis-domain.js';
import {state,replaceProject,mutate,undo,redo,analysisSelection,selectedAnalysisChannels,setAnalysisChannel,setAnalysisGeometry,analysisGeometry,selectAsset} from '../web/modules/state.js';
import {renderSourceOverlays} from '../web/modules/source-overlays.js';
import {panelImageMarkup} from '../web/modules/composer-view.js';
import {channelPreviews} from '../web/modules/channels.js';
import {renderTable} from '../web/modules/source-view.js';
import {renderResult} from '../web/modules/analysis-results-view.js';

const identity=(asset_id='image-a',channel=0,frame=0,series=0)=>({asset_id,series,frame,channel});
function viewFixture({frame=0,series=0}={}) {
  return {version:1,mode:'merge',active_channel:'c0',split:{columns:0,gap_px:8,include_merge:false},channels:[
    {id:'c0',name:'Channel 1',color:'#2878ff',black:null,white:null,gamma:1,visible:true,source:identity('image-a',0,frame,series)},
    {id:'c1',name:'Channel 2',color:'#25d77d',black:null,white:null,gamma:1,visible:true,source:identity('image-a',1,frame,series)},
  ]};
}
function descriptor(view=viewFixture(),width=100,height=60) {
  const source=view.channels[0].source;return {asset_id:source.asset_id,series:source.series,frame:source.frame,width_px:width,height_px:height,default_view:structuredClone(view),channels:view.channels.map(channel=>({...channel,bit_depth:16,dtype:'uint16',role:'signal',minimum:0,maximum:65535})),series_list:[{index:0,width_px:width,height_px:height,frames:8,channels:2}]};
}
function projectFixture() {
  const asset={id:'image-a',name:'cells.tif',kind:'image',width_px:100,height_px:60,channels:2,bit_depth:16};
  return ensureProject({id:'channels-project',revision:7,title:'Channel regression',assets:[asset,{...asset,id:'image-b',name:'other.tif'}],panels:[],annotations:[],measurements:[],settings:{asset_edits:{'image-a':{...defaultEdit(asset),channel_view:viewFixture()}},analysis_params:{method:'channel_intensity'}},figure:{width_mm:180,height_mm:120},audit:[]});
}
function install(t) {
  t.mock.method(globalThis,'setTimeout',()=>0);state.tab='analysis';state.analysisKind='microscopy';replaceProject(projectFixture());
  state.selectedAssetId='image-a';state.channelDescriptors=new Map();state.sourceRoi=null;state.sourcePointerIdentity=null;
  return state.project;
}
function rememberMeasurement() {
  const params=structuredClone(analysisSelection().input.params);
  assert.ok(params);const record={id:'measured',kind:'microscopy',params,source_asset_ids:analysisSourceAssetIds('microscopy',params),result:{kind:'microscopy',rows:[{channel_id:'c0',raw_mean:12}],objects:[]}};
  state.project.measurements.push(record);return record;
}

test('channel display edits are immutable and validate raw levels, gamma, and colors before saving',()=>{
  const view=viewFixture(),modified=channelFieldPatch(view,'c0','gamma','1.4');
  assert.equal(modified.channels[0].gamma,1.4);assert.equal(view.channels[0].gamma,1);
  assert.equal(channelFieldPatch(view,'c0','black','').channels[0].black,null);
  assert.equal(channelFieldPatch(view,'c0','white','65500').channels[0].white,65500);
  assert.throws(()=>channelFieldPatch(view,'c0','gamma',.01),/0.1–5/);
  assert.throws(()=>channelFieldPatch(view,'c0','gamma',6),/0.1–5/);
  assert.throws(()=>channelFieldPatch(view,'c0','color','red'),/颜色/);
  const black=channelFieldPatch(view,'c0','black',150);assert.throws(()=>channelFieldPatch(black,'c0','white',149),/白点/);
});

test('same-series frame switching preserves explicit display styles and changes every raw source identity',()=>{
  const old=viewFixture();Object.assign(old.channels[0],{name:'DAPI 核',color:'#3388FF',black:120,white:54000,gamma:1.4,visible:false});old.mode='split';old.split.include_merge=true;
  const next=viewAtSourcePlane(old,descriptor(viewFixture({frame:3})));
  assert.equal(next.channels[0].name,'DAPI 核');assert.equal(next.channels[0].gamma,1.4);assert.equal(next.channels[0].white,54000);
  assert.equal(next.channels[0].visible,false);assert.equal(next.mode,'split');assert.equal(next.split.include_merge,true);
  assert.deepEqual(next.channels.map(item=>item.source.frame),[3,3]);assert.equal(old.channels[0].source.frame,0);next.active_channel='c1';assert.deepEqual(compositionInitialSource(next,'image-a'),identity('image-a',1,3));
  const otherSeries=viewAtSourcePlane(old,descriptor(viewFixture({series:1})));
  assert.equal(otherSeries.channels[0].name,'Channel 1');assert.equal(otherSeries.channels[0].gamma,1);
});

test('combining files keeps raw source references and rejects duplicate planes and mismatched dimensions',()=>{
  const a=descriptor(),bView=viewFixture();for(const channel of bView.channels)channel.source.asset_id='image-b';const b=descriptor(bView);
  const descriptors=new Map([[descriptorKey(identity()),a],[descriptorKey(identity('image-b')),b]]);
  const combined=compositionView([identity(),identity('image-b',1)],descriptors);
  assert.equal(combined.width_px,100);assert.equal(combined.view.channels.length,2);
  assert.deepEqual(combined.view.channels[1].source,identity('image-b',1));assert.equal(combined.view.channels[1].id,'c1');
  assert.throws(()=>compositionView([identity(),identity()],descriptors),/重复/);
  descriptors.set(descriptorKey(identity('image-b')),{...b,width_px:101});
  assert.throws(()=>compositionView([identity(),identity('image-b')],descriptors),/完全相同/);
});

test('split layout matches native-pixel grid, appends merge, and includes fractional crop edge pixels',()=>{
  const view=viewFixture();view.mode='split';view.split.include_merge=true;
  const layout=channelLayout(view,100,60,{x:1.2,y:2.6,width:30.1,height:20.1});
  assert.deepEqual(layout.crop,{x:1,y:2,width:31,height:21});assert.equal(layout.columns,2);assert.equal(layout.rows,2);
  assert.equal(layout.width_px,70);assert.equal(layout.height_px,50);
  assert.deepEqual(layout.tiles.map(tile=>[tile.id,tile.x_px,tile.y_px]),[['c0',0,0],['c1',39,0],['merge',0,29]]);
  const gallery=previewGridGeometry({count:4,width_px:192,height_px:128,columns:3,gap_px:32,available_width_px:450,available_height_px:700});assert.equal(gallery.columns,3);assert.equal(gallery.gap_px/gallery.scale,32);assert.ok(gallery.tile_width_px>=145);
  view.channels[0].visible=false;view.mode='single';view.active_channel='c0';assert.equal(channelPaneViews(view)[0].id,'c0');
});

test('split panels compute effective PPI and scale bars from each original tile',()=>{
  const asset=projectFixture().assets[0],view=viewFixture();view.mode='split';view.split.columns=2;
  const panel={...defaultEdit(asset),channel_view:view,width_mm:104,height_mm:30,scale_bar:{um_per_px:.5,length_um:10,color:'#ffffff'}};
  // Two 100px tiles + 8px gap are 104mm wide: each source pixel is 0.5mm.
  assert.equal(effectivePpi(panel,asset),50.8);assert.equal(scaleBarGeometry(panel,asset).length_mm,10);
  panel.rotation=37;panel.flip_h=true;assert.equal(scaleBarGeometry(panel,asset).length_mm,10);
});

test('display visibility, mode, names, and gamma never choose or expire scientific analysis channels',t=>{
  install(t);const record=rememberMeasurement();
  mutate(project=>{const view=project.settings.asset_edits['image-a'].channel_view;view.channels[0].visible=false;view.channels[0].gamma=2;view.channels[0].name='Renamed';view.mode='single';view.active_channel='c1';});
  assert.equal(selectedAnalysisChannels()[0].source.channel,0);assert.equal(analysisSelection().record,record);assert.equal(analysisSelection().stale,false);
  setAnalysisChannel('c1',true);assert.equal(selectedAnalysisChannels().length,2);assert.equal(analysisSelection().stale,true);
  undo();assert.equal(selectedAnalysisChannels().length,1);assert.equal(analysisSelection().stale,false);
});

test('microscopy regions persist by raw identity and undo restores geometry without reverting result history',t=>{
  install(t);setAnalysisGeometry('roi',{type:'ellipse',x:10,y:10,width:30,height:20});const record=rememberMeasurement();
  setAnalysisGeometry('roi',{type:'polygon',x:10,y:10,width:40,height:30,points:[[10,10],[50,20],[30,40]]});
  assert.equal(analysisSelection().stale,true);undo();assert.equal(analysisGeometry().roi.type,'ellipse');assert.equal(state.sourceRoi.width,30);assert.equal(analysisSelection().stale,false);assert.equal(state.project.measurements[0],record);
  redo();assert.equal(analysisGeometry().roi.type,'polygon');setAnalysisChannel('c1',true,{single:true});assert.equal(analysisGeometry().roi,undefined);
  undo();assert.equal(analysisGeometry().roi.type,'polygon');selectAsset('image-b');assert.equal(analysisGeometry().roi,undefined);
});

test('scientific signatures distinguish raw frames and preserve multi-file measurement provenance',()=>{
  const asset=projectFixture().assets[0],channels=[{id:'a',name:'A',source:identity()},{id:'b',name:'B',source:identity('image-b',1,3)}];
  const params=buildAnalysisParams({kind:'microscopy',asset,values:{method:'channel_intensity'},channels});
  assert.deepEqual(analysisSourceAssetIds('microscopy',params),['image-a','image-b']);
  const renamed=structuredClone(params);renamed.channels[0].name='Display-only name';assert.equal(analysisInputSignature('microscopy',params),analysisInputSignature('microscopy',renamed));
  renamed.channels[0].source.frame=1;assert.notEqual(analysisInputSignature('microscopy',params),analysisInputSignature('microscopy',renamed));
  assert.notEqual(sourceIdentityKey(identity('image-a',0,1)),sourceIdentityKey(identity('image-a',0,0)));
});

test('manual fluorescence thresholds and fixed backgrounds are explicit per raw channel',()=>{
  const asset=projectFixture().assets[0],channels=viewFixture().channels.map(({id,name,source})=>({id,name,source}));
  const threshold=buildAnalysisParams({kind:'microscopy',asset,channels,values:{method:'threshold',threshold_method:'manual',thresholds:{c0:{value:1200},c1:{value:35}}}});
  assert.deepEqual(threshold.thresholds,{c0:{method:'manual',value:1200},c1:{method:'manual',value:35}});
  assert.throws(()=>buildAnalysisParams({kind:'microscopy',asset,channels,values:{method:'threshold',threshold_method:'manual',threshold:0,thresholds:{c0:{value:'bad'}}}}),/阈值/);
  const intensity=buildAnalysisParams({kind:'microscopy',asset,channels,values:{method:'channel_intensity',background_method:'fixed',background_values:{c0:-2,c1:37}}});
  assert.deepEqual(intensity.background_values,{c0:-2,c1:37});
  assert.throws(()=>buildAnalysisParams({kind:'microscopy',asset,channels,values:{method:'channel_intensity',background_method:'roi'}}),/背景区域/);
  assert.throws(()=>buildAnalysisParams({kind:'microscopy',asset,channels:[],values:{method:'channel_intensity'}}),/勾选/);
});

test('IHC submits the original RGB plane, not scalar pseudocolor channels',()=>{
  const asset=projectFixture().assets[0],source=identity('image-b',2,3,1);
  const params=buildAnalysisParams({kind:'microscopy',asset,values:{method:'ihc'},source,channels:[{id:'c2',name:'Blue',source}]});
  assert.equal(params.channels,undefined);assert.equal(params.channel,undefined);assert.equal(params.asset_id,'image-b');assert.equal(params.series,1);assert.equal(params.frame,3);
});

test('line and Cellpose input bounds agree with the CPU analysis contract',()=>{
  const asset=projectFixture().assets[0],channels=[{id:'c0',source:identity()}],geometry={line:{x1:0,y1:0,x2:50,y2:20}};
  for(const values of [{method:'line_profile',samples:1},{method:'line_profile',line_width_px:102},{method:'cellpose',diameter_px:1},{method:'cellpose',diameter_px:2001},{method:'cellpose',flow_threshold:11},{method:'cellpose',cellprob_threshold:-21},{method:'cellpose',min_area:1.5}])assert.throws(()=>buildAnalysisParams({kind:'microscopy',asset,channels,geometry,values}));
  const profile=buildAnalysisParams({kind:'microscopy',asset,channels,geometry:{...geometry,roi:{x:0,y:0,width:2,height:2},background_roi:{x:0,y:0,width:2,height:2}},values:{method:'line_profile',samples:2,line_width_px:101}});
  assert.equal(profile.roi,undefined);assert.equal(profile.background_roi,undefined);assert.equal(profile.samples,2);
});

test('WB source overlays never draw an ROI from another frame in the same TIFF',t=>{
  const project=install(t),asset=project.assets[0],edit=project.settings.asset_edits['image-a'];state.analysisKind='wb';
  project.settings.analysis_rois=[{id:'old-frame',asset_id:asset.id,source:identity('image-a',0,0),x:1,y:1,width:10,height:10},{id:'current-frame',asset_id:asset.id,source:identity('image-a',0,3),x:5,y:5,width:20,height:10}];
  const markup=renderSourceOverlays({asset,edit,width:100,height:60,sources:[identity('image-a',0,3)],analysis:true});
  assert.doesNotMatch(markup,/data-roi-id="old-frame"/);assert.match(markup,/>R2<\/text>/);
});

test('direct split rendering mirrors tile positions and keeps captions and calibrated scale bars',t=>{
  t.mock.method(channelPreviews,'get',()=>({status:'ready',url:'blob:test'}));
  const asset=projectFixture().assets[0],view=viewFixture();view.mode='split';view.split.columns=2;
  const panel={...defaultEdit(asset),id:'p',channel_view:view,width_mm:104,height_mm:30,flip_h:true,rotation:20,scale_bar:{um_per_px:.5,length_um:10,color:'#ffffff'}};
  const markup=panelImageMarkup(panel,asset);
  assert.match(markup,/rotate\(20 52 15\)/);assert.match(markup,/split-panel-tile" transform="translate\(54 0\)/);
  assert.equal((markup.match(/class="panel-scale-bar"/g) || []).length,2);assert.match(markup,/fill="#202723"/);
  assert.match(markup,/font-family="DejaVu Sans"/);assert.equal((markup.match(/href="blob:test"/g) || []).length,2);
});

test('qPCR original excluded wells are unchecked and disabled rather than shown as user-included',t=>{
  install(t);state.table={canonical:true,rows:[{source_row:3,sample:'A',target:'ACTB',ct:21,included:false},{source_row:4,sample:'B',target:'ACTB',ct:22,included:true}],total_rows:2};
  const markup=renderTable({id:'table',name:'Ct.csv',kind:'table'});
  assert.match(markup,/id="qpcr-include-3"[^>]*disabled title="原表标记排除"/);assert.doesNotMatch(markup,/id="qpcr-include-3"[^>]*checked/);
  assert.match(markup,/id="qpcr-include-4"[^>]*checked/);
});

test('non-segmentation microscopy results do not claim zero cells and expose true profiles and pairs',t=>{
  install(t);const selection={persisted:true,stale:false,record:{id:'r',params:{method:'channel_overlap'},source_asset_ids:['image-a']},result:{kind:'microscopy',count:0,rows:[],pairs:[{channel_a_name:'A',channel_b_name:'B',pearson_r:.5,manders_m1:.8,status:'ok'}]}};
  let markup=renderResult(selection);assert.doesNotMatch(markup,/对象数量|0 个细胞/);assert.match(markup,/Pearson r/);assert.match(markup,/>0.5</);
  selection.record.params.method='line_profile';selection.result={kind:'microscopy',count:0,summary:{channel_count:1,samples_per_channel:3,line_length_px:2,line_length_um:null},profiles:[{channel_id:'c0',channel_name:'A',distance_px:[0,1,2],distance_um:null,values:[2,4,null]}]};
  markup=renderResult(selection);assert.match(markup,/原始线强度剖面/);assert.match(markup,/原始强度/);assert.doesNotMatch(markup,/NaN/);assert.match(markup,/每通道采样点/);assert.match(markup,/采样线长度 \/ px/);assert.doesNotMatch(markup,/>samples_per_channel</);
});

test('preview cache deduplicates requests, aborts obsolete work, and never applies a late stale result',async()=>{
  const pending=[],revoked=[];let created=0;
  const cache=new ChannelPreviewCache({fetcher:(payload,signal)=>new Promise(resolve=>pending.push({payload,signal,resolve})),createUrl:()=>`blob:${++created}`,revokeUrl:url=>revoked.push(url),maxEntries:3,maxBytes:1000,maxConcurrent:1});
  cache.beginFrame();const first=cache.get({value:1,max_size:1});assert.equal(cache.get({value:1,max_size:1}),first);cache.endFrame();await settle();assert.equal(pending.length,1);
  cache.beginFrame();const latest=cache.get({value:2,max_size:1});cache.endFrame();assert.equal(pending[0].signal.aborted,true);
  pending[0].resolve(new Blob(['old']));await settle();assert.equal(created,0);assert.equal(pending.length,2);
  pending[1].resolve(new Blob(['new']));await settle();assert.equal(latest.status,'ready');assert.equal(latest.url,'blob:1');assert.equal(cache.entries.size,1);
  cache.clear();assert.deepEqual(revoked,['blob:1']);assert.equal(cache.bytes,0);
});

test('preview cache has strict entry and decoded-surface memory bounds even when every visible item is pinned',async()=>{
  const cache=new ChannelPreviewCache({fetcher:async()=>new Blob(['123']),createUrl:()=> 'blob:ready',revokeUrl:()=>{},maxEntries:2,maxBytes:10});
  cache.beginFrame();const one=cache.get({v:1,max_size:1}),two=cache.get({v:2,max_size:1}),three=cache.get({v:3,max_size:1});cache.endFrame();await settle();
  assert.equal(cache.entries.size,2);assert.equal(three.status,'error');assert.equal(one.status,'ready');assert.equal(two.status,'error');assert.ok(cache.bytes<=10);
  cache.clear();assert.equal(cache.bytes,0);
});

test('a newest preview can replace an obsolete in-flight request at the entry bound',async()=>{
  const pending=[],cache=new ChannelPreviewCache({fetcher:(_payload,signal)=>new Promise((resolve,reject)=>{pending.push(resolve);signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')));}),createUrl:()=> 'blob:new',revokeUrl:()=>{},maxEntries:1,maxBytes:100});
  cache.beginFrame();cache.get({v:1,max_size:1});cache.endFrame();await settle();
  cache.beginFrame();const latest=cache.get({v:2,max_size:1});cache.endFrame();await settle();assert.equal(latest.status,'loading');assert.equal(cache.entries.size,1);
  pending[1](new Blob(['ok']));await settle();assert.equal(latest.status,'ready');cache.clear();
});

test('busy preview retries are bounded and cancelled recipes never retry',async()=>{
  let calls=0,waits=0;const result=await fetchChannelPreview({},new AbortController().signal,{fetcher:async()=>{calls++;return calls<3?new Response(JSON.stringify({detail:'busy'}),{status:429,headers:{'Retry-After':'1'}}):new Response('png');},delay:async()=>{waits++;}});
  assert.equal(await result.text(),'png');assert.equal(calls,3);assert.equal(waits,2);
  calls=0;await assert.rejects(()=>fetchChannelPreview({},null,{fetcher:async()=>{calls++;return new Response('busy',{status:429});},delay:async()=>{}}),/busy/);assert.equal(calls,4);
  const controller=new AbortController();calls=0;await assert.rejects(()=>fetchChannelPreview({},controller.signal,{fetcher:async()=>{calls++;return new Response('busy',{status:429});},delay:async()=>controller.abort()}),{name:'AbortError'});assert.equal(calls,1);
});

test('WB exposure choices are explicit, source-checked, and part of the scientific input identity',()=>{
  const asset=projectFixture().assets[0],rois=[
    {id:'r1',asset_id:asset.id,source:identity(),x:1,y:1,width:5,height:5,sample_id:'S1',role:'target',exposure_id:'10 s'},
    {id:'r2',asset_id:'image-b',source:identity('image-b'),x:1,y:1,width:5,height:5,sample_id:'S1',role:'target',exposure_id:'30 s'},
  ];
  const unset=buildAnalysisParams({kind:'wb',asset,rois}),chosen=buildAnalysisParams({kind:'wb',asset,rois,values:{selected_exposures:{S1:'10 s'}}});
  assert.equal(unset.selected_exposures,undefined);assert.deepEqual(chosen.selected_exposures,{S1:'10 s'});
  assert.notEqual(analysisInputSignature('wb',chosen),analysisInputSignature('wb',unset));
  assert.throws(()=>buildAnalysisParams({kind:'wb',asset,rois,values:{selected_exposures:{S1:'90 s'}}}),/所选曝光/);
});
