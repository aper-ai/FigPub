import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {groupBandRows,makeWbDraft,draftRois,attachExistingControls,parseLaneTable,wbLayoutPanels,applyExposureDecisions} from '../web/modules/wb-domain.js';
const require=createRequire(import.meta.url),engine=require('../web/gel-engine.js');

function fixture(){
  const source={asset_id:'asset',series:1,frame:2,channel:3},asset={id:'asset',kind:'image',width_px:400,height_px:240};
  const gel={lane_centers:[50,150,250,350],lane_names:[],bands:[60,180].flatMap(y=>[0,1,2,3].map(lane_index=>({lane_index,x:50+lane_index*100,y,height_px:8,score:12,bbox:{x:30+lane_index*100,y:y-4,width:40,height:8}}))),localization:{source}};
  const crop={x:0,y:0,width:400,height:240},edit={crop,gel,display:{low:null,high:null,gamma:1,invert:false}};
  const draft=makeWbDraft({projectId:'p',source,asset,gel,crop,edit,sequence:4});
  draft.rows[0].role='target';draft.rows[0].protein='Target';draft.rows[1].role='control';draft.rows[1].protein='ACTB';draft.experiment_id='experiment';draft.confirmed=true;
  draft.lanes.forEach((lane,i)=>Object.assign(lane,{sample_id:`S${i}`,group:i<2?'Control':'Treated',biological_replicate:`bio${i}`,technical_replicate:'1'}));
  return {draft,asset,source,gel,crop,edit,project:{id:'p',panels:[],figure:{width_mm:180,height_mm:120}}};
}

test('band rows preserve original frames and generate paired rectangles without mutating localization',()=>{
  const {draft,gel,source}=fixture(),before=structuredClone(gel),rois=draftRois(draft);
  assert.equal(rois.length,8);assert.deepEqual(gel,before);
  assert.ok(rois.every(roi=>JSON.stringify(roi.source)===JSON.stringify(source)));
  for(const target of rois.filter(roi=>roi.role==='target')){const control=rois.find(roi=>roi.id===target.control_roi_id);assert.equal(control.lane_index,target.lane_index);assert.equal(control.sample_id,target.sample_id);}
});

test('default draft does not invent biological identities or target assignments',()=>{
  const {source,asset,gel,crop,edit}=fixture();
  const draft=makeWbDraft({projectId:'p',source,asset,gel,crop,edit,sequence:0});
  assert.ok(draft.rows.every(row=>row.role==='ignore'));assert.ok(draft.lanes.every(lane=>lane.group===''&&lane.biological_replicate===''));
  assert.throws(()=>draftRois(draft),/选择至少一行/);
});

test('missing bands yield explicitly reviewed reference windows, not invented detections',()=>{
  const {draft}=fixture();draft.rows[0].bands=draft.rows[0].bands.filter(band=>band.lane_index!==2);
  const rois=draftRois(draft),missing=rois.find(roi=>roi.role==='target'&&roi.lane_index===2);
  assert.equal(missing.detection.kind,'expected_position_no_detected_band');assert.equal(missing.detection.score,null);
});

test('individual rectangle edits survive while overlapping target and control is rejected',()=>{
  const {draft}=fixture();draft.roi_overrides[`${draft.rows[0].id}:0`]={x:20,y:45,width:57,height:23};
  assert.equal(draftRois(draft)[0].width,57);
  draft.roi_overrides[`${draft.rows[1].id}:0`]={x:20,y:45,width:57,height:23};
  assert.throws(()=>draftRois(draft),/相互重叠/);
});

test('out-of-bounds coordinates, duplicate sample identities, and source switches reject explicitly',()=>{
  const {draft,source,asset,gel,crop,edit}=fixture();draft.roi_overrides[`${draft.rows[0].id}:0`]={x:390,y:45,width:57,height:23};
  assert.throws(()=>draftRois(draft),/原始平面/);
  draft.roi_overrides={};Object.assign(draft.lanes[1],{sample_id:draft.lanes[0].sample_id,biological_replicate:draft.lanes[0].biological_replicate});
  assert.throws(()=>draftRois(draft),/身份重复/);
  assert.throws(()=>makeWbDraft({projectId:'p',source:{...source,frame:5},asset,gel,crop,edit}),/其他原始帧/);
});

test('TSV input maps only included lanes and keeps biological repeats explicit',()=>{
  const {draft}=fixture();draft.lanes[1].included=false;
  const result=parseLaneTable('样本 ID\t组别\t生物学重复 ID\t技术重复 ID\nA\tControl\tB1\tT1\nB\tTreat\tB2\tT1\nC\tTreat\tB3\tT1',draft.lanes);
  assert.equal(result[0].sample_id,'A');assert.equal(result[2].sample_id,'B');assert.equal(result[1].sample_id,'S1');
  assert.throws(()=>parseLaneTable('A\tControl',draft.lanes),/应有 3 行/);
});

test('continuous strip layout keeps intervening excluded lanes and explicit original source',()=>{
  const {draft,project,asset}=fixture();draft.lanes[1].included=false;
  const rois=draftRois(draft),chart={id:'chart',asset_id:'chart-image',crop:{x:0,y:0,width:800,height:600}};
  const panels=wbLayoutPanels(project,asset,draft,rois,{chartPanel:chart});
  assert.equal(panels.length,3);assert.equal(panels[0].title,'Target');assert.equal(panels[1].title,'ACTB');
  assert.equal(panels[0].channel,3);assert.equal(panels[0].frame,2);assert.equal(panels[0].series,1);
  assert.ok(panels[0].crop.x<150&&panels[0].crop.x+panels[0].crop.width>150);assert.deepEqual(panels[0].gel.hidden_lanes,[]);
  assert.equal(panels[0].wb_provenance.continuous_source_crop,true);assert.equal(panels[2].id,'chart');
  assert.ok(panels.slice(0,2).every(panel=>panel.crop.width/(panel.width_mm/25.4)>=300-1e-9));
  assert.ok(panels.every(panel=>panel.x_mm>=0&&panel.y_mm>=0&&panel.x_mm+panel.width_mm<=180&&panel.y_mm+panel.height_mm<=120));
});

test('inferred lanes are initially excluded and sparse band rows remain visible',()=>{
  const {source,asset,gel,crop,edit}=fixture();gel.inferred_lanes=[2];gel.bands.push({lane_index:0,y:120,height_px:5});
  const draft=makeWbDraft({projectId:'p',source,asset,gel,crop,edit});assert.equal(draft.lanes[2].included,false);
  assert.equal(groupBandRows(gel,{sourceWidth:400,sourceHeight:240}).length,3);
});

test('separate original target and control drafts match true identities and keep each source crop',()=>{
  const {draft,asset,project}=fixture(),controlDraft=structuredClone(draft);
  controlDraft.id='control-draft';controlDraft.source={asset_id:'control-asset',series:2,frame:1,channel:0};controlDraft.asset_id='control-asset';controlDraft.rows[0].role='ignore';
  const controls=draftRois(controlDraft,{allowUnpaired:true});assert.ok(controls.every(roi=>roi.role==='control'));
  draft.rows[1].role='ignore';draft.control_mode='existing';draft.control_draft_id=controlDraft.id;
  const targets=draftRois(draft,{allowUnpaired:true}),linked=attachExistingControls(draft,targets,controls);
  assert.equal(linked.rois.length,4);assert.equal(linked.controls.length,4);
  assert.ok(linked.rois.every(roi=>linked.controls.some(control=>control.id===roi.control_roi_id&&control.biological_replicate===roi.biological_replicate)));
  project.assets=[asset,{...asset,id:'control-asset'}];project.settings={wb_drafts:{'control-draft':controlDraft}};
  const panels=wbLayoutPanels(project,asset,draft,[...linked.rois,...linked.controls]);
  assert.equal(panels[0].asset_id,'asset');assert.equal(panels[1].asset_id,'control-asset');assert.equal(panels[1].series,2);assert.equal(panels[1].frame,1);
  controls[0].experiment_id='another-experiment';assert.throws(()=>attachExistingControls(draft,targets,controls),/没有匹配/);
});

test('control-only preparation is explicit and does not meet quantitative pairing requirements',()=>{
  const {draft}=fixture();draft.rows[0].role='ignore';
  assert.throws(()=>draftRois(draft),/至少一行目标/);
  const controls=draftRois(draft,{allowUnpaired:true});assert.equal(controls.length,4);assert.ok(controls.every(roi=>roi.role==='control'));
});

test('exposure application only changes selected cohorts and never revives explicit exclusions',()=>{
  const rois=[{id:'a',role:'target',included:true},{id:'b',role:'target',included:true},{id:'c',role:'target',included:false}];
  const review={roi_signature:'signature',method:{ranking:'complete_cohort'},roi_decisions:[{roi_id:'a',included:false,group_id:'one'},{roi_id:'b',included:true,control_roi_id:'control',group_id:'two'}]};
  const out=applyExposureDecisions(rois,review,['one']);assert.equal(out[0].included,false);assert.equal(out[1].exposure_review,undefined);assert.equal(rois[0].included,true);
  review.roi_decisions.push({roi_id:'c',included:true,group_id:'three'});assert.throws(()=>applyExposureDecisions(rois,review),/不能自动重新纳入/);
});

test('localization candidate rectangles scale to actual raw pixels',()=>{
  const width=160,height=100,data=new Float32Array(width*height);
  for(let y=0;y<height;y++)for(let x=0;x<width;x++)data[y*width+x]=.9-[35,80,125].reduce((value,center)=>value+.6*Math.exp(-(((x-center)/7)**2)-((y-40)/3)**2),0);
  const result=engine.analyze({width,height,data,source_width:640,source_height:400},{lane_count:3});
  assert.equal(result.lane_centers.length,3);assert.ok(result.bands.length>=3);
  assert.ok(result.bands.every(band=>band.bbox.x>=0&&band.bbox.y>=0&&band.bbox.x+band.bbox.width<=640&&band.bbox.y+band.bbox.height<=400));
  assert.ok(result.bands.every(band=>band.y>140&&band.y<180));
});
