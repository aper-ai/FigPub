import {state,emit,mutate,notify,analysisGeometry,analysisSelection,analysisRegionKey,selectedAnalysisChannels} from './state.js';
import {clone,escapeHtml as e,formatValue} from './domain.js';
import {advancedDefaults} from './histology-domain.js';

const field=(label,key,value,{type='number',min='',max='',step='any',placeholder=''}={})=>`<label class="field"><span>${label}</span><input id="analysis-${key}" data-analysis-field="${key}" type="${type}" value="${e(value ?? '')}" step="${step}" ${min!==''?`min="${min}"`:''} ${max!==''?`max="${max}"`:''} placeholder="${e(placeholder)}"></label>`;
const select=(label,key,options,value)=>`<label class="field"><span>${label}</span><select id="analysis-${key}" data-analysis-field="${key}">${options.map(([id,name])=>`<option value="${e(id)}" ${String(value)===String(id)?'selected':''}>${e(name)}</option>`).join('')}</select></label>`;
const check=(label,key,value)=>`<label class="checkbox-label"><input type="checkbox" id="advanced-${key}" data-advanced-boolean="${key}" ${value?'checked':''}>${label}</label>`;
const details=(id,title,content)=>`<details class="compact-details" id="${id}"><summary>${title}</summary><div class="details-content">${content}</div></details>`;
const notice=(title,body)=>`<div class="notice"><strong>${title}</strong><p>${body}</p></div>`;

function ihcFields(params,geometry) {
  const profiles=Object.values(state.project?.settings.ihc_profiles || {}),locked=params.locked_profile;
  let output=field('批次校准名称','profile_name',params.profile_name,{type:'text'});
  if(profiles.length)output+=`<label class="field"><span>已保存的批次校准</span><select id="ihc-saved-profile" data-ihc-profile><option value="">选择后应用…</option>${profiles.map(profile=>`<option value="${e(profile.id)}">${e(profile.name)} · ${e(profile.id.slice(0,6))}</option>`).join('')}</select></label>`;
  if(locked)output+=notice('正在使用锁定校准',`${e(locked.name)} · 阈值 ${locked.grade_thresholds.map(value=>e(formatValue(value))).join(' / ')}。染色向量、白点和阈值在各图保持一致。`)+`<button class="button full small" data-action="ihc-unlock-profile">解除锁定并单独调整</button>`;
  else {
    output+=select('H/DAB 染色向量','stain_method',[['macenko','Macenko · 从组织估计'],['fixed','固定 / 实测染色向量']],params.stain_method);
    output+=params.stain_method==='fixed'?field('H 向量 · R, G, B 光密度','stain_h_vector',params.stain_h_vector,{type:'text'})+field('DAB 向量 · R, G, B 光密度','stain_dab_vector',params.stain_dab_vector,{type:'text'}):field('Macenko 角度百分位','macenko_percentile',params.macenko_percentile,{min:.01,max:10});
    output+=select('强度分级','grading_method',[['manual','手动固定光密度阈值'],['gmm','GMM · 四组强度拟合'],['examples','阴阳示例校准阳性界线']],params.grading_method);
    if(params.grading_method!=='gmm')output+=`<div class="advanced-threshold-grid">${field('0 → 1+','grade_threshold_1',params.grade_threshold_1,{min:0})}${field('1+ → 2+','grade_threshold_2',params.grade_threshold_2,{min:0})}${field('2+ → 3+','grade_threshold_3',params.grade_threshold_3,{min:0})}</div>`;
    if(params.grading_method==='examples')output+=`<div class="advanced-example-actions"><button class="button small ${state.analysisRegionTarget==='ihc_negative'?'active':''}" data-action="ihc-example-region" data-role="negative">${geometry?.ihc_negative?'重画':'绘制'}阴性示例</button><button class="button small ${state.analysisRegionTarget==='ihc_positive'?'active':''}" data-action="ihc-example-region" data-role="positive">${geometry?.ihc_positive?'重画':'绘制'}阳性示例</button></div><p class="microcopy">阴阳示例只校准第一条阳性界线；中、强界线保留上述明确设置。示例须在分析范围内。</p>`;
    if(params.grading_method==='gmm')output+='<p class="microcopy">四个强度组必须有足够样本和可分性；数据不足或单峰时会要求校准，不强行给出四级结果。</p>';
    output+=details('ihc-illumination','组织与原始白点',field('组织最小光密度','tissue_min_od',params.tissue_min_od,{min:0})+field('原始 RGB 白点 · 留空按位深','rgb_white_level',params.rgb_white_level,{min:0,placeholder:'8-bit 默认 255；float 默认 1'}));
  }
  output+=details('ihc-artifacts','组织伪影排除',`<p class="microcopy">已保留 ${geometry?.ihc_exclusions?.length || 0} 个排除区域。此设置随当前原始平面保存。</p><button class="button small full" data-action="ihc-example-region" data-role="exclusion">绘制伪影区域</button>${geometry?.ihc_exclusion?'<button class="button small full primary" data-action="ihc-append-exclusion">添加当前伪影区域</button>':''}${geometry?.ihc_exclusions?.length?'<button class="button small full ghost" data-action="ihc-clear-exclusions">清除伪影排除</button>':''}`);
  return output+notice('按组织像素评分','0 / 1+ / 2+ / 3+ 的面积比例与 0–300 像素加权指数。该指标不等同于逐细胞 H-score。');
}

function fiberFields(params,channels) {
  const choices=channels.map(channel=>[channel.id,channel.name || channel.id]),optional=[['','未提供'],...choices];
  let output=select('纤维 / 肌管结构原始通道','fiber_channel_id',choices,params.fiber_channel_id || channels[0]?.id)+select('实例来源','segmentation',[['threshold','阈值与分水岭候选'],['labels','导入的整数实例标签通道'],['cellpose','Cellpose cyto3 · 通用候选']],params.segmentation);
  if(params.segmentation==='labels')output+=select('整数实例标签通道 · 0 为背景','label_channel_id',optional,params.label_channel_id)+notice('使用已有分割结果','将同尺寸标签 TIFF 与原图组合，再勾选参与分析的原始通道。标签须使用整数实例 ID，并明确指定标签通道。');
  else if(params.segmentation==='cellpose')output+=field('预期直径 / 原始 px','diameter_px',params.diameter_px ?? 90,{min:2,max:2000})+notice('通用模型候选','使用已安装的 CPU cyto3。专用肌纤维/肌管模型生成的标签可从标签通道导入。');
  else {
    output+=select('结构信号类型','fiber_input',[['filled','实心纤维 / 肌管信号'],['membrane','膜 / 边界信号包围的横截面']],params.fiber_input)+select('结构极性','fiber_polarity',[['bright','亮结构 / 暗背景'],['dark','暗结构 / 亮背景']],params.fiber_polarity)+select('结构阈值','fiber_threshold_method',[['otsu','Otsu'],['manual','指定原始强度']],params.fiber_threshold_method);
    if(params.fiber_threshold_method==='manual')output+=field('结构阈值 / 原始值','fiber_threshold',params.fiber_threshold);
    output+=details('fiber-segmentation-options','分割细节',field('平滑 sigma / px','fiber_sigma',params.fiber_sigma,{min:0,max:20})+(params.fiber_input==='membrane'?field('边界闭合半径 / px','membrane_close_radius',params.membrane_close_radius,{min:0,max:20,step:1}):check('分离接触对象','fiber_split_touching',params.fiber_split_touching)+field('分离种子间距 / px','fiber_min_distance',params.fiber_min_distance,{min:1,max:1000,step:1})));
  }
  output+=`<div class="field-grid">${field('最小面积 / px²','fiber_min_area',params.fiber_min_area,{min:1,step:1})}${field('最大面积 / px² · 可选','fiber_max_area',params.fiber_max_area,{min:1,step:1})}</div>`+check('排除接触分析区域边缘的纤维','exclude_border',params.exclude_border);
  output+=details('fiber-nuclei-options','细胞核与中央 / 周边判定',select('细胞核强度通道','nucleus_channel_id',optional,params.nucleus_channel_id)+select('或提供细胞核整数标签 · 优先使用','nucleus_label_channel_id',optional,params.nucleus_label_channel_id)+select('细胞核阈值','nucleus_threshold_method',[['otsu','Otsu'],['manual','指定原始强度']],params.nucleus_threshold_method)+(params.nucleus_threshold_method==='manual'?field('细胞核阈值 / 原始值','nucleus_threshold',params.nucleus_threshold):'')+select('细胞核极性','nucleus_polarity',[['bright','亮核 / 暗背景'],['dark','暗核 / 亮背景']],params.nucleus_polarity)+field('最小细胞核面积 / px²','nucleus_min_area',params.nucleus_min_area,{min:1,step:1})+field('细胞核种子间距 / px','nucleus_min_distance',params.nucleus_min_distance,{min:1,max:1000,step:1})+field('核归属的最小覆盖率','nucleus_min_overlap',params.nucleus_min_overlap,{min:.5,max:1})+field('周边带 / 纤维最大内距离','peripheral_fraction',params.peripheral_fraction,{min:.01,max:.99})+'<p class="microcopy">多个纤维共同覆盖或覆盖不足时标为不确定。未提供核通道时，核指标留空。</p>');
  if(params.method==='myotube')output+=field('融合肌管的最少核数','fusion_min_nuclei',params.fusion_min_nuclei,{min:2,max:1000,step:1})+'<p class="microcopy">融合指数 = 达到最少核数、归属明确的肌管内核数 / 当前 ROI 所有检测核数；核外与不确定核仍计入分母。</p>';
  const markers=params.fiber_markers || [];
  output+=details('fiber-marker-types','纤维标记分型',markers.map((marker,index)=>`<div class="fiber-marker-entry"><label class="field"><span>标记通道</span><select id="fiber-marker-${index}-channel" data-fiber-marker="channel_id" data-index="${index}">${choices.map(([id,name])=>`<option value="${e(id)}" ${id===marker.channel_id?'selected':''}>${e(name)}</option>`).join('')}</select></label><label class="field"><span>分型名称</span><input id="fiber-marker-${index}-name" data-fiber-marker="name" data-index="${index}" value="${e(marker.name)}"></label><label class="field"><span>平均原始强度 ≥</span><input id="fiber-marker-${index}-threshold" data-fiber-marker="threshold" data-index="${index}" type="number" step="any" value="${e(marker.threshold)}"></label><button class="text-button" data-action="fiber-remove-marker" data-index="${index}">移除此标记</button></div>`).join('')+'<button class="button full small" data-action="fiber-add-marker">添加分型标记</button><p class="microcopy">按每个纤维的原始通道均值与明确阈值分型；多阳性保留联合类别，不强制归为一种。</p>');
  return output;
}

export function renderAdvancedFields(input,geometry={},channels=[]) {
  const params={...advancedDefaults,...input};
  return `<div class="advanced-microscopy-fields">${params.method==='ihc_advanced'?ihcFields(params,geometry):fiberFields(params,channels)}</div>`;
}
function editParams(change,label) {mutate(project=>{const params=project.settings.analysis_params=clone(state.analysisParams);change(params,project);},label);}
function editGeometry(change,label) {const key=analysisRegionKey();mutate(project=>{project.settings.analysis_regions ||= {};const geometry=project.settings.analysis_regions[key] ||= {};change(geometry);},label);}
export const advancedActions={
  'ihc-example-region':element=>{const role=element.dataset.role;if(!['positive','negative','exclusion'].includes(role))return;state.analysisRegionTarget=`ihc_${role}`;state.sourceTool='roi';state.sourcePolygon=[];emit();notify(`请在当前原图绘制${{positive:'阳性示例',negative:'阴性示例',exclusion:'伪影排除区域'}[role]}`,'info');},
  'ihc-append-exclusion':()=>{const region=analysisGeometry().ihc_exclusion;if(!region)return;editGeometry(geometry=>{geometry.ihc_exclusions ||= [];if(geometry.ihc_exclusions.length>=100)throw new Error('最多保留 100 个伪影区域');geometry.ihc_exclusions.push(clone(region));delete geometry.ihc_exclusion;},'添加 IHC 伪影排除');state.analysisRegionTarget='roi';state.sourceTool='select';emit();},
  'ihc-clear-exclusions':()=>editGeometry(geometry=>{delete geometry.ihc_exclusions;delete geometry.ihc_exclusion;},'清除 IHC 伪影排除'),
  'ihc-lock-profile':()=>{const selection=analysisSelection();if(selection.stale||!selection.persisted||!selection.result?.calibration_profile){notify('请先完成当前来源的 IHC 分析，再锁定校准','info');return;}const profile=clone(selection.result.calibration_profile);editParams((params,project)=>{project.settings.ihc_profiles ||= {};project.settings.ihc_profiles[profile.id]=profile;params.locked_profile=profile;},'保存并锁定 IHC 批次校准');notify('批次校准已锁定；后续图像复用同一向量、白点和阈值');},
  'ihc-unlock-profile':()=>editParams(params=>{params.locked_profile=null;},'解除 IHC 批次锁定'),
  'ihc-reset-auto':()=>editParams(params=>{params.locked_profile=null;params.stain_method='macenko';params.grading_method='gmm';},'恢复 IHC 自动估计'),
  'fiber-add-marker':()=>{const channels=selectedAnalysisChannels();if(!channels.length)return;editParams(params=>{params.fiber_markers ||= [];if(params.fiber_markers.length>=16)throw new Error('最多 16 个分型标记');const channel=channels[0];params.fiber_markers.push({channel_id:channel.id,name:channel.name || channel.id,threshold:''});},'添加纤维分型标记');},
  'fiber-remove-marker':element=>editParams(params=>{params.fiber_markers=(params.fiber_markers || []).filter((_,index)=>index!==Number(element.dataset.index));},'移除纤维分型标记'),
};
export function handleAdvancedChange(element) {
  if(element.matches('[data-advanced-boolean]')) {editParams(params=>{params[element.dataset.advancedBoolean]=element.checked;},'调整显微分析选项');return true;}
  if(element.matches('[data-fiber-marker]')) {editParams(params=>{const marker=params.fiber_markers?.[Number(element.dataset.index)];if(marker)marker[element.dataset.fiberMarker]=element.value;},'调整纤维分型条件');return true;}
  if(element.matches('[data-ihc-profile]')) {const profile=state.project.settings.ihc_profiles?.[element.value];if(profile)editParams(params=>{params.locked_profile=clone(profile);params.profile_name=profile.name;},'应用已保存 IHC 批次校准');return true;}
  return false;
}

function geometrySvg(region,color,label,scale) {
  if(!region)return '';
  const attrs=`fill="${color}" fill-opacity=".06" stroke="${color}" stroke-width="${Math.max(.8,1.6*scale)}" stroke-dasharray="${4*scale} ${2*scale}"`;
  const shape=region.type==='ellipse'?`<ellipse cx="${region.x+region.width/2}" cy="${region.y+region.height/2}" rx="${region.width/2}" ry="${region.height/2}" ${attrs}/>`:region.type==='polygon'?`<polygon points="${region.points.map(point=>point.join(',')).join(' ')}" ${attrs}/>`:`<rect x="${region.x}" y="${region.y}" width="${region.width}" height="${region.height}" ${attrs}/>`;
  return `<g pointer-events="none">${shape}<text x="${region.x}" y="${Math.max(12*scale,region.y-3*scale)}" fill="${color}" font-size="${12*scale}">${label}</text></g>`;
}
export function renderAdvancedOverlays(geometry={},scale=1) {
  if(state.analysisParams.method!=='ihc_advanced')return '';
  return geometrySvg(geometry.ihc_negative,'#3a82c4','阴性示例',scale)+geometrySvg(geometry.ihc_positive,'#bd7326','阳性示例',scale)+geometrySvg(geometry.ihc_exclusion,'#b74d78','待添加伪影',scale)+(geometry.ihc_exclusions || []).map((region,index)=>geometrySvg(region,'#b74d78',`排除 ${index+1}`,scale)).join('');
}
function table(rows,columns) {
  const labels={classification:{central:'中央核',peripheral:'周边核',uncertain:'归属待复核',outside:'未归属纤维'},
    reason:{no_fiber_overlap:'核体未覆盖纤维',insufficient_or_multiple_fiber_overlap:'覆盖不足或跨多个纤维',nuclear_body_distance_percentile:'核体边界距离第 10 百分位'},
    phenotype:{negative:'标记阴性',unresolved:'无法判定'}};
  const display=(row,key)=>labels[key]&&Object.hasOwn(labels[key],row[key])?labels[key][row[key]]:formatValue(row[key]);
  return `<div class="data-table-wrap"><table class="data-table"><thead><tr>${columns.map(([,name])=>`<th>${e(name)}</th>`).join('')}</tr></thead><tbody>${rows.slice(0,500).map(row=>`<tr>${columns.map(([key])=>`<td>${e(display(row,key))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}
function gradeMap(preview) {
  if(!preview||preview.values?.length!==preview.width*preview.height)return '';
  const colors=['#cdd8df','#e6c78d','#ba8347','#633b26'],paths=['','','',''];
  for(let y=0;y<preview.height;y++)for(let x=0;x<preview.width;) {const value=preview.values[y*preview.width+x],start=x;while(x<preview.width&&preview.values[y*preview.width+x]===value)x++;if(value>=0&&value<4)paths[value]+=`M${start} ${y}h${x-start}v1h${start-x}z`;}
  return `<figure class="ihc-grade-map"><svg viewBox="0 0 ${preview.width} ${preview.height}" role="img" aria-label="IHC 组织强度分级缩略图"><rect width="100%" height="100%" fill="#f5f6f3"/>${paths.map((path,i)=>`<path d="${path}" fill="${colors[i]}"/>`).join('')}</svg><figcaption>分级位置预览 · 每 ${preview.stride} 像素取样；计量使用全部组织像素</figcaption></figure>`;
}
export function renderAdvancedResults(result,selection={}) {
  if(!result)return '';
  const method=result.analysis_method || result.method?.name || result.method;
  if(method==='ihc_advanced') {
    const rows=result.rows || [],colors=['#cdd8df','#e6c78d','#ba8347','#633b26'];
    return `<section class="advanced-result"><div class="result-section-heading"><h4>组织强度分级</h4><button class="button small" data-action="ihc-lock-profile" ${selection.stale||!selection.persisted?'disabled':''}>保存并锁定为批次校准</button></div><div class="ihc-grade-bar" aria-label="0 / 1+ / 2+ / 3+ 面积构成">${rows.map((row,index)=>`<span style="width:${row.percent}%;background:${colors[index]}" title="${e(row.grade)}：${Number(row.percent).toFixed(2)}%"></span>`).join('')}</div>${table(rows,[['grade','等级'],['area_px','面积 px²'],['percent','组织面积 %'],['lower_dab_od','DAB 下界'],['upper_dab_od','DAB 上界']])}${gradeMap(result.grade_preview)}<p class="microcopy">0–300 像素加权指数：${e(formatValue(result.summary?.pixel_weighted_score))}。这是组织像素分布，不是逐细胞 H-score。</p></section>`;
  }
  if(!['fiber_cross_section','myotube'].includes(method))return '';
  return `<section class="advanced-result"><div class="result-section-heading"><h4>纤维 / 肌管几何与核归属</h4><button class="text-button" data-action="download-results" data-result-section="objects">完整对象 CSV</button></div>${table(result.objects || [],[['id','实例'],['area_px','面积 px²'],['area_um2','面积 μm²'],['min_feret_px','最小 Feret px'],['min_feret_um','最小 Feret μm'],['major_axis_length_px','长轴 px'],['minor_axis_length_px','短轴 px'],['aspect_ratio','长宽比'],['skeleton_length_px','骨架长度 px'],['nuclei_count','归属核'],['central_nuclei','中央核'],['peripheral_nuclei','周边核'],['uncertain_nuclei','待复核核'],['phenotype','标记分型']])}${result.nuclei?.length?`<details class="method-details" id="fiber-nuclei-table"><summary>逐核归属与判定 · ${result.nuclei.length} 个</summary><button class="button small" data-action="download-results" data-result-section="nuclei">细胞核 CSV</button>${table(result.nuclei,[['id','细胞核'],['fiber_id','纤维'],['classification','类别'],['overlap_fraction','覆盖率'],['normalized_edge_distance_q10','归一化边界距离'],['reason','依据']])}</details>`:''}${result.phenotypes?.length?`<h4>标记分型构成</h4>${table(result.phenotypes,[['phenotype','类别'],['fiber_count','纤维数'],['fraction','比例']])}`:''}<p class="microcopy">实例拆分、合并、补画或删除后，使用原始通道重新计算上述形态、核归属及融合指标。</p></section>`;
}
