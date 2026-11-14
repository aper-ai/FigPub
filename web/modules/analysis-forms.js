import {state,currentRawSource,availableAnalysisChannels,selectedAnalysisChannels,analysisGeometry,analysisSelection} from './state.js';
import {escapeHtml as e,formatValue} from './domain.js';
import {microscopyMethods,statisticalTests} from './analysis-domain.js';
import {rawSourceLabel} from './channel-view.js';
import {sourceIdentityKey} from './channel-domain.js';
import {icon} from './icons.js';
import {isAdvancedMethod} from './histology-domain.js';
import {renderAdvancedFields} from './histology-fibers.js';
import {renderExposureReviewEntry} from './wb-workflow.js';
import {renderCellposeStatus} from './cellpose-status.js';
const section=(title,content,description='')=>`<section class="inspector-section"><div class="inspector-heading"><h3>${title}</h3></div>${description?`<p class="section-description">${description}</p>`:''}${content}</section>`;
const field=(label,key,value,{type='number',step='any',placeholder='',min=null,max=null}={})=>`<label class="field"><span>${label}</span><input id="analysis-${key}" type="${type}" data-analysis-field="${key}" value="${e(value ?? '')}" ${type==='number'?`step="${step}"`:''} ${min===null?'':`min="${min}"`} ${max===null?'':`max="${max}"`} placeholder="${e(placeholder)}"></label>`;
const select=(label,key,options,value)=>`<label class="field"><span>${label}</span><select id="analysis-${key}" data-analysis-field="${key}">${options.map(([id,name])=>`<option value="${id}" ${id===value?'selected':''}>${e(name)}</option>`).join('')}</select></label>`;
const details=(id,title,content)=>`<details class="compact-details" id="${id}"><summary>${title}</summary><div class="details-content">${content}</div></details>`;
function statisticsFields(params) {
  return select('统计检验','test',statisticalTests,params.test || 'none')+'<p class="microcopy">组间检验使用独立生物学重复。配对检验按生物学 ID 匹配；多项对照比较报告 Holm 校正后的 p 值。显著性标记由使用者审核后添加。</p>';
}
function wbFields(params) {
  const options=availableAnalysisChannels(),source=currentRawSource(),groups=[...new Set(state.project.settings.analysis_rois.map(roi=>roi.group).filter(Boolean))];
  const drafts=Object.values(state.project.settings.wb_drafts || {});
  return select('测量范围','wb_draft_scope_id',[['','当前项目全部 ROI'],...drafts.map(draft=>[draft.id,`${draft.experiment_id || '实验'} · ${draft.exposure_id || draft.id}`])],params.wb_draft_scope_id || '')+(options.length?`<label class="field"><span>当前测量原始通道</span><select id="wb-raw-source" data-wb-source>${options.map(channel=>`<option value="${e(channel.id)}" ${sourceIdentityKey(channel.source)===sourceIdentityKey(source)?'selected':''}>${e(channel.name)} · ${e(rawSourceLabel(channel.source,{file:false}))}</option>`).join('')}</select></label>`:'')+
    select('条带极性','polarity',[['dark','深色条带 / 浅背景'],['bright','亮色条带 / 深背景']],params.polarity)+
    select('背景扣除','background',[['border','ROI 边界局部背景'],['rolling_ball','滚动球背景'],['none','不扣除']],params.background)+
    (params.background==='rolling_ball'?field('滚动球半径 / 原始 px','radius',params.radius,{min:1,max:100,step:1}):'')+
    `<label class="field"><span>对照组 · 可选，用于相对表达</span><input id="analysis-control_group" data-analysis-field="control_group" value="${e(params.control_group)}" list="wb-group-options" placeholder="与 ROI 组名一致"><datalist id="wb-group-options">${groups.map(group=>`<option value="${e(group)}"></option>`).join('')}</datalist></label>`+
    renderExposureReviewEntry()+details('wb-detector','探测器原始范围',field('探测器 ADC 位数 · 可选','adc_bits',params.adc_bits,{min:1,max:32,step:1})+field('黑电平 · 可选','black_level',params.black_level)+field('饱和上限 · 可选','saturation_value',params.saturation_value))+(Object.keys(params.selected_exposures || {}).length?'<p class="microcopy">当前保留旧项目的样本级曝光选择。</p><button class="button full small" data-action="reset-wb-exposure-selection">清除旧曝光选择，按逐区域纳入</button>':'')+details('wb-statistics','统计检验',statisticsFields(params))+
    '<div class="notice"><strong>生物学重复与曝光</strong><p>同一样本的多次曝光不增加生物学重复数。缺少样本身份或内参配对信息时，仅报告可计算的强度。</p></div>';
}
function qpcrFields(params) {
  const asset=state.project.assets.find(item=>item.id===state.selectedAssetId),table=state.table?.canonical&&state.table.asset_id===asset?.id?state.table:null,targets=table?.targets || [],groups=table?.groups || [],samples=table?.sample_groups || [];
  const sheets=[...new Set([...(state.qpcrSheetNames.get(asset?.id) || []),...(table?.sheets || []),asset?.metadata?.sheet,params.sheet].filter(Boolean))];
  const worksheet=select('工作表','sheet',[['','自动识别有效 Ct 工作表'],...sheets.map(name=>[name,name])],params.sheet || '');
  const reference=params.reference_genes?.length?params.reference_genes.join(', '):params.reference_gene;
  return `${table?`<div class="notice"><strong>已识别数据表</strong><p>基因：${e(targets.slice(0,8).join('、') || '未找到')}。<br>组别：${e(groups.slice(0,8).join('、') || '请在样本分组中填写')}。</p><p>在下面的输入框选择或填写准确名称。</p></div>`:''}${worksheet}<p class="microcopy">内参、分组和排除孔分别保存在当前原文件与工作表中；切换后不会沿用其他表的设置。</p><label class="field"><span>内参基因 · 多项用逗号分隔</span><input id="analysis-reference_gene" data-analysis-field="reference_gene" value="${e(reference)}" list="qpcr-target-options" placeholder="例如 ACTB, GAPDH"><datalist id="qpcr-target-options">${targets.map(target=>`<option value="${e(target)}"></option>`).join('')}</datalist></label><label class="field"><span>对照组</span><input id="analysis-control_group" data-analysis-field="control_group" value="${e(params.control_group)}" list="qpcr-group-options" placeholder="与实际组名一致"><datalist id="qpcr-group-options">${groups.map(group=>`<option value="${e(group)}"></option>`).join('')}</datalist></label><div class="method-formula">2<sup>−ΔΔCt</sup><span>相对表达量</span></div>${details('qpcr-groups','样本分组与技术重复质控',`${samples.length?`<div class="sample-group-list">${samples.map((sample,index)=>{const key=`${sample.experiment_id || ''}::${sample.sample}`,value=params.group_mapping?.[key] ?? params.group_mapping?.[sample.sample] ?? sample.group;return `<label class="field"><span>${e(sample.sample)}${sample.experiment_id?` · ${e(sample.experiment_id)}`:''}<small>生物学 ID ${e(sample.biological_replicate || '未提供')}</small></span><input id="qpcr-map-${index}" data-qpcr-group-key="${e(key)}" value="${e(value)}" placeholder="组名"></label>`;}).join('')}</div>`:'<p class="microcopy">读取标准化表格后可按样本设置组别。</p>'}${field('Ct 上限 · 留空不设置','max_ct',params.max_ct,{min:0,placeholder:'不设置'})}${field('技术重复 Ct 的 SD 提示阈值','technical_sd_limit',params.technical_sd_limit,{min:0})}<p class="microcopy">预览表中取消「纳入」可排除原始孔。原表不改写，结果保留所有孔的质控原因。</p>`)}${details('qpcr-statistics','统计检验',statisticsFields(params))}`;
}
function channelChoices(params) {
  if(['ihc','ihc_advanced'].includes(params.method)) {
    const options=availableAnalysisChannels(),planes=[...new Map(options.map(channel=>[`${channel.source.asset_id}:${channel.source.series}:${channel.source.frame}`,channel])).values()],selected=selectedAnalysisChannels()[0]?.source;
    return `<label class="field"><span>原始 RGB 平面</span><select id="ihc-rgb-source" data-analysis-rgb>${planes.map(channel=>`<option value="${e(channel.id)}" ${selected&&channel.source.asset_id===selected.asset_id&&channel.source.series===selected.series&&channel.source.frame===selected.frame?'selected':''}>${e(rawSourceLabel(channel.source))}</option>`).join('')}</select></label><div class="notice"><strong>使用原始 RGB 彩色平面</strong><p>IHC 从原始 RGB 颜色计算染色分离。伪彩合并图和单通道强度不作为 IHC 输入。</p></div>`;
  }
  const options=availableAnalysisChannels(),selected=selectedAnalysisChannels(),multiple=['channel_intensity','threshold','channel_overlap','line_profile','fiber_cross_section','myotube'].includes(params.method);
  return `<div class="analysis-channel-choices"><div class="field-label">明确选择待分析的原始通道${multiple?' · 可多选':''}</div>${options.map(channel=>`<label class="analysis-channel-choice"><input type="${multiple?'checkbox':'radio'}" name="analysis-raw-channel" id="analysis-raw-${e(channel.id)}" data-analysis-channel="${e(channel.id)}" data-analysis-channel-multiple="${multiple}" ${selected.some(item=>sourceIdentityKey(item.source)===sourceIdentityKey(channel.source))?'checked':''}><span><strong>${e(channel.name)}</strong><small>${e(rawSourceLabel(channel.source))}</small></span></label>`).join('')}</div><p class="microcopy">此选择独立于显示开关、伪彩色和 Gamma，测量使用原始强度。</p>`;
}
function geometryFields(params) {
  const geometry=analysisGeometry();
  if(params.method==='line_profile')return `<div class="notice"><strong>${geometry.line?'已设置原始采样线':'尚未设置采样线'}</strong><p>${geometry.line?`${formatValue(geometry.line.x1)}, ${formatValue(geometry.line.y1)} → ${formatValue(geometry.line.x2)}, ${formatValue(geometry.line.y2)} px`:'在图像上从起点拖到终点。'}</p></div><button class="button full small" data-action="source-tool" data-tool="analysis-line">${icon('line')}绘制 / 重画采样线</button>${geometry.line?'<button class="button full small ghost" data-action="clear-analysis-line">清除采样线</button>':''}<div class="field-grid">${field('采样点数','samples',params.samples,{min:2,max:4096,step:1})}${field('线宽 / 原始 px','line_width_px',params.line_width_px,{min:1,max:101,step:1})}</div><p class="microcopy">沿原始采样线读取强度。区域 ROI 与背景扣除不参与线剖面。</p>`;
  return `<div class="analysis-region-status"><span>${geometry.roi?`${{rectangle:'矩形',ellipse:'椭圆',polygon:'多边形'}[geometry.roi.type || 'rectangle']} · ${geometry.roi.width} × ${geometry.roi.height} px`:'分析范围：整幅原始图像'}</span>${geometry.roi?'<button class="text-button" data-action="clear-roi">恢复整图</button>':''}</div><button class="button full small ${state.analysisRegionTarget==='roi'?'active':''}" data-action="analysis-region-target" data-target="roi">绘制分析区域</button>${params.method==='channel_intensity'&&params.background_method==='roi'?`<button class="button full small ${state.analysisRegionTarget==='background_roi'?'active':''}" data-action="analysis-region-target" data-target="background_roi">绘制背景区域</button><p class="microcopy">${geometry.background_roi?`已设置背景：${geometry.background_roi.width} × ${geometry.background_roi.height} px`:'需要独立背景区域。可使用矩形、椭圆或多边形。'}</p>${geometry.background_roi?'<button class="button full small ghost" data-action="clear-background-roi">清除背景区域</button>':''}`:''}`;
}
function cellposeFields(params) {
  return select('本地模型','model',[['cyto3','cyto3 · 细胞'],['nuclei','nuclei · 细胞核']],params.model)+
    renderCellposeStatus(state.cellposeStatus,params.model,state.cellposeStatusLoading)+`${field('预期细胞直径 / 原始 px','diameter_px',params.diameter_px,{min:2,max:2000})}${details('cellpose-thresholds','Cellpose 阈值',`${field('Flow 阈值','flow_threshold',params.flow_threshold,{min:0,max:10})}${field('细胞概率阈值','cellprob_threshold',params.cellprob_threshold,{min:-20,max:20})}`)}<p class="microcopy">使用本地 CPU 执行 Cellpose 推理。模型不可用时停止分析；结果需结合原图复核。</p>`;
}
function microFields(params) {
  let fields=select('分析方法','method',microscopyMethods,params.method)+channelChoices(params);
  if(isAdvancedMethod(params.method))return fields+renderAdvancedFields(params,analysisGeometry(),selectedAnalysisChannels())+field('原始像素尺度 / μm·px⁻¹','um_per_px',params.um_per_px,{min:0,placeholder:'未知时留空'})+geometryFields(params);
  if(['nuclei','ihc','cellpose'].includes(params.method))fields+=field('最小面积 / 原始 px²','min_area',params.min_area,{min:1,max:params.method==='cellpose'?16000000:null,step:1});
  if(['nuclei','ihc'].includes(params.method))fields+=field('阈值 · 留空自动计算','threshold',params.threshold,{placeholder:'自动'});
  if(['threshold','channel_overlap'].includes(params.method))fields+=select('阈值方法','threshold_method',[['otsu','Otsu'],['triangle','Triangle'],['li','Li'],['manual','手动原始强度阈值']],params.threshold_method)+(params.threshold_method==='manual'?field('公共原始阈值 · 可被逐通道覆盖','threshold',params.threshold,{placeholder:'或在下方逐通道填写'})+selectedAnalysisChannels().map(channel=>`<label class="field"><span>${e(channel.name)} 阈值 / 原始值</span><input type="number" step="any" id="threshold-${e(channel.id)}" data-channel-threshold="${e(channel.id)}" value="${e(params.thresholds?.[channel.id]?.value ?? '')}" placeholder="使用公共阈值"></label>`).join(''):'');
  if(params.method==='channel_intensity') {
    fields+=select('背景处理','background_method',[['none','不扣除'],['roi','独立背景区域均值'],['fixed','逐通道固定背景值']],params.background_method);
    if(params.background_method==='fixed')fields+=selectedAnalysisChannels().map(channel=>`<label class="field"><span>${e(channel.name)} 背景 / 原始强度</span><input type="number" step="any" id="background-${e(channel.id)}" data-background-channel="${e(channel.id)}" value="${e(params.background_values?.[channel.id] ?? '')}" placeholder="填写测得的背景强度"></label>`).join('');
  }
  if(params.method==='cellpose')fields+=cellposeFields(params);
  fields+=field('原始像素尺度 / μm·px⁻¹','um_per_px',params.um_per_px,{min:0,placeholder:'未知时留空'})+geometryFields(params);
  if(params.method==='threshold')fields+='<p class="microcopy">报告阈值阳性面积与连通区域；连通区域数量不等同于细胞数量。</p>';
  if(params.method==='channel_overlap')fields+='<p class="microcopy">重叠由阈值掩膜确定；Pearson 与 Manders 使用原始通道值。配准误差、背景和串色可能影响解释。</p>';
  return fields;
}
export function renderAnalysisInspector() {
  const asset=state.project.assets.find(item=>item.id===state.selectedAssetId),valid=state.analysisKind==='qpcr'?asset?.kind==='table':asset?.kind==='image';
  if(!valid)return section('开始前准备',`<div class="analysis-empty-settings"><strong>${state.analysisKind==='qpcr'?'先准备 Ct 数据表':'先选择原始图像'}</strong><p>${state.analysisKind==='qpcr'?'导入后会显示可用基因、组别与分析设置。':'导入后再选择分析方法、原始通道和测量区域。'}</p><button class="button full primary" data-action="${state.analysisKind==='wb'?'import-wb':'import'}" data-task="${state.analysisKind}">${icon('upload')}选择文件</button>${state.analysisKind==='qpcr'?'<button class="button full" data-action="qpcr-template">下载 Ct 表模板</button>':''}</div>`);
  const selection=analysisSelection();
  const params=state.analysisParams,fields=state.analysisKind==='wb'?wbFields(params):state.analysisKind==='qpcr'?qpcrFields(params):microFields(params);
  const roi=state.analysisKind==='wb'?state.project.settings.analysis_rois.find(item=>item.id===state.selectedRoiId):null;
  return section('分析参数',fields)+(roi?section('选中测量区域',`<dl class="property-list"><div><dt>位置</dt><dd>${roi.x}, ${roi.y} px</dd></div><div><dt>尺寸</dt><dd>${roi.width} × ${roi.height} px</dd></div><div><dt>角色</dt><dd>${roi.role==='control'?'内参':'目标蛋白'}</dd></div></dl><div class="field-grid">${[['x','X / px'],['y','Y / px'],['width','宽度 / px'],['height','高度 / px']].map(([key,label])=>`<label class="field"><span>${label}</span><input id="roi-geometry-${key}" type="number" min="${key==='x'||key==='y'?0:1}" step="1" data-roi-field="${key}" data-roi-id="${roi.id}" value="${roi[key]}"></label>`).join('')}</div><p class="microcopy">${e(rawSourceLabel(roi.source || {asset_id:roi.asset_id,series:0,frame:0,channel:0}))}</p><button class="button small full danger-hover" data-action="delete-roi" data-id="${roi.id}">删除此区域</button>`):'')+section('结果与复算',`<p class="section-description">${selection.result?'结果记录原始通道、测量区域与参数。更改分析设置后，请重新计算。':'完成分析后，这里可以下载结果表；可生成的图表会在结果区提供入口。'}</p><button class="button full" data-action="download-results" ${selection.result?'':'disabled'}>${icon('download')}下载当前结果</button>`);
}
