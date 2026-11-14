/** WB draft recipes. Every rectangle remains in original pixel coordinates. */
import {clone,uid,panelFromAsset,defaultEdit} from './domain.js';
import {sourceIdentityKey} from './channel-domain.js';

const text=value=>String(value ?? '').trim();
const median=values=>{const sorted=[...values].sort((a,b)=>a-b);return sorted.length?sorted[Math.floor(sorted.length/2)]:0;};
const finite=value=>Number.isFinite(Number(value));

export function groupBandRows(gel,{sourceWidth,sourceHeight}={}) {
  const centers=gel.lane_centers || [],bands=(gel.bands || []).filter(band=>Number.isInteger(band.lane_index)&&band.lane_index>=0&&band.lane_index<centers.length&&finite(band.y)&&band.y>=0&&band.y<sourceHeight).map(clone).sort((a,b)=>a.y-b.y);
  const rows=[],typicalHeight=median(bands.map(band=>Number(band.height_px)).filter(value=>value>0)) || Math.max(3,sourceHeight/70);
  const tolerance=Math.max(4,Math.min(sourceHeight*.035,typicalHeight*1.8));
  for(const band of bands) {
    const matching=rows.filter(row=>!row.bands.some(item=>item.lane_index===band.lane_index)&&Math.abs(row.y-band.y)<=tolerance).sort((a,b)=>Math.abs(a.y-band.y)-Math.abs(b.y-band.y))[0];
    if(matching){matching.bands.push(band);matching.y=median(matching.bands.map(item=>item.y));}
    else rows.push({id:`r${rows.length+1}`,y:Number(band.y),bands:[band],role:'ignore',protein:''});
  }
  const pitch=centers.length>1?median(centers.slice(1).map((center,index)=>center-centers[index])):sourceWidth*.6;
  return rows.sort((a,b)=>a.y-b.y).map((row,index)=>{
    const nearest=Math.min(index?row.y-rows[index-1].y:Infinity,index+1<rows.length?rows[index+1].y-row.y:Infinity);
    const heights=row.bands.map(band=>Number(band.height_px)).filter(value=>value>0);
    const widths=row.bands.map(band=>Number(band.bbox?.width)).filter(value=>value>0);
    return {...row,coverage:row.bands.length,roi_width:Math.max(5,Math.round(Math.min(pitch*.82,(median(widths)||pitch*.55)*1.35))),roi_height:Math.max(5,Math.round(Math.min(nearest*.75,2.4*(median(heights)||typicalHeight)+4)))};
  });
}

export function makeWbDraft({projectId,source,asset,gel,crop,edit,sequence,existingSamples=[]}) {
  if(!asset||!source?.asset_id)throw new Error('请先选择 WB 原始图像');
  const width=Number(asset.width_px),height=Number(asset.height_px),centers=gel?.lane_centers || [];
  if(!Number.isInteger(width)||!Number.isInteger(height)||width<5||height<5)throw new Error('原始平面尺寸无效');
  if(!centers.length||centers.some((value,index)=>!finite(value)||value<0||value>=width||(index>0&&value<=centers[index-1])))throw new Error('请先识别并校正递增的泳道位置');
  if(gel.localization?.source&&sourceIdentityKey(gel.localization.source)!==sourceIdentityKey(source))throw new Error('定位结果属于其他原始帧或通道，请重新识别');
  const inferred=new Set(gel.inferred_lanes || []);
  return {project_id:projectId,source:clone(source),asset_id:asset.id,width,height,source_sequence:sequence,source_fingerprint:wbSourceFingerprint(source,gel,crop),
    id:uid('wb-draft'),experiment_id:'',exposure_id:`${source.asset_id.slice(0,12)}:S${source.series}:F${source.frame}:C${source.channel}`,exposure_series_id:'',exposure_seconds:'',
    edit:clone(edit),crop:clone(crop),rows:groupBandRows(gel,{sourceWidth:width,sourceHeight:height}),roi_overrides:{},
    lanes:centers.map((x,index)=>({index,x:Number(x),included:!inferred.has(index),inferred:inferred.has(index),sample_id:existingSamples[index]?.sample_id || gel.lane_names?.[index] || '',group:existingSamples[index]?.group || '',biological_replicate:existingSamples[index]?.biological_replicate || '',technical_replicate:existingSamples[index]?.technical_replicate || '1'})),
    polarity:gel.polarity || 'dark',confirmed:false,chart_type:'scatter',control_group:'',statistics:'none',create_layout:true,run_quantification:true,control_mode:'current',control_draft_id:''};
}

export function wbSourceFingerprint(source,gel,crop) {
  return JSON.stringify({source,centers:gel?.lane_centers || [],bands:gel?.bands || [],crop});
}

function rectangle(values,width,height) {
  const output={};for(const key of ['x','y','width','height']){output[key]=Number(values[key]);if(!Number.isFinite(output[key]))throw new Error('ROI 坐标与面积须为有限数值');}
  if(output.x<0||output.y<0||output.width<3||output.height<3||output.x+output.width>width||output.y+output.height>height)throw new Error('ROI 须至少为 3×3 px，且完整位于原始平面内');
  return output;
}

export function draftRois(draft,{requireIdentity=true,validateGeometry=true,allowUnpaired=false}={}) {
  const rows=draft.rows.filter(row=>['target','control'].includes(row.role)),lanes=draft.lanes.filter(lane=>lane.included);
  if(!rows.length||rows.filter(row=>row.role==='control').length>1||(!allowUnpaired&&(!rows.some(row=>row.role==='target')||rows.filter(row=>row.role==='control').length!==1)))throw new Error(allowUnpaired?'请至少选择一行目标或内参，且内参最多一行':'请明确选择至少一行目标蛋白和一行内参');
  if(!lanes.length)throw new Error('请至少保留一个样本泳道');
  const proteins=rows.map(row=>text(row.protein));
  if(requireIdentity&&(proteins.some(name=>!name)||new Set(proteins).size!==proteins.length))throw new Error('请填写不重复的目标和内参蛋白名称');
  if(requireIdentity&&!text(draft.experiment_id))throw new Error('请填写实验 ID，避免跨实验配对');
  if(requireIdentity&&lanes.some(lane=>!text(lane.sample_id)||!text(lane.group)||!text(lane.biological_replicate)))throw new Error('请为每条保留泳道填写样本、组别和生物学重复 ID');
  const identities=lanes.map(lane=>JSON.stringify([text(lane.sample_id),text(lane.biological_replicate),text(lane.technical_replicate)||'1']));
  if(requireIdentity&&new Set(identities).size!==identities.length)throw new Error('样本/生物学/技术重复身份重复；同一样本重复上样请明确技术重复 ID');
  const time=draft.exposure_seconds===''||draft.exposure_seconds==null?undefined:Number(draft.exposure_seconds);
  if(time!==undefined&&(!Number.isFinite(time)||time<=0))throw new Error('曝光时间须为正的秒数');
  const rois=[];
  for(const row of rows)for(const lane of lanes) {
    const band=row.bands.find(item=>item.lane_index===lane.index),width=Number(row.roi_width),height=Number(row.roi_height);
    if(!Number.isFinite(width)||!Number.isFinite(height)||width<3||height<3||width>draft.width||height>draft.height)throw new Error('整行 ROI 宽高超出原图或小于 3 px');
    const y=band?.y ?? row.y,key=`${row.id}:${lane.index}`,override=draft.roi_overrides[key];
    const proposed=override || {x:Math.min(draft.width-width,Math.max(0,Math.round(lane.x-width/2))),y:Math.min(draft.height-height,Math.max(0,Math.round(y-height/2))),width,height};
    const geometry=validateGeometry?rectangle(proposed,draft.width,draft.height):clone(proposed);
    rois.push({...geometry,id:`${draft.id}-${row.id}-${lane.index}`,asset_id:draft.source.asset_id,source:clone(draft.source),role:row.role,protein:text(row.protein),
      sample_id:text(lane.sample_id),group:text(lane.group),biological_replicate:text(lane.biological_replicate),technical_replicate:text(lane.technical_replicate)||'1',
      experiment_id:text(draft.experiment_id),exposure_id:text(draft.exposure_id),exposure_seconds:time,exposure_series_id:text(draft.exposure_series_id)||'default',included:true,
      wb_draft_id:draft.id,wb_row_id:row.id,lane_index:lane.index,
      detection:{kind:band?'detected_candidate':'expected_position_no_detected_band',score:band?.score ?? null,reviewed:Boolean(draft.confirmed)}});
  }
  for(const target of rois.filter(roi=>roi.role==='target')) {
    const control=rois.find(roi=>roi.role==='control'&&roi.lane_index===target.lane_index);if(control)target.control_roi_id=control.id;
  }
  // A target and its control cannot share source pixels, including after edits.
  for(const target of rois.filter(roi=>roi.role==='target')) {
    const control=rois.find(roi=>roi.id===target.control_roi_id);
    if(validateGeometry&&control&&Math.min(target.x+target.width,control.x+control.width)>Math.max(target.x,control.x)&&Math.min(target.y+target.height,control.y+control.height)>Math.max(target.y,control.y))throw new Error('目标与内参 ROI 相互重叠，请调整行选择或矩形');
  }
  return rois;
}

export function attachExistingControls(draft,rois,available) {
  if(!draft.control_draft_id)throw new Error('请选择已经核对的内参识别方案');
  if(rois.some(roi=>roi.role==='control'))throw new Error('使用已有内参时，请把当前图的内参行设为不用于本次定量');
  if(!rois.some(roi=>roi.role==='target'))throw new Error('当前原图需要至少一行目标蛋白');
  const output=rois.map(clone),controls=[];
  for(const target of output) {
    const fields=['experiment_id','sample_id','group','biological_replicate','technical_replicate'];
    if(fields.some(key=>!text(target[key])))throw new Error('跨来源内参配对需要完整的实验、样本、组别和重复身份');
    const candidates=available.filter(roi=>roi.wb_draft_id===draft.control_draft_id&&roi.role==='control'&&roi.included!==false&&fields.every(key=>text(roi[key])===text(target[key])));
    if(candidates.length!==1)throw new Error(`${target.sample_id} 在所选内参识别方案中${candidates.length?'有多个匹配':'没有匹配'}；请核对实验、样本、组别和重复身份`);
    const control=candidates[0];
    if(sourceIdentityKey(control.source)===sourceIdentityKey(target.source)&&['x','y','width','height'].every(key=>control[key]===target[key]))throw new Error('目标与内参不能使用同一源区域');
    target.control_roi_id=control.id;if(!controls.some(roi=>roi.id===control.id))controls.push(clone(control));
  }
  return {rois:output,controls};
}

export function parseLaneTable(input,lanes) {
  let lines=text(input).split(/\r?\n/).filter(line=>line.trim()).map(line=>line.split('\t').map(text));
  if(lines[0]&&/样本|sample/i.test(lines[0][0])&&/组|group/i.test(lines[0][1] || ''))lines=lines.slice(1);
  const selected=lanes.filter(lane=>lane.included);
  if(lines.length!==selected.length)throw new Error(`粘贴数据应有 ${selected.length} 行，对应当前保留泳道`);
  if(lines.some(row=>row.length<3||!row[0]||!row[1]||!row[2]))throw new Error('每行至少三列：样本 ID、组别、生物学重复 ID；第四列为可选技术重复 ID');
  const result=clone(lanes);selected.forEach((lane,index)=>{Object.assign(result.find(item=>item.index===lane.index),{sample_id:lines[index][0],group:lines[index][1],biological_replicate:lines[index][2],technical_replicate:lines[index][3] || '1'});});return result;
}

export function wbLayoutPanels(project,asset,draft,rois,{chartPanel=null}={}) {
  const allWidth=Number(project.figure.width_mm),allHeight=Number(project.figure.height_mm);
  const margin=9,gap=8,availableWidth=Math.max(30,allWidth-2*margin),chartWidth=chartPanel?Math.min(76,availableWidth*.44):0;
  const imageWidth=chartPanel?availableWidth-chartWidth-gap:availableWidth;
  const panels=[],grouped=new Map();
  for(const roi of rois){const key=JSON.stringify([sourceIdentityKey(roi.source),roi.role,roi.protein,roi.wb_row_id]);if(!grouped.has(key))grouped.set(key,[]);grouped.get(key).push(roi);}
  const strips=[...grouped.values()].sort((a,b)=>(a[0].role==='control')-(b[0].role==='control')||a[0].y-b[0].y);
  for(const members of strips) {
    const first=members[0],source=first.source,sourceAsset=(project.assets || []).find(item=>item.id===source.asset_id) || (asset.id===source.asset_id?asset:null);
    if(!sourceAsset)throw new Error('WB 版式引用的原件不存在');
    const ownSource=sourceIdentityKey(source)===sourceIdentityKey(draft.source),sourceDraft=project.settings?.wb_drafts?.[first.wb_draft_id],sourceWidth=ownSource?draft.width:sourceDraft?.width || sourceAsset.width_px,sourceHeight=ownSource?draft.height:sourceDraft?.height || sourceAsset.height_px,padding=2;
    const left=Math.max(0,Math.floor(Math.min(...members.map(roi=>roi.x))-padding)),right=Math.min(sourceWidth,Math.ceil(Math.max(...members.map(roi=>roi.x+roi.width))+padding));
    const top=Math.max(0,Math.floor(Math.min(...members.map(roi=>roi.y))-padding)),bottom=Math.min(sourceHeight,Math.ceil(Math.max(...members.map(roi=>roi.y+roi.height))+padding));
    const crop={x:left,y:top,width:right-left,height:bottom-top},ordered=[...members].sort((a,b)=>a.x-b.x);
    const edit={...clone(ownSource?draft.edit:project.settings?.asset_edits?.[source.asset_id] || defaultEdit(sourceAsset)),crop,series:source.series,frame:source.frame,channel:source.channel,page:source.frame,rotation:0,flip_h:false,flip_v:false,
      channel_view:null,gel:{lane_centers:ownSource?draft.lanes.map(lane=>lane.x):ordered.map(roi=>roi.x+roi.width/2),lane_names:ownSource?draft.lanes.map(lane=>lane.included?text(lane.sample_id):''):ordered.map(roi=>text(roi.sample_id)),marker_points:[],marker_unit:'kDa',hidden_lanes:[]}};
    const panel=panelFromAsset(project,sourceAsset,edit);panel.title=text(first.protein);panel.wb_draft_id=draft.id;panel.wb_row_id=first.wb_row_id;
    panel.wb_provenance={source:clone(source),roi_ids:members.map(roi=>roi.id),continuous_source_crop:true,excluded_lanes_retained_in_image:true};
    panel.width_mm=imageWidth;panel.height_mm=imageWidth*crop.height/crop.width;panels.push(panel);
  }
  const ppi=Number(project.settings?.min_ppi)>0?Number(project.settings.min_ppi):300;
  const sourceLimitedWidth=Math.min(imageWidth,...panels.map(panel=>panel.crop.width*25.4/ppi));
  panels.forEach(panel=>{panel.width_mm=sourceLimitedWidth;panel.height_mm=sourceLimitedWidth*panel.crop.height/panel.crop.width;});
  const contentHeight=panels.reduce((sum,panel)=>sum+panel.height_mm,0)+(panels.length-1)*12;
  const maxHeight=Math.max(20,allHeight-2*margin-8),scale=Math.min(1,maxHeight/Math.max(1,contentHeight));
  let y=margin+6;panels.forEach((panel,index)=>{panel.width_mm*=scale;panel.height_mm*=scale;panel.x_mm=margin;panel.y_mm=y;panel.label=index===0?'A':'';y+=panel.height_mm+12*scale;});
  if(chartPanel){chartPanel.x_mm=margin+sourceLimitedWidth*scale+gap;chartPanel.y_mm=margin+6;chartPanel.width_mm=chartWidth;chartPanel.height_mm=Math.min(maxHeight,chartWidth*.75);chartPanel.label='B';chartPanel.wb_draft_id=draft.id;panels.push(chartPanel);}
  return panels;
}

export function reviewInputSignature(rois,polarity) {return JSON.stringify({rois,polarity});}

export function applyExposureDecisions(rois,review,selectedGroups=null) {
  const decisions=new Map((review.roi_decisions || []).filter(item=>selectedGroups===null||selectedGroups.includes(item.group_id)).map(item=>[item.roi_id,item]));
  return rois.map(roi=>{const decision=decisions.get(roi.id);if(!decision)return clone(roi);if(roi.included===false&&decision.included)throw new Error('复核之后该 ROI 已被排除，不能自动重新纳入');return {...clone(roi),included:Boolean(decision.included),...(roi.role==='target'?{control_roi_id:decision.control_roi_id || ''}:{}),exposure_review:{group_id:decision.group_id,roi_signature:review.roi_signature,method:review.method.ranking}};});
}
