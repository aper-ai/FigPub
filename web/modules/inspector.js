import {renderWbEntry} from './wb-workflow.js';
import {state, selectedAsset, selectedPanel, selectedPanels, selectedEdit} from './state.js';
import {escapeHtml as e, effectivePpi, fullCrop, scaleBarDraft, scaleBarGeometry, round} from './domain.js';
import {icon} from './icons.js';
import {renderChannelInspector} from './channel-view.js';
import {renderAnalysisInspector} from './analysis-forms.js';
import {renderCompositionControls,renderSelectionControls} from './composition.js';
import {layoutNumberAttributes} from './numeric-input.js';

const field = (label, key, value, {unit = '', step = '1', min = '', max = '', placeholder = '', type = 'number', scope = 'panel'} = {}) => {
  const layout = type === 'number' && ['panel','figure','annotation'].includes(scope) && (['mm','pt','°'].includes(unit) || key === 'rotation');
  return `<label class="field"><span>${label}</span><div class="input-with-unit"><input id="${scope}-${key}" data-field="${key}" data-scope="${scope}" type="${type}" ${layout ? layoutNumberAttributes(value) : `value="${e(value ?? '')}"`} step="${layout ? 'any' : step}" ${min !== '' ? `min="${min}"` : ''} ${max !== '' ? `max="${max}"` : ''} placeholder="${e(placeholder)}" ${type === 'text' ? '' : 'inputmode="decimal"'}>${unit ? `<small>${unit}</small>` : ''}</div></label>`;
};
const fold = (id,title,content) => `<details class="inspector-fold" id="${id}"><summary>${title}</summary>${content}</details>`;
const section = (title, content, subtitle = '', extra = '') => `<section class="inspector-section"><div class="inspector-heading"><h3>${title}</h3>${extra}</div>${subtitle ? `<p class="section-description">${subtitle}</p>` : ''}${content}</section>`;

export function renderPreflightSummary() {
  const preflight = state.preflight;
  return section('导出检查', `<button class="button full" data-action="preflight">${icon('check')}检查当前图稿</button>${preflight ? `<div class="preflight-overview ${preflight.issues?.some(issue => issue.severity === 'error') ? 'warning' : ''}">${icon(preflight.issues?.length ? 'info' : 'check')}<span>${preflight.issues?.length ? `${preflight.issues.length} 项需要关注` : '当前检查项通过'}</span></div><div class="preflight-issues">${(preflight.issues || []).map((issue, index) => `<button class="preflight-issue ${issue.severity}" data-action="locate-issue" data-index="${index}">${icon(issue.severity === 'error' || issue.severity === 'warning' ? 'alert' : 'info')}<span>${e(issue.message)}</span>${issue.panel_id ? icon('chevron') : ''}</button>`).join('')}</div>` : '<p class="microcopy">检查有效分辨率、字体、画布边界与源数据关联。</p>'}`);
}

function sourceInspector() {
  const asset = selectedAsset();
  if (!asset) return section('开始使用', `<div class="inspector-empty">${icon('image')}<h4>导入第一份素材</h4><p>支持原始 TIFF、矢量图稿和数据表。导入后可裁切、分析并组成完整 Figure。</p><button class="button full primary" data-action="import">${icon('upload')}导入素材</button></div>`);
  const metadata = `<dl class="property-list"><div><dt>文件类型</dt><dd>${e(asset.kind.toUpperCase())}</dd></div>${asset.width_px ? `<div><dt>像素尺寸</dt><dd>${asset.width_px} × ${asset.height_px}</dd></div>` : ''}${asset.bit_depth ? `<div><dt>位深</dt><dd>${asset.bit_depth}-bit</dd></div>` : ''}${asset.channels ? `<div><dt>通道</dt><dd>${asset.channels}</dd></div>` : ''}${asset.frames > 1 ? `<div><dt>图像页数</dt><dd>${asset.frames}</dd></div>` : ''}</dl><div class="source-hash"><span>原始文件指纹</span><code title="${e(asset.sha256 || '')}">${e((asset.sha256 || '—').slice(0, 20))}…</code></div><button class="button full" data-action="download-original">${icon('download')}下载原始文件</button>`;
  if (asset.kind === 'table') return section('数据分析', '<p class="section-description">在分析工作台中指定基因和对照组，保持原表不变。</p><button class="button full primary" data-action="analyze-table">进入 qPCR 分析</button>') + fold('source-file-details','源文件信息与原件下载',section('数据来源',metadata));
  return section('当前素材', `<h4 class="asset-inspector-name">${e(asset.name)}</h4><button class="button full primary" data-action="add-panel">${icon('plus')}加入组图</button>${asset.kind==='image'?'<button class="button full" data-action="enter-channels">逐通道显示与荧光</button>':''}`) + renderCropFields() + fold('source-file-details','源文件信息与原件下载',section('源文件信息', metadata)) + fold('source-display-settings','高级显示设置',section('显示设置', selectedEdit().channel_view?'<p class="microcopy">当前使用逐通道显示配方。</p><button class="button full" data-action="enter-channels">编辑通道显示</button>':renderDisplayFields(), '只改变显示效果；定量计算仍使用原始数值。'));
}

function renderCropFields() {
  const edit = selectedEdit(), crop = edit.crop || fullCrop(selectedAsset());
  return section('原图裁切', `<p class="microcopy">在原图框选区域，再点击「应用裁切」。原始文件保持完整。</p><div class="inline-actions"><button class="button small" data-action="source-tool" data-tool="roi">${icon('crop')}在图上框选</button><button class="button small ghost" data-action="reset-crop">恢复完整图</button></div><details class="compact-details" id="crop-exact-coordinates"><summary>精确设置裁切坐标</summary><div class="details-content field-grid">${field('X', 'x', crop.x, {unit: 'px', scope: 'crop', min: 0})}${field('Y', 'y', crop.y, {unit: 'px', scope: 'crop', min: 0})}${field('宽度', 'width', crop.width, {unit: 'px', scope: 'crop', min: 1})}${field('高度', 'height', crop.height, {unit: 'px', scope: 'crop', min: 1})}</div></details>`);
}

function renderDisplayFields() {
  const display = selectedEdit().display || {};
  return `<div class="field-grid">${field('黑点', 'low', display.low, {scope: 'display', placeholder: '自动', step: 'any'})}${field('白点', 'high', display.high, {scope: 'display', placeholder: '自动', step: 'any'})}</div><label class="checkbox-label"><input type="checkbox" data-field="invert" data-scope="display" ${display.invert ? 'checked' : ''}>反转明暗</label><p class="microcopy">黑白点使用原始强度数值。仅作全图线性调整。</p><button class="button small ghost" data-action="reset-display">重置显示</button>`;
}

function gelInspector() {
  const asset = selectedAsset(); if (!asset || asset.kind !== 'image') return sourceInspector();
  const gel = selectedEdit().gel || {}, centers = gel.lane_centers || [], names = gel.lane_names || [], markers = gel.marker_points || [];
  const tools = section('凝胶与 WB 标注', `<div class="segmented full"><button data-action="gel-mode" data-mode="protein" class="${state.gelMode !== 'dna' ? 'selected' : ''}">蛋白胶 / WB</button><button data-action="gel-mode" data-mode="dna" class="${state.gelMode === 'dna' ? 'selected' : ''}">DNA 胶</button></div><button class="button full primary" data-action="detect-gel" ${state.gelBusy ? 'disabled' : ''}>${state.gelBusy ? '<span class="spinner"></span>' : icon('magic')}${state.gelBusy ? '正在分析泳道…' : '识别泳道与条带'}</button>${state.gelBusy ? '<button class="button full ghost small" data-action="cancel-gel">取消识别</button>' : ''}<p class="microcopy">条带识别结果需对照原图复核。Marker 校准使用已确认的标准品参数。</p>`);
  const laneSection = section(`泳道 <span class="count-badge">${centers.length}</span>`, `<div class="lane-count-row"><label class="field"><span>预期泳道数</span><input id="lane-count" type="number" min="1" max="100" data-gel-lane-count value="${state.gelLaneCount || centers.length || ''}" placeholder="自动"></label><button class="button small" data-action="even-lanes">均匀分布</button></div><div class="lane-list">${centers.map((center, index) => `<div class="lane-row"><span>${index + 1}</span><input id="lane-${index}-name" data-lane-field="name" data-index="${index}" value="${e(names[index] || '')}" placeholder="样本名称" aria-label="泳道 ${index + 1} 名称"><input id="lane-${index}-x" type="number" data-lane-field="x" data-index="${index}" value="${Math.round(center)}" min="0" max="${asset.width_px}" aria-label="泳道 ${index + 1} X 坐标"><button class="icon-button small" data-action="delete-lane" data-index="${index}" title="移除检测线，不删除图像">${icon('close')}</button></div>`).join('')}</div><button class="button small full" data-action="add-lane">${icon('plus')}添加泳道线</button>`, centers.length ? '右侧数字为原图 X 坐标（px）。' : '自动识别或设置数量后手动校正。');
  const markerSection = section('Marker 校准', `<label class="field"><span>标尺单位</span><select id="marker-unit" data-field="marker_unit" data-scope="gel"><option value="kDa" ${gel.marker_unit !== 'bp' ? 'selected' : ''}>kDa</option><option value="bp" ${gel.marker_unit === 'bp' ? 'selected' : ''}>bp</option></select></label><div class="marker-head"><span>原图 Y / px</span><span>已知大小</span></div><div class="marker-list">${markers.map((point,index) => `<div class="marker-row"><input id="marker-${index}-y" type="number" data-marker-field="y" data-index="${index}" value="${Math.round(point.y)}" aria-label="Marker Y坐标"><input id="marker-${index}-size" type="number" data-marker-field="size" data-index="${index}" value="${e(point.size)}" min="0.001" step="any" aria-label="Marker 已知大小"><button class="icon-button small" data-action="delete-marker" data-index="${index}" title="删除校准点">${icon('close')}</button></div>`).join('')}</div><button class="button small full ${state.sourceTool === 'marker' ? 'active' : ''}" data-action="source-tool" data-tool="marker">${icon('point')}在原图添加校准点</button><p class="microcopy">输入 Marker 的已知大小；至少需要两个有效校准点。外推结果需另行核查。</p>`);
  const suggestions = state.gelSuggestions?.marker_candidates || [];
  const candidateSection = suggestions.length ? section('Marker 候选', `<div class="candidate-list">${suggestions.map((candidate,index)=>`<button data-action="marker-candidate" data-index="${index}"><span><strong>${e(candidate.ladder_name)}</strong><small>泳道 ${candidate.lane_index+1} · ${candidate.points.length} 个匹配条带</small></span>${icon('chevron')}</button>`).join('')}</div><p class="microcopy">候选仅供核对。确认型号与条带后才写入校准。</p>`) : '';
  const estimate = state.markerEstimate;
  const estimateSection = markers.length >= 2 ? section('条带大小估计', `<button class="button full small ${state.sourceTool === 'estimate' ? 'active' : ''}" data-action="source-tool" data-tool="estimate">${icon('point')}点击原图估计大小</button>${estimate ? `<div class="marker-estimate"><strong>${estimate.extrapolated ? '≈ ' : ''}${Number(estimate.size.toPrecision(4))} ${e(estimate.unit)}</strong><span>${estimate.extrapolated ? '超出校准范围，属于外推估计' : '已知 Marker 点之间的插值'} · Y ${Math.round(estimate.y)} px</span></div>` : ''}`) : '';
  return section('自动定量与出图',renderWbEntry()) + tools + renderCropFields() + laneSection + fold('gel-marker-tools','Marker 校准与分子量估计（可选）',markerSection + candidateSection + estimateSection) + fold('gel-display-settings','显示与组图',section('显示与输出', (selectedEdit().channel_view?'<button class="button full" data-action="enter-channels">编辑原始通道显示</button>':renderDisplayFields()) + '<button class="button full primary" data-action="add-panel">加入组图</button>'));
}

function renderScaleBarFields(panel,asset) {
  const bar=scaleBarDraft(panel),geometry=scaleBarGeometry(panel,asset,bar),enabled=Boolean(panel.scale_bar);
  return section('比例尺',`${field('原始像素尺度 · μm / px','um_per_px',bar.um_per_px,{scope:'scale-bar',step:'any',min:0,placeholder:'输入实测或仪器标定值'})}<div class="field-grid">${field('标示长度','length_um',bar.length_um,{scope:'scale-bar',unit:'μm',step:'any',min:0,placeholder:'如 50'})}<label class="field"><span>颜色</span><input type="color" id="scale-bar-color" data-field="color" data-scope="scale-bar" value="${e(bar.color)}"></label></div><label class="checkbox-label"><input type="checkbox" id="scale-bar-visible" data-scale-bar-visible ${enabled?'checked':''} ${!enabled&&!geometry.valid?'disabled':''}>显示比例尺</label><p class="microcopy scale-bar-status ${geometry.valid?'':'incomplete'}">${geometry.valid?`图中长度 ${round(geometry.length_mm,3)} mm · 对应 ${round(geometry.source_length_px,3)} 个原始像素`:e(geometry.message)}</p><p class="microcopy">裁切与版面缩放后自动换算。未提供有效标定时不显示；关闭后保留输入值。</p>`);
}

function composeInspector() {
  const project=state.project, panel=selectedPanel(), count=selectedPanels().length;
  const annotation=project.annotations.find(item=>item.id===state.selectedAnnotationId), asset=selectedAsset();
  let content=section('画布', `<div class="field-grid">${field('宽度','width_mm',project.figure.width_mm,{unit:'mm',scope:'figure',min:20,max:2000,step:.1})}${field('高度','height_mm',project.figure.height_mm,{unit:'mm',scope:'figure',min:20,max:2000,step:.1})}</div><p class="microcopy">使用下方期刊预设，或输入准确的成图尺寸。</p>`);
  const inspectorTab=state.compositionInspectorTab || (panel||annotation?'selection':'layout');
  content+=`<div class="composition-inspector-tabs" role="tablist" aria-label="组图设置"><button role="tab" aria-selected="${inspectorTab==='layout'}" data-action="composition-inspector" data-view="layout">版式与期刊</button><button role="tab" aria-selected="${inspectorTab==='selection'}" data-action="composition-inspector" data-view="selection">对象属性${count?` · ${count}`:''}</button></div>`;
  if(inspectorTab==='layout')return content+renderCompositionControls(project)+renderPreflightSummary();
  content+=renderSelectionControls();
  if(panel) {
    const ppi=effectivePpi(panel,asset), acceptable=ppi==null || ppi>=project.settings.min_ppi;
    content+=section(count>1?'主选面板属性':'选中面板', `
      <div class="selected-panel-head"><span class="panel-letter">${e(panel.label || '·')}</span><span>${e(asset?.name || '素材缺失')}</span><button class="icon-button small danger-hover" data-action="delete-selection" title="删除选中面板">${icon('trash')}</button></div>
      <label class="field"><span>编号方式</span><select id="panel-label-mode" data-label-mode>${[['auto','自动编号'],['manual','手工编号（保留）'],['none','不显示编号']].map(([value,title])=>`<option value="${value}" ${panel.label_mode===value?'selected':''}>${title}</option>`).join('')}</select></label>
      <div class="field-grid">${field('编号','label',panel.label,{type:'text'})}${field('标题','title',panel.title,{type:'text'})}${field('X','x_mm',panel.x_mm,{unit:'mm',step:.1})}${field('Y','y_mm',panel.y_mm,{unit:'mm',step:.1})}${field('宽度','width_mm',panel.width_mm,{unit:'mm',step:.1,min:1})}${field('高度','height_mm',panel.height_mm,{unit:'mm',step:.1,min:1})}${field('编号字号','label_font_size_pt',panel.label_font_size_pt || 10,{unit:'pt',step:.5,min:1})}${field('标题字号','title_font_size_pt',panel.title_font_size_pt || 8,{unit:'pt',step:.5,min:1})}${field('旋转','rotation',panel.rotation || 0,{unit:'°',step:.1})}</div>
      <label class="checkbox-label"><input type="checkbox" data-lock-aspect ${state.lockAspect!==false?'checked':''}>拖动角点时锁定宽高比</label>
      <div class="ppi-indicator ${acceptable?'':'low'}">${icon(acceptable?'check':'alert')}<div><strong>${ppi===null?'矢量 / 混合素材':`${Math.round(ppi)} ppi`}</strong><span>${ppi===null?'内嵌位图在导出检查中核验':'按原图裁切与当前尺寸计算'}</span></div></div>
      ${count===1?`<div class="align-toolbar">${[['left','左对齐'],['center','水平居中'],['right','右对齐'],['top','顶部对齐'],['bottom','底部对齐']].map(([value,title])=>`<button class="icon-button" data-action="align-panel" data-align="${value}" title="${title}">${icon(value)}</button>`).join('')}</div>`:''}
      <div class="inline-actions"><button class="button small ${panel.flip_h?'active':''}" data-action="flip-panel" data-direction="h">水平翻转</button><button class="button small ${panel.flip_v?'active':''}" data-action="flip-panel" data-direction="v">垂直翻转</button></div>
      <div class="inline-actions"><button class="button small" data-action="edit-panel-source">${icon('crop')}编辑源区域</button><button class="button small" data-action="duplicate-panel">${icon('duplicate')}复制选中</button></div>
      ${count>1?'<p class="microcopy">上方属性只编辑主选面板；拖动和方向键移动全部选中面板。</p>':''}
    `);
    if(asset?.kind==='image') content+=`${section('原始通道','<button class="button full" data-action="enter-channels">编辑面板通道显示</button>')}${renderScaleBarFields(panel,asset)}${section('凝胶文字',field('泳道字号','font_size_pt',panel.gel?.font_size_pt || 7,{scope:'gel',unit:'pt',step:.5,min:1}))}`;
  } else if(annotation) {
    content+=section('选中标注',`${annotation.type==='text'?field('文字','text',annotation.text,{scope:'annotation',type:'text'}):''}<div class="field-grid">${field('X','x_mm',annotation.x_mm,{scope:'annotation',unit:'mm',step:.1})}${field('Y','y_mm',annotation.y_mm,{scope:'annotation',unit:'mm',step:.1})}${field(annotation.type==='line'?'终点 ΔX':'宽度','width_mm',annotation.width_mm,{scope:'annotation',unit:'mm',step:.1,min:annotation.type==='line'?'':.1})}${field(annotation.type==='line'?'终点 ΔY':'高度','height_mm',annotation.height_mm,{scope:'annotation',unit:'mm',step:.1,min:annotation.type==='line'?'':.1})}${annotation.type==='text'?field('字号','font_size_pt',annotation.font_size_pt || 8,{scope:'annotation',unit:'pt',step:.5,min:1}):''}<label class="field"><span>颜色</span><input type="color" id="annotation-color" data-field="color" data-scope="annotation" value="${e(annotation.color || '#222222')}"></label></div><button class="button small full danger-hover" data-action="delete-selection">${icon('trash')}删除标注</button>`);
  }
  if(!panel&&!annotation)content+=section('对象属性','<p class="microcopy">点击面板或标注查看属性；Shift 点选或空白处框选可选择多个面板。</p>');
  return content+renderPreflightSummary();
}

export function renderInspector() {
  if (!state.project) return '';
  const titles = {source:'素材属性', channels:'通道与荧光', gel:'标注与校准', analysis:'分析设置', compose:'画布与属性'};
  return `<aside class="inspector"><div class="sidebar-heading"><h2>${titles[state.tab]}</h2>${icon('settings')}</div><div class="inspector-scroll">${state.tab === 'gel' ? gelInspector() : state.tab === 'channels' ? renderChannelInspector() : state.tab === 'analysis' ? renderAnalysisInspector() : state.tab === 'compose' ? composeInspector() : sourceInspector()}</div><div class="inspector-footer">${icon('lock')}本地处理 · 原件不被覆盖</div></aside>`;
}
