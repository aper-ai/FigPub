/** Guided WB detection → reviewed original-pixel ROIs → figure and real chart. */
import {state,emit,notify,mutate,selectedAsset,selectedEdit,currentRawSource,flushSave,analysisSelection} from './state.js';
import {clone,escapeHtml as e,formatValue,fullCrop,uid} from './domain.js';
import {sourceIdentityKey} from './channel-domain.js';
import {loadChannelDescriptor} from './channels.js';
import {detectGel} from './gel.js';
import {runAnalysis,plotResults} from './analysis.js';
import {request} from './api.js';
import {makeWbDraft,draftRois,attachExistingControls,parseLaneTable,wbLayoutPanels,wbSourceFingerprint,reviewInputSignature,applyExposureDecisions} from './wb-domain.js';

const input=(id,value,attributes='',type='text')=>`<input id="${id}" type="${type}" value="${e(value ?? '')}" ${type==='number'?'step="any"':''} ${attributes}>`;
const draftField=(draft,key,label,type='text')=>`<label class="field"><span>${label}</span>${input(`wb-draft-${key}`,draft[key],`data-wb-draft-field="${key}"`,type)}</label>`;
const inlineError=message=>message?`<div class="wb-error" role="alert">${e(message)}</div>`:'';

export function renderWbEntry() {
  return '<button class="button full primary" data-action="open-wb-workflow">WB 自动出图向导</button><p class="microcopy">识别条带后，一次确认目标、内参与样本，生成可编辑 ROI 和完整图稿。</p>';
}
export function renderExposureReviewEntry() {
  return '<button class="button full" data-action="open-wb-exposures">比较多曝光质量</button><p class="microcopy">同时核对目标和内参，保留全部原始曝光，明确应用后才改变统计纳入。</p>';
}

export async function openWbWorkflow({autoDetect=false}={}) {
  const asset=selectedAsset();if(asset?.kind!=='image'){notify('请先导入并选择一张 WB 原始图像','info');return;}
  const originalSource=clone(currentRawSource());state.selectedAssetId=originalSource.asset_id;state.selectedPanelId=null;state.sourcePointerIdentity=originalSource;
  state.tab='gel';state.selectedAnnotationId=null;state.sourceRoi=null;emit();
  try {
    const projectId=state.project.id,source=clone(currentRawSource());
    const descriptor=await loadChannelDescriptor(source);
    if(state.project.id!==projectId||sourceIdentityKey(currentRawSource())!==sourceIdentityKey(source))return;
    if(autoDetect||!selectedEdit().gel?.bands?.length)await detectGel();
    if(state.project.id!==projectId||sourceIdentityKey(currentRawSource())!==sourceIdentityKey(source))return;
    const edit=selectedEdit(),crop=edit.crop || fullCrop(descriptor),gel=edit.gel;
    const actualAsset=state.project.assets.find(item=>item.id===source.asset_id);
    const fresh=makeWbDraft({projectId,source,asset:{...actualAsset,width_px:descriptor.width_px,height_px:descriptor.height_px},gel,crop,edit,sequence:state.mutationSequence});
    const previous=Object.values(state.project.settings.wb_drafts || {}).reverse().find(item=>item.source_fingerprint===fresh.source_fingerprint);
    const draft=previous?{...fresh,...clone(previous)}:fresh;
    if(previous){for(const roi of state.project.settings.analysis_rois.filter(item=>item.wb_draft_id===draft.id)){draft.roi_overrides[`${roi.wb_row_id}:${roi.lane_index}`]={x:roi.x,y:roi.y,width:roi.width,height:roi.height};const lane=draft.lanes.find(item=>item.index===roi.lane_index);if(lane)for(const key of ['sample_id','group','biological_replicate','technical_replicate'])lane[key]=roi[key] || '';const row=draft.rows.find(item=>item.id===roi.wb_row_id);if(row){row.protein=roi.protein;row.role=roi.role;}}}
    draft.confirmed=false;
    state.modal={type:'wb-workflow',draft,error:'',paste:'',project_id:projectId};emit();
  } catch(error){notify(`WB 条带识别未完成：${error.message}`,'error');}
}

function sourcePreview(draft,rois=[]) {
  const query=new URLSearchParams({max_size:'1400',series:draft.source.series,frame:draft.source.frame,channel:draft.source.channel});
  const stroke=Math.max(1,draft.width/450);
  const boxes=rois.filter(roi=>['x','y','width','height'].every(key=>Number.isFinite(roi[key]))&&roi.width>0&&roi.height>0).map(roi=>`<g><rect x="${roi.x}" y="${roi.y}" width="${roi.width}" height="${roi.height}" fill="${roi.role==='control'?'#cb7930':'#078477'}" fill-opacity=".08" stroke="${roi.role==='control'?'#cb7930':'#078477'}" stroke-width="${stroke}" ${roi.detection.kind==='expected_position_no_detected_band'?'stroke-dasharray="6 3"':''}/><title>${e(`${roi.protein || roi.role} · 泳道 ${roi.lane_index+1} · ${roi.detection.kind==='detected_candidate'?'检测候选':'未检出，参考位置窗口'}`)}</title></g>`).join('');
  return `<div class="wb-source-preview"><svg viewBox="0 0 ${draft.width} ${draft.height}" role="img" aria-label="WB 原图与待确认测量区域"><image href="/api/assets/${e(draft.source.asset_id)}/preview?${query}" width="${draft.width}" height="${draft.height}"/>${draft.rows.filter(row=>row.role==='ignore').map(row=>`<line x1="0" x2="${draft.width}" y1="${row.y}" y2="${row.y}" stroke="#4d6673" stroke-width="${stroke}" opacity=".35"/>`).join('')}${boxes}</svg></div>`;
}

function renderDraft() {
  const {draft,error,paste}=state.modal;let rois=[],geometryError='';try{rois=draftRois(draft,{requireIdentity:false,validateGeometry:false,allowUnpaired:true});draftRois(draft,{requireIdentity:false,allowUnpaired:true});}catch(problem){geometryError=problem.message;}
  const controlDrafts=Object.values(state.project.settings.wb_drafts || {}).filter(item=>item.id!==draft.id&&state.project.settings.analysis_rois.some(roi=>roi.wb_draft_id===item.id&&roi.role==='control'&&roi.included!==false));
  const controlPicker=`<div class="field-grid"><label class="field"><span>内参来源</span><select id="wb-draft-control_mode" data-wb-draft-field="control_mode"><option value="current" ${draft.control_mode!=='existing'?'selected':''}>内参在当前原图</option><option value="existing" ${draft.control_mode==='existing'?'selected':''}>使用另一原图已准备的内参</option></select></label>${draft.control_mode==='existing'?`<label class="field"><span>选择内参识别方案</span><select id="wb-draft-control_draft_id" data-wb-draft-field="control_draft_id"><option value="">请选择已核对的内参</option>${controlDrafts.map(item=>`<option value="${e(item.id)}" ${draft.control_draft_id===item.id?'selected':''}>${e(item.experiment_id || '未填实验')} · ${e(state.project.assets.find(asset=>asset.id===item.asset_id)?.name || item.asset_id)} · ${e(item.exposure_id)}</option>`).join('')}</select></label>`:''}</div><p class="microcopy">目标与内参分在不同文件时，先在内参图选择内参行并取消“运行定量”，保存 ROI；再在目标图选择该内参识别方案。按实验、样本、组别及重复 ID 精确配对，不按泳道顺序猜测。</p>`;
  const rows=draft.rows.map(row=>`<tr><td>${formatValue(row.y)}</td><td>${row.coverage ?? row.bands.length} / ${draft.lanes.length}</td><td><select id="wb-row-${row.id}-role" data-wb-row="${row.id}" data-wb-row-field="role">${[['ignore','不用于本次定量'],['target','目标蛋白'],['control','内参']].map(([value,label])=>`<option value="${value}" ${row.role===value?'selected':''}>${label}</option>`).join('')}</select></td><td>${input(`wb-row-${row.id}-protein`,row.protein,`data-wb-row="${row.id}" data-wb-row-field="protein" placeholder="蛋白名称"`)}</td><td>${input(`wb-row-${row.id}-width`,row.roi_width,`data-wb-row="${row.id}" data-wb-row-field="roi_width" min="3"`,'number')}</td><td>${input(`wb-row-${row.id}-height`,row.roi_height,`data-wb-row="${row.id}" data-wb-row-field="roi_height" min="3"`,'number')}</td></tr>`).join('');
  const lanes=draft.lanes.map(lane=>`<tr><td><input id="wb-lane-${lane.index}-included" type="checkbox" data-wb-lane="${lane.index}" data-wb-lane-field="included" ${lane.included?'checked':''}></td><td>${lane.index+1}${lane.inferred?'<small>间距推断</small>':''}</td>${[['sample_id','样本 ID'],['group','组别'],['biological_replicate','独立实验 ID'],['technical_replicate','技术重复 ID']].map(([field,placeholder])=>`<td>${input(`wb-lane-${lane.index}-${field}`,lane[field],`data-wb-lane="${lane.index}" data-wb-lane-field="${field}" placeholder="${placeholder}"`)}</td>`).join('')}</tr>`).join('');
  const rectangles=rois.map(roi=>`<tr><td>${e(roi.protein || roi.role)} · ${roi.lane_index+1}${roi.detection.kind==='expected_position_no_detected_band'?'<small class="wb-missing">未检出 · 参考窗口</small>':''}</td>${['x','y','width','height'].map(field=>`<td>${input(`wb-rect-${roi.wb_row_id}-${roi.lane_index}-${field}`,roi[field],`data-wb-rectangle="${roi.wb_row_id}:${roi.lane_index}" data-wb-rectangle-field="${field}"`,'number')}</td>`).join('')}</tr>`).join('');
  const content=`<div class="wb-workflow">${inlineError(error)}<p class="modal-intro">条带位置已识别。确认目标蛋白、内参和样本信息后，生成定量 ROI、结果与图稿。</p>${sourcePreview(draft,rois)}<section><h3>1 · 选择条带行</h3><p class="microcopy">绿色表示目标蛋白，橙色表示内参。虚线区域为未检出条带的候选位置，须对照原图复核。</p><div class="data-table-wrap"><table class="data-table wb-row-table"><thead><tr><th>原图 Y / px</th><th>检出泳道数</th><th>用途</th><th>蛋白名称</th><th>ROI 宽 / px</th><th>ROI 高 / px</th></tr></thead><tbody>${rows}</tbody></table></div><button class="button small" data-action="wb-add-row">补充一行参考窗口</button>${geometryError?`<p class="microcopy">${e(geometryError)}</p>`:''}</section><section><h3>2 · 实验与样本</h3>${controlPicker}<div class="field-grid">${draftField(draft,'experiment_id','实验 ID')}${draftField(draft,'exposure_id','曝光 ID · 同次采集保持一致')}${draftField(draft,'exposure_series_id','曝光比较系列 · 可选')}${draftField(draft,'exposure_seconds','曝光时间 / 秒 · 可选','number')}</div><div class="data-table-wrap"><table class="data-table wb-lane-table"><thead><tr><th>纳入</th><th>泳道</th><th>样本 ID</th><th>组别</th><th>生物学重复 ID</th><th>技术重复 ID</th></tr></thead><tbody>${lanes}</tbody></table></div><details id="wb-paste"><summary>从表格批量粘贴样本信息</summary><label class="field"><span>按保留泳道顺序：样本 ID、组别、生物学重复 ID、技术重复 ID（可选），列之间用 Tab</span><textarea id="wb-paste-text" data-wb-paste rows="4" placeholder="从 Excel 复制四列，粘贴到这里">${e(paste)}</textarea></label><button class="button small" data-action="wb-apply-paste">填入样本表</button></details></section><section><h3>3 · 检查区域与输出</h3><details id="wb-rectangles"><summary>精确调整 ${rois.length} 个定量 ROI</summary><div class="data-table-wrap wb-rectangle-table"><table class="data-table"><thead><tr><th>蛋白 / 泳道</th><th>X</th><th>Y</th><th>宽</th><th>高</th></tr></thead><tbody>${rectangles}</tbody></table></div></details><div class="field-grid"><label class="field"><span>条带极性</span><select id="wb-draft-polarity" data-wb-draft-field="polarity"><option value="dark" ${draft.polarity==='dark'?'selected':''}>深色条带 / 浅背景</option><option value="bright" ${draft.polarity==='bright'?'selected':''}>亮色条带 / 深背景</option></select></label><label class="field"><span>统计图</span><select id="wb-draft-chart_type" data-wb-draft-field="chart_type">${[['scatter','散点 + 均值'],['bar','柱状图 + 实际重复点'],['box','箱线图']].map(([id,label])=>`<option value="${id}" ${draft.chart_type===id?'selected':''}>${label}</option>`).join('')}</select></label>${draftField(draft,'control_group','对照组 · 可选，用于相对表达')}</div><label class="checkbox-label"><input id="wb-draft-run_quantification" type="checkbox" data-wb-draft-field="run_quantification" ${draft.run_quantification?'checked':''}>运行原始值定量并加入统计图</label><label class="checkbox-label"><input id="wb-draft-confirmed" type="checkbox" data-wb-draft-field="confirmed" ${draft.confirmed?'checked':''}>已核对区域、目标／内参和实际样本身份</label><p class="microcopy">目标和内参分别裁出连续原图横条，保留横条内所有像素，不删除或重排中间泳道。统计按独立生物学重复聚合；单个重复不生成误差条或显著性结论。</p></section></div>`;
  return {title:'WB 自动出图 · 条带复核',content,footerContent:'<button class="button" data-action="close-modal">返回原图</button><button class="button primary" data-action="wb-create-draft">生成定量 ROI 与图稿</button>'};
}

function currentDraftIsValid(draft) {
  if(state.project.id!==draft.project_id||sourceIdentityKey(currentRawSource())!==sourceIdentityKey(draft.source)||wbSourceFingerprint(currentRawSource(),selectedEdit().gel,selectedEdit().crop)!==draft.source_fingerprint)throw new Error('原图、帧、裁切或定位结果已经改变，请重新打开 WB 向导');
}

function placeDraftPanels(project,asset,draft,rois,chartPanel=null) {
  const others=project.panels.filter(panel=>panel.wb_draft_id!==draft.id&&panel.id!==chartPanel?.id);
  const output=wbLayoutPanels(project,asset,draft,rois,{chartPanel}),shift=others.length?Math.max(...others.map(panel=>panel.y_mm+panel.height_mm))+12:0;
  output.forEach(panel=>panel.y_mm+=shift);
  const needed=Math.max(project.figure.height_mm,...output.map(panel=>panel.y_mm+panel.height_mm+9));
  if(needed>2000)throw new Error('现有画布没有容纳新 WB 图组的空间，请新建项目或调整版式');
  project.figure.height_mm=needed;project.panels=[...others,...output];return output;
}

async function createDraft() {
  const modal=state.modal;if(modal?.type!=='wb-workflow'||state.wbWorking)return;
  const taskToken=uid('wb-task');
  try {
    const draft=clone(modal.draft);currentDraftIsValid(draft);
    if(!draft.confirmed)throw new Error('请先核对并勾选区域和样本身份确认');
    let rois=draftRois(draft,{requireIdentity:Boolean(draft.run_quantification),allowUnpaired:draft.control_mode==='existing'||!draft.run_quantification}),linkedControls=[];
    if(rois.some(roi=>!roi.protein))throw new Error('请填写所选行的蛋白名称');
    if(draft.control_mode==='existing'){const linked=attachExistingControls(draft,rois,state.project.settings.analysis_rois);rois=linked.rois;linkedControls=linked.controls;}
    const layoutRois=[...rois,...linkedControls],others=state.project.settings.analysis_rois.filter(roi=>roi.wb_draft_id!==draft.id||!roi.wb_row_id);
    if(others.length+rois.length>5000)throw new Error('本项目的 WB ROI 已超过 5000，请拆分项目');
    if(rois.some(roi=>others.some(other=>sourceIdentityKey(other.source || {asset_id:other.asset_id,series:0,frame:0,channel:0})===sourceIdentityKey(roi.source)&&['x','y','width','height'].every(key=>Number(other[key])===roi[key]))))throw new Error('这些源像素已经存在其他测量 ROI；请编辑已有识别方案或移除重复区域');
    const asset=state.project.assets.find(item=>item.id===draft.asset_id),projectId=state.project.id;
    const staged={...state.project,figure:clone(state.project.figure),panels:state.project.panels.map(clone)};placeDraftPanels(staged,asset,draft,layoutRois);
    mutate(project=>{project.settings.analysis_rois=[...others,...rois];project.settings.wb_drafts ||= {};project.settings.wb_drafts[draft.id]=clone(draft);
      project.settings.analysis_params={...state.analysisParams,background:'border',polarity:draft.polarity,control_group:draft.control_group,test:'none',wb_draft_scope_id:draft.id};project.settings.analysis_chart_type=draft.chart_type;
      project.panels=staged.panels;project.figure=staged.figure;
    },'生成 WB 测量区域和连续原图版式');
    state.modal=null;state.analysisKind='wb';state.selectedPanelId=null;state.sourcePointerIdentity=clone(draft.source);state.selectedAssetId=draft.asset_id;state.tab=draft.run_quantification?'analysis':'compose';state.wbWorking=true;state.wbTaskToken=taskToken;emit();
    if(draft.run_quantification){
      await runAnalysis();
      if(state.project.id!==projectId)return;
      const selection=analysisSelection('wb');
      if(selection.stale||!selection.persisted||!selection.result?.groups?.length){notify('ROI 和条带图已生成；当前定量未产生可用组统计，请查看结果中的质量提示','info');return;}
      const recordId=selection.record.id;
      await plotResults();
      if(state.project.id!==projectId)return;
      const panel=state.project.panels.find(item=>item.id===state.selectedPanelId&&item.analysis_source?.measurement_id===recordId);
      if(!panel){notify('条带图已保留，结果图没有成功加入；可在分析页重试','info');return;}
      mutate(project=>{const chart=project.panels.find(item=>item.id===panel.id);const created=placeDraftPanels(project,asset,draft,layoutRois,chart);state.selectedPanelId=created[0].id;},'组合目标蛋白、内参与统计图');
    }
    state.selectedAssetId=draft.asset_id;state.sourcePointerIdentity=clone(draft.source);state.tab='compose';emit();notify('WB 图组已生成，ROI、裁切和所有面板仍可编辑');
  } catch(error){if(state.modal===modal){modal.error=error.message;emit();}else notify(`WB 图组未完整生成：${error.message}`,'error');}
  finally{if(state.wbTaskToken===taskToken){state.wbWorking=false;state.wbTaskToken=null;}emit();}
}

export function openExposureReview() {
  const rois=state.project.settings.analysis_rois;if(!rois.length){notify('请先用 WB 向导或手动创建定量 ROI','info');return;}
  state.modal={type:'wb-exposures',project_id:state.project.id,rois:clone(rois),original_signature:exposureStateSignature(),
    polarity:state.analysisParams.polarity || 'dark',adc_bits:state.analysisParams.adc_bits ?? '',black_level:state.analysisParams.black_level ?? '',saturation_value:state.analysisParams.saturation_value ?? '',error:'',review:null,selected_groups:[],loading:false};emit();
}

function exposureDraftSignature(modal) {
  return JSON.stringify([reviewInputSignature(modal.rois,modal.polarity),...['adc_bits','black_level','saturation_value'].map(key=>String(modal[key] ?? ''))]);
}
function exposureStateSignature() {
  return exposureDraftSignature({...state.analysisParams,rois:state.project.settings.analysis_rois});
}

const qualityNames={nonfinite_pixels:'无效像素',roi_too_small:'区域过小',background_unavailable:'无背景',detector_range_unknown:'缺少探测器范围',saturated_pixels:'原始削顶',nonpositive_signal:'信号不高于背景',low_signal_to_noise:'低信噪',values_outside_detector_range:'超出声明量程',explicit_identity_missing:'实验身份未完整',invalid_roi_role:'角色无效',excluded_by_user:'已排除',duplicate_source_pixels:'重复源像素',duplicate_measurement_identity:'同一测量重复',incomplete_exposure_set:'缺少样本或条带',target_control_pair_missing:'目标／内参未成对',control_identity_ambiguous:'内参身份不唯一',roi_area_mismatch_between_exposures:'各曝光 ROI 面积不同',roi_quality_failed:'ROI 未通过质控'};

function renderExposures() {
  const modal=state.modal,review=modal.review;
  const identities=`<details id="wb-exposure-identities" ${review?'':'open'}><summary>核对曝光与实验身份 · ${modal.rois.length} 个 ROI</summary><p class="microcopy">同一可比系列内各曝光须包含相同样本、目标和内参。不同文件的目标／内参可填写相同配对 ID；不自动按文件名猜测。</p><div class="data-table-wrap wb-exposure-table"><table class="data-table"><thead><tr><th>候选</th><th>样本 / 蛋白 / 角色</th><th>实验 ID</th><th>比较系列</th><th>曝光 ID</th><th>配对 ID · 可选</th><th>秒 · 可选</th></tr></thead><tbody>${modal.rois.slice(0,500).map((roi,index)=>`<tr><td><input id="wb-exposure-${index}-included" type="checkbox" data-wb-exposure-row="${index}" data-wb-exposure-key="included" ${roi.included===false?'':'checked'}></td><td>${e(roi.sample_id)} / ${e(roi.protein)}<small>${roi.role==='control'?'内参':'目标'} · ${e(roi.group)} · 生物 ${e(roi.biological_replicate)}</small></td>${['experiment_id','exposure_series_id','exposure_id','exposure_pair_id','exposure_seconds'].map(key=>`<td>${input(`wb-exposure-${index}-${key}`,roi[key],`data-wb-exposure-row="${index}" data-wb-exposure-key="${key}"`,key==='exposure_seconds'?'number':'text')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>${modal.rois.length>500?'<p class="microcopy">身份表仅显示前 500 条；建议按实验拆分较大的项目。</p>':''}</details>`;
  const results=review?`<section><h3>完整曝光系列推荐</h3>${review.groups.map(group=>`<article class="wb-exposure-group"><h4>${e(group.experiment_id || '缺实验 ID')} · ${e(group.exposure_series_id)} <span>${group.logical_measurements} 个对应测量</span></h4><p>${e(group.explanation)}</p><div class="data-table-wrap"><table class="data-table"><thead><tr><th>曝光／配对 ID</th><th>最弱条带 SNR</th><th>最高削顶比例</th><th>结果</th></tr></thead><tbody>${group.candidates.map(candidate=>`<tr class="${candidate.pair_id===group.recommended_pair_id?'wb-recommended':''}"><td>${e(candidate.pair_id)}</td><td>${formatValue(candidate.minimum_snr)}</td><td>${candidate.max_saturation_fraction==null?'未知':`${formatValue(candidate.max_saturation_fraction*100)}%`}</td><td>${candidate.pair_id===group.recommended_pair_id?'推荐':candidate.eligible?'可用':candidate.reasons.map(reason=>qualityNames[reason] || reason).join('；')}</td></tr>`).join('')}</tbody></table></div>${group.recommended_pair_id?`<label class="checkbox-label"><input type="checkbox" id="wb-exposure-apply-${group.id}" data-wb-exposure-group="${group.id}" ${modal.selected_groups.includes(group.id)?'checked':''}>应用推荐 ${e(group.recommended_pair_id)}，本系列其他曝光保留但不参与统计</label>`:''}</article>`).join('')}<details id="wb-exposure-quality"><summary>逐 ROI 原始值质量详情</summary><div class="data-table-wrap wb-exposure-table"><table class="data-table"><thead><tr><th>样本 / 蛋白</th><th>曝光</th><th>净积分</th><th>背景</th><th>背景噪声</th><th>SNR</th><th>判定</th></tr></thead><tbody>${review.rows.slice(0,500).map(row=>`<tr><td>${e(row.sample_id)} / ${e(row.protein)}</td><td>${e(row.pair_id)}</td><td>${formatValue(row.signed_integral)}</td><td>${formatValue(row.background)}</td><td>${formatValue(row.noise_sigma)}</td><td>${formatValue(row.snr)}</td><td>${row.eligible?'可用':row.reasons.map(reason=>qualityNames[reason] || reason).join('；')}</td></tr>`).join('')}</tbody></table></div></details>${review.warnings.map(warning=>`<p class="microcopy">${e(warning)}</p>`).join('')}</section>`:'';
  return {title:'WB 多曝光质量复核',content:`<div class="wb-workflow">${inlineError(modal.error)}${identities}<div class="field-grid">${['adc_bits','black_level','saturation_value'].map((key,index)=>`<label class="field"><span>${['探测器 ADC 位数 · 可选','黑电平 · 可选','饱和上限 · 可选'][index]}</span>${input(`wb-exposure-${key}`,modal[key],`data-wb-exposure-setting="${key}"`,'number')}</label>`).join('')}</div><p class="microcopy">默认核对存储位深的上下限。12 位仪器保存为 16 位 TIFF 时，应填写真实 ADC=12。评分只基于原始 ROI，不使用显示亮度。</p><button class="button" data-action="wb-review-exposures" ${modal.loading?'disabled':''}>${modal.loading?'正在读取原始 ROI…':'分析曝光质量'}</button>${results}</div>`,footerContent:`<button class="button" data-action="close-modal">关闭</button><button class="button primary" data-action="wb-apply-exposures" ${!review||!modal.selected_groups.length||modal.loading?'disabled':''}>应用已勾选推荐</button>`};
}

async function reviewExposures() {
  const modal=state.modal;if(modal?.type!=='wb-exposures'||modal.loading)return;
  try {
    if(state.project.id!==modal.project_id||exposureStateSignature()!==modal.original_signature)throw new Error('测量区域、分析极性或探测器范围已经改变，请重新打开曝光复核');
    const options={};for(const key of ['adc_bits','black_level','saturation_value'])if(modal[key]!==''&&modal[key]!=null){const value=Number(modal[key]);if(!Number.isFinite(value)||(key==='adc_bits'&&(!Number.isInteger(value)||value<1||value>32)))throw new Error('请输入有效的探测器范围，ADC 应为 1–32 的整数');options[key]=value;}
    const rows=clone(modal.rois);for(const row of rows)if(row.exposure_seconds==='')delete row.exposure_seconds;else if(row.exposure_seconds!=null)row.exposure_seconds=Number(row.exposure_seconds);
    const inputSignature=exposureDraftSignature(modal),projectId=modal.project_id;
    mutate(project=>{project.settings.analysis_rois=rows;project.settings.analysis_params={...state.analysisParams,...options};for(const key of ['adc_bits','black_level','saturation_value'])if(!(key in options))delete project.settings.analysis_params[key];},'核对 WB 曝光身份和探测器范围');
    modal.original_signature=exposureStateSignature();modal.loading=true;modal.error='';modal.review=null;modal.selected_groups=[];emit();
    await flushSave();
    if(state.modal!==modal)return;
    if(state.project.id!==projectId||exposureDraftSignature(modal)!==inputSignature||exposureStateSignature()!==modal.original_signature)throw new Error('保存期间复核输入已改变，修改已保留，请重新分析');
    const revision=state.project.revision;
    const result=await request('/api/wb/exposure-review',{method:'POST',body:{project_id:projectId,project_revision:revision,rois:rows,polarity:modal.polarity,...options}});
    if(state.modal!==modal)return;
    if(state.project.id!==projectId||exposureDraftSignature(modal)!==inputSignature||exposureStateSignature()!==modal.original_signature)throw new Error('质量计算期间复核输入已改变，修改已保留，请重新分析');
    modal.review=result;modal.selected_groups=result.groups.filter(group=>group.recommended_pair_id).map(group=>group.id);modal.rois=clone(rows);
  } catch(error){if(state.modal===modal)modal.error=error.message;}
  finally{modal.loading=false;emit();}
}

function applyExposures() {
  const modal=state.modal;if(modal?.type!=='wb-exposures'||!modal.review)return;
  try {
    if(state.project.id!==modal.project_id||exposureStateSignature()!==modal.original_signature)throw new Error('复核之后测量输入或探测器范围已经改变，请重新计算推荐');
    if(!modal.selected_groups.length)throw new Error('请勾选需要应用的完整曝光系列');
    const updated=applyExposureDecisions(state.project.settings.analysis_rois,modal.review,modal.selected_groups);
    mutate(project=>{project.settings.analysis_rois=updated;project.settings.analysis_params={...state.analysisParams,selected_exposures:{},wb_draft_scope_id:''};
      project.settings.wb_exposure_reviews ||= [];project.settings.wb_exposure_reviews.push({created_at:new Date().toISOString(),roi_signature:modal.review.roi_signature,method:modal.review.method,groups:modal.review.groups.filter(group=>modal.selected_groups.includes(group.id))});project.settings.wb_exposure_reviews=project.settings.wb_exposure_reviews.slice(-20);
    },'应用完整 WB 曝光质量推荐');
    state.modal=null;state.tab='analysis';state.analysisKind='wb';emit();notify('已更新曝光纳入与目标／内参配对，请重新运行定量；所有原件和未选曝光均保留');
  } catch(error){modal.error=error.message;emit();}
}

export function renderWbWorkflow() {return state.modal?.type==='wb-exposures'?renderExposures():renderDraft();}

export async function handleWbAction(action,_element) {
  if(action==='open-wb-workflow'){await openWbWorkflow();return true;}
  if(action==='open-wb-exposures'){openExposureReview();return true;}
  if(action==='wb-create-draft'){await createDraft();return true;}
  if(action==='wb-review-exposures'){await reviewExposures();return true;}
  if(action==='wb-apply-exposures'){applyExposures();return true;}
  if(action==='wb-add-row'&&state.modal?.draft){const draft=state.modal.draft;draft.rows.push({id:`manual${Date.now()}`,y:Math.round(draft.height/2),bands:[],coverage:0,role:'ignore',protein:'',roi_width:Math.max(5,Math.round(draft.width/Math.max(1,draft.lanes.length)*.7)),roi_height:Math.max(5,Math.round(draft.height/30))});emit();return true;}
  if(action==='wb-apply-paste'&&state.modal?.draft){try{state.modal.draft.lanes=parseLaneTable(state.modal.paste,state.modal.draft.lanes);state.modal.error='';}catch(error){state.modal.error=error.message;}emit();return true;}
  return false;
}

export function handleWbInput(element) {
  const modal=state.modal;if(!['wb-workflow','wb-exposures'].includes(modal?.type))return false;
  const value=element.type==='checkbox'?element.checked:element.value;let handled=true;
  if(element.hasAttribute('data-wb-draft-field'))modal.draft[element.dataset.wbDraftField]=value;
  else if(element.hasAttribute('data-wb-row')){const row=modal.draft.rows.find(item=>item.id===element.dataset.wbRow);if(row){row[element.dataset.wbRowField]=value;if(['roi_width','roi_height'].includes(element.dataset.wbRowField))for(const key of Object.keys(modal.draft.roi_overrides))if(key.startsWith(`${row.id}:`))delete modal.draft.roi_overrides[key];}}
  else if(element.hasAttribute('data-wb-lane')){const lane=modal.draft.lanes.find(item=>item.index===Number(element.dataset.wbLane));if(lane)lane[element.dataset.wbLaneField]=value;}
  else if(element.hasAttribute('data-wb-rectangle')){try{const key=element.dataset.wbRectangle,[rowId,lane]=key.split(':'),roi=draftRois(modal.draft,{requireIdentity:false,validateGeometry:false,allowUnpaired:true}).find(item=>item.wb_row_id===rowId&&item.lane_index===Number(lane));if(roi){const geometry=modal.draft.roi_overrides[key] || {x:roi.x,y:roi.y,width:roi.width,height:roi.height};geometry[element.dataset.wbRectangleField]=Number(value);modal.draft.roi_overrides[key]=geometry;}}catch(error){modal.error=error.message;}}
  else if(element.hasAttribute('data-wb-paste'))modal.paste=value;
  else if(element.hasAttribute('data-wb-exposure-row')){modal.rois[Number(element.dataset.wbExposureRow)][element.dataset.wbExposureKey]=value;modal.review=null;}
  else if(element.hasAttribute('data-wb-exposure-setting')){modal[element.dataset.wbExposureSetting]=value;modal.review=null;}
  else if(element.hasAttribute('data-wb-exposure-group')){modal.selected_groups=modal.selected_groups.filter(id=>id!==element.dataset.wbExposureGroup);if(value)modal.selected_groups.push(element.dataset.wbExposureGroup);}
  else handled=false;
  if(handled)emit();return handled;
}
