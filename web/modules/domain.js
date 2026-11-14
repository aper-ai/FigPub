import {channelLayout} from './channel-domain.js';
import {alphabeticLabel, ensureCompositionSettings, reconcilePanelLabels} from './layout-engine.js';
/** Coordinates are source pixels, layout millimeters, text points. */
export const clone = value => structuredClone(value);
export const uid = (prefix = 'item') => `${prefix}_${crypto.randomUUID().slice(0, 12)}`;
export const clamp = (value, minimum, maximum) => Math.max(minimum, Math.min(maximum, value));
export const number = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;
export const round = (value, digits = 2) => Math.round(value * 10 ** digits) / 10 ** digits;
export const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
export const figureFont=text=>/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}\u3000-\u303f\uff00-\uffef]/u.test(String(text))?'Noto Sans CJK SC':'DejaVu Sans';
export const fullCrop = asset => ({x: 0, y: 0, width: Math.max(1, asset?.width_px || 1000), height: Math.max(1, asset?.height_px || 750)});
export const emptyGel = () => ({lane_centers: [], lane_names: [], marker_points: [], marker_unit: 'kDa', hidden_lanes: []});
export const emptyDisplay = () => ({low: null, high: null, gamma: 1, invert: false});
export const defaultEdit = asset => ({crop: fullCrop(asset), rotation: 0, flip_h: false, flip_v: false, display: emptyDisplay(), gel: emptyGel()});
export const assetFor = (project, id) => project?.assets?.find(asset => asset.id === id);
export const panelFor = (project, id) => project?.panels?.find(panel => panel.id === id);
export const isPicture = asset => asset && asset.kind !== 'table';
export function ensureProject(project) {
  project.assets ||= []; project.panels ||= []; project.annotations ||= []; project.measurements ||= []; project.audit ||= [];
  project.figure ||= {width_mm: 180, height_mm: 120, background: '#ffffff'};
  project.settings ||= {}; project.settings.min_ppi ||= 300; project.settings.font_family ||= 'DejaVu Sans';
  project.settings.asset_edits ||= {}; project.settings.analysis_rois ||= [];project.settings.analysis_regions ||= {};project.settings.analysis_channel_selections ||= {};
  ensureCompositionSettings(project);
  reconcilePanelLabels(project);
  for(const [assetId,edit] of Object.entries(project.settings.asset_edits)){
    edit.crop ||= fullCrop(assetFor(project,assetId));edit.display ||= emptyDisplay();edit.gel ||= emptyGel();
    edit.gel.lane_centers ||= [];edit.gel.lane_names ||= [];edit.gel.marker_points ||= [];edit.gel.hidden_lanes ||= [];
  }
  for (const panel of project.panels) {
    const asset = assetFor(project, panel.asset_id); panel.crop ||= fullCrop(asset); panel.display ||= emptyDisplay(); panel.gel ||= emptyGel();
    panel.gel.lane_centers ||= []; panel.gel.lane_names ||= []; panel.gel.marker_points ||= []; panel.gel.hidden_lanes ||= [];
  }
  return project;
}
export function effectivePpi(panel, asset) {
  if (!panel || !asset || asset.kind === 'table') return null;
  if (['pdf', 'svg'].includes(asset.kind)) return null;
  const crop = panel.crop || fullCrop(asset),segments=retainedSegments(panel,asset);
  if(panel.channel_view?.mode==='split') {const layout=channelLayout(panel.channel_view,Math.max(asset.width_px,crop.x+crop.width),Math.max(asset.height_px,crop.y+crop.height),crop);return Math.min(layout.width_px/panel.width_mm,layout.height_px/panel.height_mm)*25.4;}
  return Math.min(...segments.map(segment=>segment.crop.width/Math.max(.01,panel.width_mm*segment.fraction)*25.4),crop.height/Math.max(.01,panel.height_mm)*25.4);
}
/** Derive a view of kept source intervals; removed pixels are never painted over. */
export function retainedSegments(panel,asset) {
  const crop=panel.crop || fullCrop(asset),centers=panel.gel?.lane_centers || [],hidden=new Set(panel.gel?.hidden_lanes || []);
  if(!centers.length || !hidden.size)return [{crop,offset:0,fraction:1}];
  const boundaries=[crop.x,...centers.slice(1).map((center,index)=>(centers[index]+center)/2),crop.x+crop.width],intervals=[];
  for(let index=0;index<centers.length;index++){
    const left=Math.max(crop.x,Math.round(boundaries[index])),right=Math.min(crop.x+crop.width,Math.round(boundaries[index+1]));
    if(hidden.has(index)||right<=left)continue;
    if(intervals.length&&intervals.at(-1).right===left)intervals.at(-1).right=right;else intervals.push({left,right});
  }
  if(!intervals.length)return [];
  const available=1-.025*(intervals.length-1),pixels=intervals.reduce((sum,interval)=>sum+interval.right-interval.left,0);
  let offset=0;
  return intervals.map(interval=>{const fraction=(interval.right-interval.left)/pixels*available,segment={crop:{x:interval.left,y:crop.y,width:interval.right-interval.left,height:crop.height},offset,fraction};offset+=fraction+.025;return segment;});
}
export function sourceXInPanel(panel,asset,x) {
  for(const segment of retainedSegments(panel,asset)){
    if(x>=segment.crop.x&&x<=segment.crop.x+segment.crop.width){const relative=segment.offset+(x-segment.crop.x)/segment.crop.width*segment.fraction;return panel.flip_h?1-relative:relative;}
  }
  return null;
}
export const analysisChartTypes=Object.freeze(['scatter','bar','box']);
export const isAnalysisChartType=value=>analysisChartTypes.includes(value);

/** A scale is always supplied for original pixels, never inferred from the display. */
export function scaleBarDraft(panel) {
  const bar=panel.scale_bar || panel.scale_bar_draft || {};
  return {um_per_px:bar.um_per_px ?? null,length_um:bar.length_um ?? null,color:bar.color || '#ffffff'};
}
export function scaleBarGeometry(panel,asset,bar=scaleBarDraft(panel)) {
  if(asset?.kind!=='image')return {valid:false,message:'比例尺需要具有原始像素尺寸的图像。'};
  if(!Number.isFinite(bar.um_per_px)||bar.um_per_px<=0)return {valid:false,message:'请填写大于 0 的标定像素尺度。'};
  if(!Number.isFinite(bar.length_um)||bar.length_um<=0)return {valid:false,message:'请填写大于 0 的比例尺长度。'};
  if(!/^#[0-9a-f]{6}$/i.test(bar.color || '#ffffff'))return {valid:false,message:'请选择有效的比例尺颜色。'};
  const crop=panel.crop || fullCrop(asset),layout=panel.channel_view?.mode==='split'?channelLayout(panel.channel_view,Math.max(asset.width_px,crop.x+crop.width),Math.max(asset.height_px,crop.y+crop.height),crop):null,widthMm=layout?panel.width_mm*layout.crop.width/layout.width_px:panel.width_mm;
  if(!Number.isFinite(crop.width)||crop.width<=0||!Number.isFinite(widthMm)||widthMm<=0)return {valid:false,message:'当前图像裁切或面板尺寸无效。'};
  const sourceLengthPx=bar.length_um/bar.um_per_px,lengthMm=sourceLengthPx/(layout?.crop.width || crop.width)*widthMm;
  if(!Number.isFinite(lengthMm)||lengthMm<=0||lengthMm>widthMm*.9)return {valid:false,message:'比例尺长度须在当前裁切宽度的 90% 以内。'};
  return {valid:true,length_mm:lengthMm,source_length_px:sourceLengthPx};
}
/** Disabled or incomplete settings remain editable but are excluded from export. */
export function updateScaleBar(panel,asset,patch={},enabled=Boolean(panel.scale_bar)) {
  const draft={...scaleBarDraft(panel),...patch},geometry=scaleBarGeometry(panel,asset,draft);
  panel.scale_bar_draft=draft;
  if(enabled&&geometry.valid)panel.scale_bar={...draft};else delete panel.scale_bar;
  return geometry;
}
export function previewUrl(asset, display = {}) {
  const base = asset.preview_url || `/api/assets/${encodeURIComponent(asset.id)}/preview`;
  const url = new URL(base, location.origin); url.searchParams.set('max_size', '1600');
  for (const key of ['low', 'high', 'gamma', 'invert']) if (display[key] !== undefined && display[key] !== null) url.searchParams.set(key, String(display[key]));
  return url.pathname + url.search;
}
export function panelFromAsset(project, asset, edit = defaultEdit(asset)) {
  const layout=edit.channel_view?.mode==='split'?channelLayout(edit.channel_view,Math.max(asset.width_px,edit.crop.x+edit.crop.width),Math.max(asset.height_px,edit.crop.y+edit.crop.height),edit.crop):null;
  const widthMm = Math.min(72, project.figure.width_mm - 16), heightMm = widthMm * (layout?layout.height_px/layout.width_px:edit.crop.height / edit.crop.width);
  const count = project.panels.length, column = count % 2, row = Math.floor(count / 2);
  const maxHeight = Math.max(12, project.figure.height_mm - 16);
  const actualHeight = Math.min(heightMm, maxHeight), actualWidth = actualHeight === heightMm ? widthMm : widthMm * actualHeight / heightMm;
  return {...clone(edit), id: uid('panel'), asset_id: asset.id,
    x_mm: clamp(8 + column * (actualWidth + 8), 0, Math.max(0, project.figure.width_mm - actualWidth)),
    y_mm: clamp(10 + row * (actualHeight + 8), 0, Math.max(0, project.figure.height_mm - actualHeight)),
    width_mm: round(actualWidth), height_mm: round(actualHeight), label: alphabeticLabel(count), label_mode: 'auto', title: ''};
}
export function normalizeRoi(start, end, asset) {
  const x = clamp(Math.min(start.x, end.x), 0, asset.width_px-1), y = clamp(Math.min(start.y, end.y), 0, asset.height_px-1);
  const right=clamp(Math.max(start.x,end.x),x+1,asset.width_px),bottom=clamp(Math.max(start.y,end.y),y+1,asset.height_px);
  return {x:Math.round(x),y:Math.round(y),width:Math.max(1,Math.round(right)-Math.round(x)),height:Math.max(1,Math.round(bottom)-Math.round(y))};
}
export function formatValue(value) {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'number') return Number.isFinite(value) ? value === 0 ? '0' : Math.abs(value) < .0001 || Math.abs(value) >= 1e7 ? value.toExponential(4) : value.toLocaleString('en-US', {maximumFractionDigits: 5}) : '—';
  if (Array.isArray(value)) return value.join(', ');
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}
export function tableCsv(rows) {
  if (!rows?.length) return '';
  const columns = [...new Set(rows.flatMap(row => Object.keys(row)))];
  const quote = value => {
    let text=String(value==null?'':typeof value==='object'?JSON.stringify(value):value);
    // Match reviewed-instance CSV: labels stay literal in spreadsheet imports;
    // actual numeric measurements retain their sign and full stored precision.
    if(typeof value==='string'&&(/^\s*[=+@-]/.test(text)||/^[\t\r]/.test(text)))text=`'${text}`;
    return `"${text.replaceAll('"', '""')}"`;
  };
  return '\ufeff' + [columns.map(quote).join(','), ...rows.map(row => columns.map(key => quote(row[key])).join(','))].join('\r\n');
}
