import {clone,isAnalysisChartType} from './domain.js';
import {sourceIdentityKey} from './channel-domain.js';
import {advancedMethods,advancedDefaults,buildAdvancedParams} from './histology-domain.js';

export const microscopyMethods=Object.freeze([
  ['nuclei','细胞核分割'],['intensity','单通道区域强度'],['channel_intensity','多通道区域强度'],
  ['threshold','阈值阳性区域'],['channel_overlap','通道重叠与相关'],['line_profile','线强度剖面'],
  ['ihc','IHC 染色分析'],['cellpose','Cellpose 细胞分割'],
  ...advancedMethods,
]);
export const statisticalTests=Object.freeze([['none','不进行显著性检验'],['welch_t','Welch t · 独立样本'],['student_t','Student t · 等方差'],['paired_t','配对 t · 匹配生物学 ID'],['anova_tukey','ANOVA + Tukey']]);
const multiMethods=new Set(['channel_intensity','threshold','channel_overlap','line_profile','fiber_cross_section','myotube']);
const rgbMethods=new Set(['ihc','ihc_advanced']);
export function defaultAnalysisParams() {
  return {background:'border',polarity:'dark',radius:50,selected_exposures:{},reference_gene:'',reference_genes:[],control_group:'',test:'none',
    method:'nuclei',channel:0,min_area:20,threshold:'',threshold_method:'otsu',thresholds:{},um_per_px:'',background_method:'none',background_values:{},
    samples:256,line_width_px:1,model:'cyto3',diameter_px:30,flow_threshold:.4,cellprob_threshold:0,
    sheet:null,group_mapping:{},excluded_source_rows:[],max_ct:null,technical_sd_limit:.5,wb_draft_scope_id:'',...clone(advancedDefaults)};
}
export function restoredAnalysisParams(settings) {return {...defaultAnalysisParams(),...clone(settings?.analysis_params || {})};}
export function wbScopedRois(rois,scopeId) {
  if(!scopeId)return rois;
  const selected=rois.filter(roi=>roi.wb_draft_id===scopeId),controls=new Set(selected.map(roi=>roi.control_roi_id).filter(Boolean));
  return rois.filter(roi=>roi.wb_draft_id===scopeId||controls.has(roi.id));
}
const optionalNumber=value=>value===''||value==null?undefined:Number(value);
export function canonicalSource(source={},assetId=null) {
  return {asset_id:source.asset_id || assetId,series:Number(source.series ?? 0),frame:Number(source.frame ?? 0),channel:Number(source.channel ?? 0)};
}
function validSource(source) {
  if(!source.asset_id||!['series','frame','channel'].every(key=>Number.isInteger(source[key])&&source[key]>=0))throw new Error('请选择有效的原始图像平面和通道');
  return source;
}
function canonicalRegion(region) {
  if(!region)return undefined;
  return {...clone(region),type:region.type || 'rectangle'};
}
function canonicalParams(kind,params) {
  const output=clone(params || {});
  if(kind==='wb'||kind==='microscopy')output.channel_axis=Number(output.channel_axis ?? -1);
  if(kind==='wb') {
    output.background ||= 'border';output.polarity ||= 'dark';output.test ||= 'none';output.control_group=String(output.control_group || '').trim();output.selected_exposures ||= {};
    if(output.background==='rolling_ball')output.radius=optionalNumber(output.radius) ?? 25;else delete output.radius;
    if(Array.isArray(output.rois))output.rois=output.rois.map(roi=>({...roi,source:canonicalSource(roi.source || {series:output.series,frame:output.frame ?? output.page,channel:output.channel},roi.asset_id)})).sort((a,b)=>String(a.id || '').localeCompare(String(b.id || '')));
  } else if(kind==='qpcr') {
    output.reference_genes=[...new Set((output.reference_genes?.length?output.reference_genes:[output.reference_gene]).map(item=>String(item || '').trim()).filter(Boolean))].sort();delete output.reference_gene;
    output.control_group=String(output.control_group ?? '').trim();output.method ||= 'delta_delta_ct';output.test ||= 'none';
    output.group_mapping ||= {};output.excluded_source_rows=(output.excluded_source_rows || []).map(Number).sort((a,b)=>a-b);
    output.sheet ||= null;output.max_ct=optionalNumber(output.max_ct) ?? null;output.technical_sd_limit=Number(output.technical_sd_limit ?? .5);
  } else if(kind==='microscopy') {
    output.method ||= 'nuclei';
    if(rgbMethods.has(output.method)) {
      output.source=canonicalSource(output.source || {series:output.series,frame:output.frame ?? output.page},output.asset_id);output.source.channel=0;
      delete output.channels;
    } else {
      const channels=output.channels?.length?output.channels:[{id:`c${Number(output.channel ?? output.source?.channel ?? 0)}`,source:canonicalSource(output.source || {series:output.series,frame:output.frame ?? output.page,channel:output.channel},output.asset_id)}];
      output.channels=channels.map(channel=>{
        const canonical={...channel,source:canonicalSource(channel.source || channel,channel.asset_id || output.asset_id)};
        delete canonical.name;delete canonical.asset_id;delete canonical.channel;delete canonical.series;delete canonical.frame;
        return canonical;
      }).sort((a,b)=>String(a.id).localeCompare(String(b.id)));
      delete output.source;
    }
    for(const key of ['asset_id','series','frame','page','channel'])delete output[key];
    if(['nuclei','ihc','cellpose','fiber_cross_section','myotube'].includes(output.method))output.min_area=Number(output.min_area ?? 20);else delete output.min_area;
    for(const key of ['threshold','um_per_px']) {const value=optionalNumber(output[key]);if(value===undefined)delete output[key];else output[key]=value;}
    if(output.method==='intensity')delete output.threshold;
    if(output.roi==null||output.method==='line_profile')delete output.roi;else output.roi=canonicalRegion(output.roi);
    if(output.background_roi)output.background_roi=canonicalRegion(output.background_roi);
  }
  return output;
}
function orderedJson(value) {
  if(typeof value==='number'&&!Number.isFinite(value))throw new Error('分析输入包含无效数字');
  if(Array.isArray(value))return value.map(orderedJson);
  if(value&&typeof value==='object')return Object.fromEntries(Object.keys(value).sort().filter(key=>value[key]!==undefined).map(key=>[key,orderedJson(value[key])]));
  return value;
}
export function analysisInputSignature(kind,params) {
  if(!params||!['wb','qpcr','microscopy'].includes(kind))return null;
  try{return JSON.stringify({kind,params:orderedJson(canonicalParams(kind,params))});}catch{return null;}
}
export function analysisSourceAssetIds(kind,params) {
  const ids=kind==='wb'?(params?.rois || []).map(roi=>roi.source?.asset_id || roi.asset_id):kind==='microscopy'&&params?.channels?.length?params.channels.map(channel=>channel.source?.asset_id || channel.asset_id || params.asset_id):[params?.source?.asset_id || params?.asset_id];
  return [...new Set(ids.filter(id=>typeof id==='string'&&id.length))].sort();
}
export function validateRegion(region) {
  if(!region)return undefined;
  const result=canonicalRegion(region);
  if(!['rectangle','ellipse','polygon'].includes(result.type))throw new Error('请选择矩形、椭圆或多边形分析区域');
  if(!['x','y','width','height'].every(key=>Number.isFinite(result[key]))||result.x<0||result.y<0||result.width<=0||result.height<=0)throw new Error('分析区域坐标或尺寸无效');
  if(result.type==='polygon'&&(!Array.isArray(result.points)||result.points.length<3||result.points.some(point=>!Array.isArray(point)||point.length!==2||point.some(value=>!Number.isFinite(value)||value<0))))throw new Error('多边形至少需要三个有效顶点');
  return result;
}
function validatedTest(value,controlGroup) {
  const test=value || 'none';if(!statisticalTests.some(item=>item[0]===test))throw new Error('请选择已支持的统计检验');
  if(test!=='none'&&test!=='anova_tukey'&&!controlGroup)throw new Error('比较检验需要先指定对照组');return test;
}
function positive(value,label,{integer=false,min=Number.MIN_VALUE,max=Infinity}={}) {
  const number=Number(value);if(!Number.isFinite(number)||number<min||number>max||(integer&&!Number.isInteger(number)))throw new Error(`${label}${min===Number.MIN_VALUE?'必须大于 0':`须至少为 ${min}`}${max<Infinity?` 且不超过 ${max}`:''}${integer?'，并使用整数':''}`);return number;
}
/** One request builder drives submission, stale detection, and chart eligibility. */
export function buildAnalysisParams({kind,asset,rois=[],values=defaultAnalysisParams(),roi=null,channels=null,source=null,geometry=null}) {
  const params={...defaultAnalysisParams(),...values};let request;
  if(kind==='wb') {
    rois=wbScopedRois(rois,params.wb_draft_scope_id);
    if(asset?.kind!=='image')throw new Error('请先选择需要测量的 WB 原始图像，PDF 和 SVG 请用于组图');
    if(!rois.length)throw new Error('请先在原图框选测量区域');
    const current=source?canonicalSource(source):null;
    if(!rois.some(item=>current?sourceIdentityKey(canonicalSource(item.source,item.asset_id))===sourceIdentityKey(current):(item.source?.asset_id || item.asset_id)===asset.id))throw new Error('当前原始平面没有测量区域，请选择已有 ROI 的通道或框选区域');
    request={rois:clone(rois),background:params.background,polarity:params.polarity};
    for(const key of ['adc_bits','black_level','saturation_value']) {
      const value=optionalNumber(params[key]);if(value===undefined)continue;
      if(!Number.isFinite(value)||(key==='adc_bits'&&(!Number.isInteger(value)||value<1||value>32)))throw new Error('请填写有效探测器范围，ADC 位数须为 1–32 的整数');
      request[key]=value;
    }
    if(!['border','rolling_ball','none'].includes(request.background)||!['dark','bright'].includes(request.polarity))throw new Error('请选择有效的条带极性和背景方法');
    if(request.background==='rolling_ball')request.radius=positive(params.radius ?? 50,'滚动球半径',{integer:true,max:100});
    if(params.control_group)request.control_group=String(params.control_group).trim();
    const test=validatedTest(params.test,request.control_group);if(test!=='none')request.test=test;
    const choices={};for(const [sample,exposure] of Object.entries(params.selected_exposures || {})) {
      const regions=rois.filter(roi=>roi.sample_id===sample);if(!regions.length||exposure==='')continue;
      if(!regions.some(roi=>String(roi.exposure_id || '1')===String(exposure)))throw new Error(`样本 ${sample} 所选曝光已不在当前 ROI 中，请重新选择`);choices[sample]=String(exposure);
    }
    if(Object.keys(choices).length)request.selected_exposures=choices;
  } else if(kind==='qpcr') {
    if(asset?.kind!=='table')throw new Error('请先选择 qPCR 数据表');
    const references=(params.reference_genes?.length?params.reference_genes:String(params.reference_gene || '').split(/[,，;；\n]/)).map(item=>String(item).trim()).filter(Boolean);
    request={asset_id:asset.id,reference_genes:[...new Set(references)],control_group:String(params.control_group || '').trim(),method:'delta_delta_ct'};
    if(!request.reference_genes.length||!request.control_group)throw new Error('请填写内参基因和对照组的准确名称');
    request.test=validatedTest(params.test,request.control_group);request.sheet=params.sheet || null;
    request.group_mapping=clone(params.group_mapping || {});request.excluded_source_rows=[...new Set(params.excluded_source_rows || [])].map(Number).sort((a,b)=>a-b);
    if(request.excluded_source_rows.some(value=>!Number.isInteger(value)||value<1))throw new Error('排除孔需要有效的原始行号');
    request.max_ct=optionalNumber(params.max_ct) ?? null;if(request.max_ct!==null)positive(request.max_ct,'Ct 上限');
    request.technical_sd_limit=positive(params.technical_sd_limit,'技术重复 SD 上限');
  } else if(kind==='microscopy') {
    if(asset?.kind!=='image')throw new Error('请选择显微原始图像，PDF 和 SVG 请用于组图');
    if(!microscopyMethods.some(item=>item[0]===params.method))throw new Error('请选择有效的显微分析方法');
    request={method:params.method};
    let selected=channels===null?null:clone(channels);
    if(selected!==null&&!selected.length&&!rgbMethods.has(params.method))throw new Error('请明确勾选至少一个待分析的原始通道');
    if(selected===null) {
      const channel=Number(params.channel);
      if(!Number.isInteger(channel)||channel<0||channel>=(asset.channels || 1))throw new Error('通道索引超出当前图像范围');
      request.asset_id=asset.id;request.channel=channel;
    } else {
      selected=selected.map(channel=>({...channel,source:validSource(canonicalSource(channel.source,asset.id))}));
      const keys=selected.map(channel=>sourceIdentityKey(channel.source));if(new Set(keys).size!==keys.length)throw new Error('同一原始通道不能重复参与分析');
      if(!multiMethods.has(params.method)&&selected.length!==1&&!rgbMethods.has(params.method))throw new Error('此方法需要明确选择一个原始通道');
      if(!rgbMethods.has(params.method))request.channels=selected;
    }
    if(rgbMethods.has(params.method)) {
      const identity=validSource(canonicalSource(source || selected?.[0]?.source || {channel:0},asset.id));
      request.asset_id=identity.asset_id;request.series=identity.series;request.frame=identity.frame;delete request.channel;
    }
    if(params.method==='channel_overlap'&&(selected?.length || 0)<2)throw new Error('通道重叠分析至少需要两个独立原始通道');
    const scale=optionalNumber(params.um_per_px);if(scale!==undefined)request.um_per_px=positive(scale,'像素尺度');
    if(params.method!=='line_profile') {const region=validateRegion(geometry?.roi ?? roi);if(region)request.roi=region;}
    if(['nuclei','ihc','cellpose'].includes(params.method))request.min_area=positive(params.min_area,'最小面积',params.method==='cellpose'?{integer:true,max:16000000}:{});
    if(['nuclei','ihc'].includes(params.method)) {const threshold=optionalNumber(params.threshold);if(threshold!==undefined){if(!Number.isFinite(threshold))throw new Error('请输入有效阈值');request.threshold=threshold;}}
    if(['threshold','channel_overlap'].includes(params.method)) {
      request.threshold_method=params.threshold_method;if(!['manual','otsu','triangle','li'].includes(request.threshold_method))throw new Error('请选择有效阈值方法');
      if(request.threshold_method==='manual'){
        if(!selected?.length)throw new Error('请明确选择待分析通道');request.thresholds={};
        for(const channel of selected){const value=optionalNumber(params.thresholds?.[channel.id]?.value) ?? optionalNumber(params.threshold);if(!Number.isFinite(value))throw new Error(`请填写 ${channel.name || channel.id} 的原始强度阈值`);request.thresholds[channel.id]={method:'manual',value};}
      }
    }
    if(params.method==='channel_intensity') {
      request.background_method=params.background_method;
      if(!['none','roi','fixed'].includes(request.background_method))throw new Error('请选择有效背景方法');
      if(request.background_method==='roi'){request.background_roi=validateRegion(geometry?.background_roi);if(!request.background_roi)throw new Error('请绘制独立背景区域');}
      if(request.background_method==='fixed') {
        request.background_values={};for(const channel of selected || []){const value=optionalNumber(params.background_values?.[channel.id]);if(!Number.isFinite(value))throw new Error(`请填写 ${channel.name || channel.id} 的固定背景强度`);request.background_values[channel.id]=value;}
      }
    }
    if(params.method==='line_profile') {
      const line=geometry?.line;if(!line||!['x1','y1','x2','y2'].every(key=>Number.isFinite(line[key])&&line[key]>=0)||Math.hypot(line.x2-line.x1,line.y2-line.y1)<1)throw new Error('请在原图上绘制至少 1 px 长的采样线');
      request.line=clone(line);request.samples=positive(params.samples,'采样点数',{integer:true,min:2,max:4096});request.line_width_px=positive(params.line_width_px,'采样线宽',{integer:true,max:101});
    }
    if(params.method==='cellpose') {
      if(!['cyto3','nuclei'].includes(params.model))throw new Error('请选择已支持的本地 Cellpose 模型');
      request.model=params.model;request.diameter_px=positive(params.diameter_px,'细胞直径',{min:2,max:2000});
      request.flow_threshold=Number(params.flow_threshold);request.cellprob_threshold=Number(params.cellprob_threshold);
      if(!Number.isFinite(request.flow_threshold)||request.flow_threshold<0||request.flow_threshold>10||!Number.isFinite(request.cellprob_threshold)||request.cellprob_threshold < -20||request.cellprob_threshold>20)throw new Error('Cellpose Flow 阈值须为 0–10，细胞概率阈值须为 −20–20');
    }
    if(advancedMethods.some(item=>item[0]===params.method))Object.assign(request,buildAdvancedParams(params,geometry,selected || []));
  } else throw new Error('未知分析类型');
  if(!analysisInputSignature(kind,request))throw new Error('分析输入包含无效数字');return request;
}
export function resolveAnalysisResult({project,kind,assetId,values,roi=null,channels=null,source=null,geometry=null,transientRecord=null}) {
  const asset=project?.assets?.find(item=>item.id===assetId);let input;
  try{const params=buildAnalysisParams({kind,asset,rois:project?.settings?.analysis_rois || [],values,roi,channels,source,geometry});input={kind,params,signature:analysisInputSignature(kind,params),error:null};}
  catch(error){input={kind,params:null,signature:null,error:error.message};}
  const saved=project?.measurements || [],records=[...saved];
  if(transientRecord&&project&&transientRecord.project_id===project.id&&!saved.some(item=>item.id===transientRecord.id))records.push(transientRecord);
  const desiredSources=input.params?analysisSourceAssetIds(kind,input.params):channels?.length?channels.map(channel=>channel.source.asset_id):[source?.asset_id || assetId];
  const candidates=records.reverse().filter(record=>{
    if(record.kind!==kind||!record.result||!asset)return false;
    const sources=analysisSourceAssetIds(kind,record.params);
    return desiredSources.some(id=>sources.includes(id))&&(!record.source_asset_ids?.length||desiredSources.some(id=>record.source_asset_ids.includes(id)));
  });
  const matching=input.signature?candidates.find(record=>analysisInputSignature(kind,record.params)===input.signature):null,record=matching || candidates[0] || null;
  return {record,result:record?.result || null,input,stale:Boolean(record&&(!input.signature||analysisInputSignature(kind,record.params)!==input.signature)),persisted:Boolean(record&&saved.some(item=>item===record))};
}
export function buildAnalysisChartRequest(selection,chartType='scatter') {
  if(!selection.record||selection.stale)throw new Error('请使用当前测量区域和参数重新分析后生成图表');
  if(!selection.persisted)throw new Error('这份结果尚未保存到当前项目，请重新运行分析');
  if(!isAnalysisChartType(chartType))throw new Error('请选择已支持的图表类型');
  return {measurement_id:selection.record.id,chart_type:chartType,width_mm:80,height_mm:60};
}
