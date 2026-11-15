import {ensureCompositionSettings, reconcilePanelLabels, panelBounds as corePanelBounds, annotationBounds as coreAnnotationBounds} from '../modules/layout-engine.js';
import {LIMITS} from './archive.js';

export const uid = prefix => `${prefix}_${crypto.randomUUID().replaceAll('-', '').slice(0, 16)}`;
export const clone = value => structuredClone(value);
/** History stores editable recipes; immutable originals/results/extensions are shared by reference. */
export function recipeSnapshot(project) {
  return {...project, assets: [...project.assets], figure: clone(project.figure), panels: clone(project.panels), annotations: clone(project.annotations), settings: clone(project.settings)};
}
export function captureCommittedWorkspace(project, sources) { return {project: recipeSnapshot(project), sources: new Map(sources)}; }
export function hasWorkspaceContent(project) {
  return ['assets', 'panels', 'annotations', 'measurements'].some(key => project[key]?.length > 0) || project.revision > 0;
}
export const round = value => Math.round(value * 1000) / 1000;
export const escapeXml = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;'}[character]));
export function newProject() {
  const time = new Date().toISOString();
  const project = {schema_version: 1, id: uid('browser'), title: '未命名图稿', revision: 0, created_at: time, updated_at: time,
    assets: [], figure: {width_mm: 180, height_mm: 120, background: '#ffffff'}, panels: [], annotations: [], measurements: [], audit: [],
    settings: {min_ppi: 300, browser_studio: {version: '0.6.2'}, asset_edits: {}, analysis_rois: []}};
  ensureCompositionSettings(project); return project;
}
export function panelForAsset(asset) {
  return {id: uid('panel'), asset_id: asset.id, x_mm: 8, y_mm: 10, width_mm: 60, height_mm: 60 * asset.height_px / asset.width_px,
    crop: {x: 0, y: 0, width: asset.width_px, height: asset.height_px}, rotation: 0, flip_h: false, flip_v: false,
    display: {low: null, high: null, gamma: 1, invert: false}, gel: {lane_centers: [], lane_names: [], marker_points: [], hidden_lanes: [], marker_unit: 'kDa'},
    label: '', label_mode: 'auto', label_font_size_pt: 10, title_font_size_pt: 8, title: ''};
}
const finite = (value, minimum, maximum) => typeof value === 'number' && Number.isFinite(value) && value >= minimum && value <= maximum;
export function validateBrowserProject(project) {
  if (project?.schema_version !== 1 || !/^[A-Za-z0-9_-]{1,100}$/.test(project.id || '') || typeof project.title !== 'string' || project.title.length > 240 || !Number.isInteger(project.revision) || project.revision < 0) throw new Error('项目身份、标题或版本无效');
  if (!finite(project.figure?.width_mm, 20, 2000) || !finite(project.figure?.height_mm, 20, 2000) || !/^#[0-9a-f]{6}$/i.test(project.figure.background || '')) throw new Error('画布宽高须为 20–2000 mm，背景须为有效颜色');
  if (!Array.isArray(project.assets) || project.assets.length > LIMITS.assets || !Array.isArray(project.panels) || project.panels.length > LIMITS.panels || !Array.isArray(project.annotations) || project.annotations.length > 1000) throw new Error('项目对象数量超过浏览器版上限');
  const assets = new Map();
  for (const asset of project.assets) {
    if (!/^[a-f0-9]{64}$/.test(asset.id || '') || asset.sha256 !== asset.id || assets.has(asset.id) || !['image', 'svg'].includes(asset.kind) || !finite(asset.width_px, 1, 100000) || !finite(asset.height_px, 1, 100000) || asset.width_px * asset.height_px > LIMITS.pixels) throw new Error('浏览器组图版仅支持有完整原件的 PNG / JPEG / WebP / 安全 SVG 项目；复杂原始格式请在本机版打开。');
    assets.set(asset.id, asset);
  }
  const identifiers = new Set();
  for (const panel of project.panels) {
    if (!/^[A-Za-z0-9_-]{1,100}$/.test(panel.id || '') || identifiers.has(panel.id) || !assets.has(panel.asset_id)) throw new Error('面板身份重复或引用原件缺失');
    identifiers.add(panel.id); const asset = assets.get(panel.asset_id), crop = panel.crop;
    if (!finite(panel.x_mm, -2000, 4000) || !finite(panel.y_mm, -2000, 4000) || !finite(panel.width_mm, 0.1, 2000) || !finite(panel.height_mm, 0.1, 2000) || !finite(panel.rotation || 0, -36000, 36000)) throw new Error('面板位置或尺寸无效');
    if (!crop || !finite(crop.x, 0, asset.width_px - 0.001) || !finite(crop.y, 0, asset.height_px - 0.001) || !finite(crop.width, 0.001, asset.width_px) || !finite(crop.height, 0.001, asset.height_px) || crop.x + crop.width > asset.width_px + .001 || crop.y + crop.height > asset.height_px + .001) throw new Error('面板裁切超出原图');
    if (panel.channel_view || panel.series > 0 || panel.frame > 0 || panel.page > 0 || panel.channel > 0 || panel.display?.invert || panel.display?.low != null || panel.display?.high != null || (panel.display?.gamma ?? 1) !== 1 || panel.gel?.lane_centers?.length || panel.gel?.marker_points?.length || panel.gel?.hidden_lanes?.length) throw new Error('此项目包含通道、强度变换或凝胶专用标注。为完整保留呈现，请在本机工作台中继续编辑。');
    for (const key of ['label', 'title']) if (typeof panel[key] !== 'string' || panel[key].length > 240) throw new Error('面板编号或标题过长');
    if (!finite(panel.label_font_size_pt ?? 10, 1, 100) || !finite(panel.title_font_size_pt ?? 8, 1, 100)) throw new Error('编号和标题字号须为 1–100 pt');
    if (panel.group_id != null && !/^[A-Za-z0-9_-]{1,100}$/.test(panel.group_id)) throw new Error('图片编组身份无效');
    if (panel.scale_bar && (!finite(panel.scale_bar.um_per_px, 1e-9, 1e9) || !finite(panel.scale_bar.length_um, 1e-9, 1e9) || !/^#[a-f0-9]{6}$/i.test(panel.scale_bar.color))) throw new Error('比例尺校准无效');
    if (panel.scale_bar && panel.scale_bar.length_um / panel.scale_bar.um_per_px > panel.crop.width * .9) throw new Error('比例尺长度超过面板宽度的 90%，请在本机版调整校准或长度');
  }
  for (const annotation of project.annotations) {
    if (!['text', 'rect', 'line'].includes(annotation.type) || !/^[A-Za-z0-9_-]{1,100}$/.test(annotation.id || '') || identifiers.has(annotation.id) || !['x_mm', 'y_mm', 'width_mm', 'height_mm'].every(key => finite(annotation[key], -4000, 4000)) || !finite(annotation.font_size_pt || 8, 1, 100) || !/^#[a-f0-9]{6}$/i.test(annotation.color || '#222222') || typeof annotation.text !== 'string' || annotation.text.length > 2000) throw new Error('标注内容或坐标无效');
    identifiers.add(annotation.id);
    if (annotation.type === 'rect' && (annotation.width_mm <= 0 || annotation.height_mm <= 0)) throw new Error('矩形宽高须为正数');
    if (annotation.group_id) throw new Error('浏览器版与本机版目前仅支持图片面板编组；请先在原项目中解除文字或图形的混合编组');
    if (annotation.anchor != null && !['start', 'middle', 'end'].includes(annotation.anchor) || annotation.fill != null && annotation.fill !== 'none' && !/^#[a-f0-9]{6}$/i.test(annotation.fill) || annotation.line_width_pt != null && !finite(annotation.line_width_pt, 0, 100) || annotation.rotation) throw new Error('标注使用了浏览器版不支持的锚点、填充、描边或旋转，请在本机版继续编辑');
  }
  project.settings ||= {}; ensureCompositionSettings(project); return project;
}
export function panelBounds(panel) {
  const bounds = corePanelBounds(panel); return {x: bounds.x_mm, y: bounds.y_mm, width: bounds.width_mm, height: bounds.height_mm};
}
export function annotationBounds(annotation) { const bounds = coreAnnotationBounds(annotation); return {x: bounds.x_mm, y: bounds.y_mm, width: bounds.width_mm, height: bounds.height_mm}; }
/** Resolve selection against the current revision; restored groups can never be partly selected. */
export function expandedSelection(project, selection) {
  const objects = [...project.panels, ...project.annotations], selected = new Set(selection), groups = new Set(project.panels.filter(item => selected.has(item.id) && item.group_id).map(item => item.group_id));
  return new Set(objects.filter(item => selected.has(item.id) || item.asset_id && item.group_id && groups.has(item.group_id)).map(item => item.id));
}
export function selectedObjects(project, selection) { const ids = expandedSelection(project, selection); return [...project.panels, ...project.annotations].filter(item => ids.has(item.id)); }
function unionBounds(bounds) {
  if (!bounds.length) return {x: 0, y: 0, width: 0, height: 0};
  const x = Math.min(...bounds.map(item => item.x)), y = Math.min(...bounds.map(item => item.y));
  return {x, y, width: Math.max(...bounds.map(item => item.x + item.width)) - x, height: Math.max(...bounds.map(item => item.y + item.height)) - y};
}
export function selectionBounds(project, selection) { return unionBounds(selectedObjects(project, selection).map(item => item.asset_id ? panelBounds(item) : annotationBounds(item))); }
/** An existing image group is one alignment/distribution unit, with its internal spacing intact. */
export function selectionUnits(project, selection) {
  const units = new Map();
  for (const item of selectedObjects(project, selection)) {
    const key = item.asset_id && item.group_id ? `group:${item.group_id}` : `object:${item.id}`;
    if (!units.has(key)) units.set(key, {id: key, items: []}); units.get(key).items.push(item);
  }
  return [...units.values()].map(unit => ({...unit, bounds: unionBounds(unit.items.map(item => item.asset_id ? panelBounds(item) : annotationBounds(item)))}));
}
export function assertUngroupedLayout(project) {
  if (project.panels.some(panel => panel.group_id)) throw new Error('图稿包含图片编组。请先解组，再自动排列或重新排版；等比适配画布可保留编组。');
}
export function planUnitAlignment(project, selection, mode) {
  const units = selectionUnits(project, selection), distributed = mode.startsWith('distribute-');
  if (!['align-left', 'align-top', 'align-center', 'align-middle', 'distribute-x', 'distribute-y'].includes(mode)) throw new Error('对齐方式无效');
  if (units.length < (distributed ? 3 : 2)) throw new Error(distributed ? '等距分布至少需要 3 个独立面板或编组' : '对齐至少需要 2 个独立面板或编组');
  const all = unionBounds(units.map(unit => unit.bounds)), placements = [];
  const translate = (unit, dx, dy) => { for (const item of unit.items) placements.push({id: item.id, x_mm: item.x_mm + dx, y_mm: item.y_mm + dy}); };
  if (distributed) {
    const horizontal = mode === 'distribute-x', dimension = horizontal ? 'width' : 'height', coordinate = horizontal ? 'x' : 'y';
    const sorted = [...units].sort((a, b) => a.bounds[coordinate] - b.bounds[coordinate]), gap = (all[dimension] - units.reduce((sum, unit) => sum + unit.bounds[dimension], 0)) / (units.length - 1);
    if (gap < -.001) throw new Error('当前两端距离不足，无法无重叠等距分布。请拉开两端或先解组再自动排列。');
    let position = all[coordinate];
    for (const unit of sorted) { translate(unit, horizontal ? position - unit.bounds.x : 0, horizontal ? 0 : position - unit.bounds.y); position += unit.bounds[dimension] + gap; }
  } else {
    for (const unit of units) { const bounds = unit.bounds; translate(unit,
      mode === 'align-left' ? all.x - bounds.x : mode === 'align-center' ? all.x + (all.width - bounds.width) / 2 - bounds.x : 0,
      mode === 'align-top' ? all.y - bounds.y : mode === 'align-middle' ? all.y + (all.height - bounds.height) / 2 - bounds.y : 0); }
  }
  return placements;
}
/** Plan from a committed snapshot. No mutations occur until every proposed position is valid. */
export function planSelectionTransform(project, selection, {dx_mm = 0, dy_mm = 0, scale = 1, scale_x = scale, scale_y = scale, pivot_x_mm = 0, pivot_y_mm = 0} = {}) {
  const objects = selectedObjects(project, selection), scaling = scale_x !== 1 || scale_y !== 1;
  if (![dx_mm, dy_mm, pivot_x_mm, pivot_y_mm].every(value => finite(value, -20000, 20000)) || !finite(scale_x, .00001, 1000) || !finite(scale_y, .00001, 1000)) throw new Error('移动或缩放参数无效');
  if (scaling && objects.some(item => !item.asset_id)) throw new Error('整体缩放仅支持图片面板；请单独调整文字或图形标注');
  if (objects.length > 1 && Math.abs(scale_x - scale_y) > 1e-8) throw new Error('多面板和编组只支持等比缩放');
  const placements = objects.map(item => {
    const value = {id: item.id, x_mm: round(pivot_x_mm + (item.x_mm - pivot_x_mm) * scale_x + dx_mm), y_mm: round(pivot_y_mm + (item.y_mm - pivot_y_mm) * scale_y + dy_mm)};
    if (scaling) { value.width_mm = round(item.width_mm * scale_x); value.height_mm = round(item.height_mm * scale_y); }
    if (!finite(value.x_mm, -2000, 4000) || !finite(value.y_mm, -2000, 4000) || scaling && (!finite(value.width_mm, .1, 2000) || !finite(value.height_mm, .1, 2000))) throw new Error('移动或缩放后超出浏览器版坐标范围');
    return value;
  });
  return placements;
}
export function selectionGeometryChanged(before, after, selection) {
  const original = new Map(selectedObjects(before, selection).map(item => [item.id, item]));
  return selectedObjects(after, selection).some(item => !original.has(item.id) || ['x_mm', 'y_mm', 'width_mm', 'height_mm'].some(key => item[key] !== original.get(item.id)[key]));
}
export function figureIssues(project) {
  const issues = [], labels = new Map(), assets = new Map(project.assets.map(asset => [asset.id, asset]));
  for (const panel of project.panels) {
    const asset = assets.get(panel.asset_id), bounds = panelBounds(panel), ppi = asset.kind === 'svg' ? null : Math.min(panel.crop.width / panel.width_mm, panel.crop.height / panel.height_mm) * 25.4;
    if (bounds.x < -.01 || bounds.y < -.01 || bounds.x + bounds.width > project.figure.width_mm + .01 || bounds.y + bounds.height > project.figure.height_mm + .01) issues.push({code: 'out-of-canvas', panel_id: panel.id, severity: 'error', message: `面板 ${panel.label || asset.name} 或其标题超出画布`});
    if (ppi != null && ppi < (project.settings.min_ppi || 300)) issues.push({code: 'low-ppi', panel_id: panel.id, severity: 'warning', message: `${panel.label || asset.name} 的实际分辨率为 ${Math.round(ppi)} ppi；提高导出 DPI 不会增加原图细节`});
    if (asset.bit_depth > 8) issues.push({code: 'high-bit-display', panel_id: panel.id, severity: 'warning', message: `${asset.name} 原件为 ${asset.bit_depth} 位，项目保留原始字节；浏览器 PNG / PDF 组图导出为 8 位显示结果，原始测量请在本机版进行`});
    if (asset.metadata?.browser_svg_text) issues.push({code: 'svg-font-review', panel_id: panel.id, severity: 'warning', message: `${asset.name} 含源 SVG 文字；原 SVG 的字体需自行确认，建议先将其文字转轮廓`});
    if (asset.metadata?.browser_embedded_raster) issues.push({code: 'svg-raster-review', panel_id: panel.id, severity: 'warning', message: `${asset.name} 含内嵌位图，请在本机版进一步检查其实际分辨率`});
    if (panel.label) { const key = panel.label.trim().toUpperCase(), matches = labels.get(key) || []; matches.push(panel.id); labels.set(key, matches); }
  }
  for (const [label, ids] of labels) if (ids.length > 1) issues.push({code: 'duplicate-label', severity: 'error', message: `编号 ${label} 重复，请修改锁定编号或恢复自动编号`});
  for (const annotation of project.annotations.filter(item => !item.hidden)) { const bounds = annotationBounds(annotation); if (bounds.x < -.01 || bounds.y < -.01 || bounds.x + bounds.width > project.figure.width_mm + .01 || bounds.y + bounds.height > project.figure.height_mm + .01) issues.push({code: 'annotation-outside', severity: 'error', message: '有文字或标注超出画布，请检查'}); }
  return issues;
}
export function normalizeLabels(project) { ensureCompositionSettings(project); return reconcilePanelLabels(project); }
