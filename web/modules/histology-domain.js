/** Pure contracts for calibrated IHC and fiber measurements. No UI state. */
export const advancedMethods=Object.freeze([
  ['ihc_advanced','高级 IHC · 染色与强度分级'],
  ['fiber_cross_section','肌纤维横截面 · 形态与核归属'],
  ['myotube','肌管 · 长宽与融合指数'],
]);
export const advancedDefaults=Object.freeze({
  stain_method:'macenko',grading_method:'manual',grade_threshold_1:.2,grade_threshold_2:.5,grade_threshold_3:.8,
  tissue_min_od:.15,macenko_percentile:1,rgb_white_level:'',profile_name:'IHC 批次校准',locked_profile:null,
  stain_h_vector:'0.650, 0.704, 0.286',stain_dab_vector:'0.268, 0.570, 0.776',
  segmentation:'threshold',fiber_channel_id:'',label_channel_id:'',fiber_input:'filled',fiber_polarity:'bright',
  fiber_threshold_method:'otsu',fiber_threshold:'',fiber_min_area:50,fiber_max_area:'',fiber_sigma:1,
  fiber_split_touching:true,fiber_min_distance:12,exclude_border:false,membrane_close_radius:1,
  nucleus_channel_id:'',nucleus_label_channel_id:'',nucleus_threshold_method:'otsu',nucleus_threshold:'',
  nucleus_polarity:'bright',nucleus_min_area:12,nucleus_max_area:'',nucleus_sigma:1,nucleus_min_distance:5,
  nucleus_min_overlap:.5,peripheral_fraction:.2,fusion_min_nuclei:2,fiber_markers:[],
});
export const isAdvancedMethod=method=>advancedMethods.some(item=>item[0]===method);
const copy=value=>value===undefined?undefined:JSON.parse(JSON.stringify(value));
const empty=value=>value===''||value==null;
function numeric(value,label,{min=0,max=Infinity,integer=false}={}) {
  if(empty(value)||typeof value==='boolean')throw new Error(`请填写${label}`);
  const n=Number(value);
  if(!Number.isFinite(n)||n<min||n>max||(integer&&!Number.isInteger(n)))throw new Error(`${label}超出有效范围`);
  return n;
}
function vector(value,label) {
  const values=Array.isArray(value)?value:String(value).split(/[,，\s]+/).filter(Boolean);
  if(values.length!==3)throw new Error(`${label}需要三个 RGB 光密度分量`);
  const result=values.map(item=>numeric(item,label));
  if(!result.some(item=>item>0))throw new Error(`${label}不能为零`);
  return result;
}
function choice(value,allowed,label) {if(!allowed.includes(value))throw new Error(`请选择有效的${label}`);return value;}
export function buildAdvancedParams(input,geometry={},channels=[]) {
  const params={...advancedDefaults,...input},out={};
  if(params.method==='ihc_advanced') {
    if(params.locked_profile) {
      const profile=params.locked_profile;
      if(profile.version!==1||profile.measurement_basis!=='natural_log_OD'||!Array.isArray(profile.grade_thresholds))throw new Error('锁定 IHC 校准格式无效');
      out.locked_profile=copy(profile);
    } else {
      out.stain_method=choice(params.stain_method,['macenko','fixed'],'染色向量方法');
      out.grading_method=choice(params.grading_method,['manual','gmm','examples'],'分级方法');
      out.macenko_percentile=numeric(params.macenko_percentile,'Macenko 百分位',{min:.01,max:10});
      if(out.stain_method==='fixed')out.stain_vectors=[vector(params.stain_h_vector,'H 向量'),vector(params.stain_dab_vector,'DAB 向量')];
      if(out.grading_method!=='gmm') {
        out.grade_thresholds=[1,2,3].map(i=>numeric(params[`grade_threshold_${i}`],`第 ${i} 条分级界线`));
        if(!(out.grade_thresholds[0]<out.grade_thresholds[1]&&out.grade_thresholds[1]<out.grade_thresholds[2]))throw new Error('分级阈值必须严格递增：阳性 < 中等 < 强');
      }
      if(out.grading_method==='examples') {
        if(!geometry?.ihc_negative||!geometry?.ihc_positive)throw new Error('请分别绘制阴性与阳性示例区域');
        out.example_rois={negative:copy(geometry.ihc_negative),positive:copy(geometry.ihc_positive)};
      }
      out.tissue_min_od=numeric(params.tissue_min_od,'组织光密度阈值');
      if(!empty(params.rgb_white_level))out.rgb_white_level=numeric(params.rgb_white_level,'RGB 白点',{min:Number.MIN_VALUE});
    }
    out.profile_name=String(params.profile_name || 'IHC 批次校准').slice(0,200);
    out.exclude_rois=copy(geometry?.ihc_exclusions || []);
    return out;
  }
  if(!['fiber_cross_section','myotube'].includes(params.method))return out;
  const ids=new Set(channels.map(channel=>channel.id));
  const pick=(value,label,optional=false)=>{if(empty(value)&&optional)return undefined;if(!ids.has(value))throw new Error(`请明确选择${label}通道`);return value;};
  out.fiber_channel_id=pick(params.fiber_channel_id || channels[0]?.id,'纤维结构');
  out.segmentation=choice(params.segmentation,['threshold','labels','cellpose'],'纤维分割方法');
  if(out.segmentation==='labels') {
    out.label_channel_id=pick(params.label_channel_id,'实例标签');
    if(out.label_channel_id===out.fiber_channel_id)throw new Error('结构强度与实例标签必须使用不同原始通道');
  }
  out.min_area=numeric(params.fiber_min_area,'纤维最小面积',{min:1,max:16000000,integer:true});
  if(!empty(params.fiber_max_area))out.max_area=numeric(params.fiber_max_area,'纤维最大面积',{min:out.min_area,max:16000000,integer:true});
  out.exclude_border=Boolean(params.exclude_border);out.include_contours=true;
  if(out.segmentation==='threshold') {
    out.fiber_input=choice(params.fiber_input,['filled','membrane'],'纤维输入');
    out.polarity=choice(params.fiber_polarity,['bright','dark'],'结构极性');
    out.threshold_method=choice(params.fiber_threshold_method,['otsu','manual'],'结构阈值方法');
    if(out.threshold_method==='manual')out.threshold=numeric(params.fiber_threshold,'结构原始阈值',{min:-Infinity});
    out.sigma=numeric(params.fiber_sigma,'结构平滑',{max:20});out.split_touching=Boolean(params.fiber_split_touching);
    out.min_distance=numeric(params.fiber_min_distance,'结构种子间距',{min:1,max:1000,integer:true});
    out.membrane_close_radius=numeric(params.membrane_close_radius,'膜闭合半径',{max:20,integer:true});
  }
  if(out.segmentation==='cellpose') {
    out.model='cyto3';out.diameter_px=numeric(params.diameter_px ?? 90,'预期纤维直径',{min:2,max:2000});
    out.flow_threshold=numeric(params.flow_threshold ?? .4,'Flow 阈值',{max:10});
    out.cellprob_threshold=numeric(params.cellprob_threshold ?? 0,'细胞概率阈值',{min:-20,max:20});
  }
  const nucleusLabel=pick(params.nucleus_label_channel_id,'细胞核实例标签',true),nucleus=pick(params.nucleus_channel_id,'细胞核强度',true);
  if(nucleusLabel)out.nucleus_label_channel_id=nucleusLabel;
  else if(nucleus)out.nucleus_channel_id=nucleus;
  if(nucleusLabel||nucleus) {
    if([nucleusLabel,nucleus].includes(out.fiber_channel_id))throw new Error('细胞核与纤维结构通道须明确分开');
    out.nucleus_min_area=numeric(params.nucleus_min_area,'细胞核最小面积',{min:1,max:16000000,integer:true});
    if(!empty(params.nucleus_max_area))out.nucleus_max_area=numeric(params.nucleus_max_area,'细胞核最大面积',{min:out.nucleus_min_area,max:16000000,integer:true});
    out.nucleus_threshold_method=choice(params.nucleus_threshold_method,['otsu','manual'],'细胞核阈值方法');
    if(nucleus&&!nucleusLabel&&out.nucleus_threshold_method==='manual')out.nucleus_threshold=numeric(params.nucleus_threshold,'细胞核原始阈值',{min:-Infinity});
    out.nucleus_polarity=choice(params.nucleus_polarity,['bright','dark'],'细胞核极性');
    out.nucleus_sigma=numeric(params.nucleus_sigma,'细胞核平滑',{max:20});
    out.nucleus_min_distance=numeric(params.nucleus_min_distance,'细胞核种子间距',{min:1,max:1000,integer:true});
  }
  out.nucleus_min_overlap=numeric(params.nucleus_min_overlap,'细胞核归属覆盖率',{min:.5,max:1});
  out.peripheral_fraction=numeric(params.peripheral_fraction,'周边带比例',{min:.01,max:.99});
  out.fusion_min_nuclei=numeric(params.fusion_min_nuclei,'融合肌管最少核数',{min:2,max:1000,integer:true});
  out.fiber_markers=(params.fiber_markers || []).map(marker=>({channel_id:pick(marker.channel_id,'分型标记'),
    name:String(marker.name || channels.find(channel=>channel.id===marker.channel_id)?.name || marker.channel_id).trim(),
    threshold:numeric(marker.threshold,'标记原始强度阈值',{min:-Infinity})}));
  if(new Set(out.fiber_markers.map(marker=>marker.name)).size!==out.fiber_markers.length)throw new Error('分型标记名称不可重复');
  return out;
}
