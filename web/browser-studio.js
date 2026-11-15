import {applyPanelLayout as applySharedLayout, resizeFigure, layoutOptions, ensureCompositionSettings, reconcilePanelLabels} from './modules/layout-engine.js';
import {JOURNAL_PRESETS, journalPresetById, journalPreflight} from './modules/journal-presets.js';
import {LIMITS, encodeProject, decodeProject} from './browser/archive.js';
import {newProject, panelForAsset, validateBrowserProject, uid, clone, recipeSnapshot, escapeXml as e, selectedObjects, expandedSelection, selectionUnits, selectionBounds, planUnitAlignment, planSelectionTransform, selectionGeometryChanged, assertUngroupedLayout, captureCommittedWorkspace, figureIssues} from './browser/model.js';
import {createSource, releaseSources, sourceBudget} from './browser/sources.js';
import {figureMarkup, exportSvg, exportPng, exportRasterPdf, prepareFonts, fontIssues} from './browser/render.js';
import {getWorkspaceStatus, loadWorkspace, saveWorkspace, clearWorkspace} from './browser/persistence.js';
import {numberField,readNumericInput,markLayoutNumberEdited} from './browser/forms.js';
import {hasWorkspaceContent} from './browser/model.js';
import {formatLayoutNumber} from './modules/numeric-input.js';
import {snapTranslation,svgPixelsPerMm,snapGuideMarkup} from './modules/snap-guides.js';

const $ = selector => document.querySelector(selector);
if (!globalThis.crypto?.subtle || !globalThis.crypto?.randomUUID || typeof structuredClone !== 'function') {
  $('#studio-workspace').innerHTML = '<section style="padding:36px;grid-column:1/-1"><h1>当前打开方式不支持此工作台</h1><p style="margin-top:16px">请使用新版 Chrome 或 Edge，直接打开完整 HTML 文件，或通过 HTTPS 网站访问。</p><p style="margin-top:8px">图片尚未读取；无需安装 Python、Node 或本机服务器。</p></section>';
  $('#save-status').textContent = '浏览器能力不足，工作区尚未启动'; for (const control of document.querySelectorAll('button,input,select')) control.disabled = true;
  throw new Error('浏览器组图需要安全上下文中的 WebCrypto 和 structuredClone。');
}
let project = newProject(), sources = new Map(), selection = new Set(), history = [], future = [], busy = false, autosaveTimer, messageTimer, zoom = 1, fitted = true, gesture = null, gestureFrame = 0, pendingPointer = null, saveSequence = 0, revisionClock = 0, persistEnabled = true, lastSavedRevision = -1, lastBackupRevision = -1, recoveryPending = false;
const AUTO_APPLY_DELAY = 350;
let parameterTimer, parameterSession = null, pendingParameterForm = null, composingParameter = false;
const parameterDrafts = new Set(), objectUi = new Map();
const sameRecipe = (left, right) => JSON.stringify([left.title, left.figure, left.panels, left.annotations, left.settings]) === JSON.stringify([right.title, right.figure, right.panels, right.annotations, right.settings]);
const selected = () => { selection = expandedSelection(project, selection); return selectedObjects(project, selection); };
const assetOf = panel => project.assets.find(asset => asset.id === panel.asset_id);
const snapshot = () => recipeSnapshot(project);
function applyPanelLayout(value, overrides) { assertUngroupedLayout(value); return applySharedLayout(value, overrides); }
function applyPlacements(placements) { const objects = new Map([...project.panels, ...project.annotations].map(item => [item.id, item])); for (const placement of placements) Object.assign(objects.get(placement.id), placement); }
function message(text, tone = '') { const dialogError=$('#workspace-recovery-error');if(dialogError&&$('#workspace-recovery-dialog').open){dialogError.hidden=tone!=='error';dialogError.textContent=tone==='error'?text:'';}clearTimeout(messageTimer); $('#messages').innerHTML = `<div class="${tone === 'error' ? 'error' : ''}">${e(text)}</div>`; messageTimer = setTimeout(() => { $('#messages').innerHTML = ''; }, tone === 'error' ? 11000 : 5000); }
function remember(before) { history.push(before); if (history.length > 50) history.shift(); future = []; }
function assertWorkspaceReady() { if(recoveryPending)throw new Error('请先恢复上次工作区，或明确新建、清除本地数据。'); }
function markChanged() {
  assertWorkspaceReady();
  revisionClock = Math.max(revisionClock, project.revision) + 1; project.revision = revisionClock; project.updated_at = new Date().toISOString(); selection = expandedSelection(project, selection); render();
  const current = project; prepareFonts(current).then(() => { if (project === current && !gesture) renderCanvas(); }).catch(() => {});
  // Capture the last committed recipe now: pointer drags mutate live geometry between commits.
  const {project: committed, sources: committedSources} = captureCommittedWorkspace(project, sources), sequence = ++saveSequence; clearTimeout(autosaveTimer); $('#save-status').textContent = persistEnabled ? '等待本地保存…' : '自动保存不可用，请下载项目';
  if (persistEnabled) autosaveTimer = setTimeout(() => {
    $('#save-status').textContent = '正在本地保存…';
    saveWorkspace(committed, committedSources).then(() => { if (sequence === saveSequence) { lastSavedRevision = committed.revision; $('#save-status').textContent = '已自动保存到此浏览器'; $('#save-status').className = ''; } }).catch(error => { if(sequence!==saveSequence)return; persistEnabled = false; $('#save-status').textContent = '自动保存失败，请下载项目'; $('#save-status').className = 'error'; message(`本地保存不可用：${error.message}。请点击“保存项目”备份。`, 'error'); });
  }, 650);
}
function mutate(operation, {session = null, form = null} = {}) {
  if (busy) return false; if(recoveryPending){$('#workspace-recovery-dialog').showModal();return false;} const before = snapshot();
  try {
    operation(project); ensureCompositionSettings(project); reconcilePanelLabels(project); validateBrowserProject(project);
    if (sameRecipe(before, project)) return true;
    if (!session || !session.historyEntry || history.at(-1) !== session.historyEntry) {
      remember(before); if (session) session.historyEntry = before;
    }
    markChanged(); return true;
  } catch (error) {
    project = before; render();
    if (form) parameterFeedback(form, error.message); else message(error.message, 'error');
    return false;
  }
}
async function withBusy(label, operation) {
  if (busy) return; busy = true; const dialogError=$('#workspace-recovery-error');if(dialogError){dialogError.hidden=true;dialogError.textContent='';}document.body.classList.add('busy'); $('#busy-status').textContent = label; const exportButton = $('#export-confirm'); exportButton.disabled = true;
  try { return await operation(); } catch (error) { message(error.message || String(error), 'error'); }
  finally { busy = false; document.body.classList.remove('busy'); $('#busy-status').textContent = ''; exportButton.disabled = false; renderButtons(); }
}
function selectObject(id, toggle = false) {
  finishParameterEdit({discardInvalid: true});
  const object = [...project.panels, ...project.annotations].find(item => item.id === id); if (!object) return;
  const ids = object.group_id ? [...project.panels, ...project.annotations].filter(item => item.group_id === object.group_id).map(item => item.id) : [id];
  if (!toggle) selection = new Set(ids); else { const remove = ids.every(value => selection.has(value)); for (const value of ids) if (remove) selection.delete(value); else selection.add(value); }
  render();
}
function renderButtons() { $('[data-action="undo"]').disabled = busy || !history.length; $('[data-action="redo"]').disabled = busy || !future.length; $('[data-action="export"]').disabled = busy || !project.panels.length && !project.annotations.length; }
function calculateFit() {
  const viewport = $('#canvas-viewport'); zoom = Math.max(.08, Math.min(2.5, (viewport.clientWidth - 72) / (project.figure.width_mm * 96 / 25.4), (viewport.clientHeight - 72) / (project.figure.height_mm * 96 / 25.4)));
}
function renderCanvas() {
  if (fitted) calculateFit(); const wrapper = $('#canvas-wrap'); wrapper.innerHTML = figureMarkup(project, sources, {editor: true, selection});
  const svg = $('#studio-svg'); svg.style.width = `${project.figure.width_mm * 96 / 25.4 * zoom}px`; svg.style.height = `${project.figure.height_mm * 96 / 25.4 * zoom}px`;
  const guides=document.createElementNS('http://www.w3.org/2000/svg','g');guides.id='snap-guides';guides.setAttribute('pointer-events','none');
  guides.innerHTML=snapGuideMarkup(gesture?.guides || [],project.figure);svg.append(guides);
  $('#empty-state').hidden = !!(project.panels.length || project.annotations.length); $('#zoom').value = String(Math.round(zoom * 100)); $('#zoom-value').textContent = `${Math.round(zoom * 100)}%`;
}
function renderAssets() {
  $('#panel-count').textContent = project.panels.length;
  const rows = Array.from($('#asset-list').children);
  if (rows.length && rows.length === project.panels.length && rows.every((row, index) => row.dataset.select === project.panels[index].id)) {
    for (const [index, row] of rows.entries()) {
      const panel = project.panels[index], asset = assetOf(panel);
      row.className = `asset-row ${selection.has(panel.id) ? 'selected' : ''}`;
      row.setAttribute('aria-label', `选择面板 ${panel.label} ${asset.name}`);
      row.querySelector('.asset-label').textContent = `${panel.label || '无编号'}${panel.label_mode === 'manual' ? ' · 锁定' : ''}${panel.group_id ? ' · 编组' : ''}`;
    }
    return;
  }
  $('#asset-list').innerHTML = project.panels.length ? project.panels.map(panel => { const asset = assetOf(panel); return `<div class="asset-row ${selection.has(panel.id) ? 'selected' : ''}" data-select="${e(panel.id)}" tabindex="0" role="button" aria-label="选择面板 ${e(panel.label)} ${e(asset.name)}"><img src="${e(sources.get(asset.id)?.url || '')}" alt=""><div><span class="asset-label">${e(panel.label || '无编号')}${panel.label_mode === 'manual' ? ' · 锁定' : ''}${panel.group_id ? ' · 编组' : ''}</span><strong title="${e(asset.name)}">${e(asset.name)}</strong><small>${asset.width_px} × ${asset.height_px} px</small></div></div>`; }).join('') : '<div class="asset-list-empty">原件导入后显示在这里</div>';
}
function renderJournalNote() {
  const preset = journalPresetById($('#journal-preset').value);
  $('#journal-note').innerHTML = preset ? `${e(preset.notes?.[0] || '')}<details><summary>规格说明与来源</summary>${(preset.notes || []).slice(1).map(note => `<p>${e(note)}</p>`).join('')}${preset.source_url ? ` <a href="${e(preset.source_snapshot_url || preset.source_url)}" target="_blank" rel="noopener noreferrer">查看依据</a>` : ''}</details>` : '按毫米设置最终图稿尺寸。';
}
function syncParameterValue(control, value) {
  if (parameterDrafts.has(control)) return;
  if (control.type === 'checkbox') control.checked = !!value;
  else if (control.value !== String(value)) control.value = String(value);
}
function renderObjectProperties(content, object) {
  const root = $('#selection-properties'), context = object ? `${project.id}:${object.id}` : '';
  const current = root.querySelector('#object-form');
  if (!current || !context || root.dataset.context !== context) {
    root.innerHTML = content; root.dataset.context = context;
    const form = root.querySelector('#object-form');
    if (form) {
      form.dataset.objectId = object.id; form.dataset.projectId = project.id;
      const ui = objectUi.get(context);
      if (ui && form.elements.lock_aspect) form.elements.lock_aspect.checked = ui.lockAspect;
      if (ui && form.querySelector('details')) form.querySelector('details').open = ui.cropOpen;
    }
    return;
  }
  // Keep the native controls attached: replacing an active number input loses
  // its selection, and replacing a textarea interrupts Chinese composition.
  const template = document.createElement('template'); template.innerHTML = content;
  const incoming = template.content.querySelector('#object-form');
  for (const control of current.elements) {
    const next = incoming.elements.namedItem(control.name);
    if (!next || control.name === 'lock_aspect' || parameterDrafts.has(control)) continue;
    syncParameterValue(control, next.type === 'checkbox' ? next.checked : next.value);
    for (const key of ['layoutValue', 'layoutDisplay', 'layoutEdited']) {
      if (next.dataset[key] === undefined) delete control.dataset[key]; else control.dataset[key] = next.dataset[key];
    }
    control.removeAttribute('aria-invalid');
  }
  const title = root.querySelector('.property-title span'), nextTitle = template.content.querySelector('.property-title span');
  if (title && nextTitle) title.textContent = nextTitle.textContent;
  if (current.nextElementSibling?.matches('p.note') && incoming.nextElementSibling?.matches('p.note')) {
    current.nextElementSibling.textContent = incoming.nextElementSibling.textContent;
    current.nextElementSibling.className = incoming.nextElementSibling.className;
  }
}
function renderProperties() {
  $('#project-title').value = project.title; syncParameterValue($('#canvas-width'), project.figure.width_mm); syncParameterValue($('#canvas-height'), project.figure.height_mm);
  syncParameterValue($('#resize-mode'), project.settings.composition.resize_mode || 'fit');
  $('#journal-preset').value = project.settings.composition.journal_preset_id || ''; renderJournalNote();
  const layout = layoutOptions(project); for (const [key, value] of Object.entries(layout)) syncParameterValue($(`#layout-form [name="${key}"]`), value); $('#number-style').value = project.settings.composition.numbering.style;
  $('#number-enabled').checked = project.settings.composition.numbering.enabled; $('#number-order').value = project.settings.composition.numbering.order;
  const objects = selected(), unitCount = selectionUnits(project, selection).length, panel = objects.length === 1 && objects[0].asset_id ? objects[0] : null, annotation = objects.length === 1 && !objects[0].asset_id ? objects[0] : null;
  $('#selection-info').textContent = objects.length ? `已选择 ${objects.length} 个对象` : '未选中对象'; $('#figure-size').textContent = `${formatLayoutNumber(project.figure.width_mm)} × ${formatLayoutNumber(project.figure.height_mm)} mm`; $('#demo-badge').hidden = !project.settings.demo;
  let content = '';
  if (panel) {
    const asset = assetOf(panel), ppi = asset.kind === 'svg' ? null : Math.min(panel.crop.width / panel.width_mm, panel.crop.height / panel.height_mm) * 25.4;
    content = `<section class="property-section"><div class="property-title"><h3>选中面板</h3><span>${e(panel.label || '·')}</span></div><form id="object-form"><div class="field-grid"><label class="field">编号<textarea name="label" rows="1" maxlength="240">${e(panel.label)}</textarea></label><label class="field">编号模式<select name="label_mode"><option value="auto" ${panel.label_mode === 'auto' ? 'selected' : ''}>自动</option><option value="manual" ${panel.label_mode === 'manual' ? 'selected' : ''}>手工锁定</option><option value="none" ${panel.label_mode === 'none' ? 'selected' : ''}>不显示</option></select></label></div><label class="field">标题<textarea name="title" rows="2" maxlength="240">${e(panel.title)}</textarea></label><div class="field-grid">${numberField('X / mm', 'x_mm', panel.x_mm)}${numberField('Y / mm', 'y_mm', panel.y_mm)}${numberField('宽 / mm', 'width_mm', panel.width_mm, 'min=".1" max="2000"')}${numberField('高 / mm', 'height_mm', panel.height_mm, 'min=".1" max="2000"')}${numberField('旋转 / °', 'rotation', panel.rotation || 0)}${numberField('编号 / pt', 'label_font_size_pt', panel.label_font_size_pt || 10, 'min="1" max="100"')}${numberField('标题 / pt', 'title_font_size_pt', panel.title_font_size_pt || 8, 'min="1" max="100"')}</div><label class="compact-check"><input type="checkbox" name="lock_aspect" checked>锁定宽高比</label><details><summary class="note">原图裁切 / px</summary><div class="field-grid">${numberField('X', 'crop_x', panel.crop.x, 'min="0"')}${numberField('Y', 'crop_y', panel.crop.y, 'min="0"')}${numberField('宽', 'crop_width', panel.crop.width, 'min="1"')}${numberField('高', 'crop_height', panel.crop.height, 'min="1"')}</div></details><p class="parameter-feedback error" role="status" aria-live="polite" hidden></p></form><p class="note ${ppi != null && ppi < 300 ? 'warning' : ''}">${ppi == null ? '来源为 SVG；源文件内嵌位图仍需核验。' : `原图实际分辨率 ${Math.round(ppi)} ppi`}</p><div class="selection-actions"><button data-action="flip-h">水平翻转</button><button data-action="flip-v">垂直翻转</button><button data-action="auto-label">恢复自动编号</button></div></section>`;
  } else if (annotation) {
    content = `<section class="property-section"><h3>选中标注</h3><form id="object-form">${annotation.type === 'text' ? `<label class="field">文字<textarea name="text" rows="3" maxlength="2000">${e(annotation.text)}</textarea></label>` : ''}<div class="field-grid">${numberField('X / mm', 'x_mm', annotation.x_mm)}${numberField('Y / mm', 'y_mm', annotation.y_mm)}${numberField('宽 / mm', 'width_mm', annotation.width_mm)}${numberField('高 / mm', 'height_mm', annotation.height_mm)}${numberField('字号 / pt', 'font_size_pt', annotation.font_size_pt || 8, 'min="1" max="100"')}<label class="field">颜色<input type="color" name="color" value="${e(annotation.color)}"></label></div><p class="parameter-feedback error" role="status" aria-live="polite" hidden></p></form></section>`;
  } else if (objects.length) content = `<section class="property-section"><h3>已选择 ${objects.length} 个对象</h3><p class="muted">拖动任一选中对象可一起移动。图片多选时可拖右下角整体等比缩放。编组仅支持图片；对齐与分布按整个编组计算。</p><button class="wide" data-action="auto-label">所选面板恢复自动编号</button></section>`;
  renderObjectProperties(content, panel || annotation);
  const toolbar = $('#selection-toolbar'), toolbarContext = `${project.id}:${objects.map(item => item.id).join(',')}:${unitCount}`;
  if (toolbar.dataset.context !== toolbarContext) {
    toolbar.dataset.context = toolbarContext;
    toolbar.innerHTML = objects.length ? [['duplicate', '复制'], ['group', '编组'], ['ungroup', '解组'], ['align-left', '左齐'], ['align-top', '顶齐'], ['align-center', '水平居中'], ['align-middle', '垂直居中'], ['distribute-x', '水平等距'], ['distribute-y', '垂直等距'], ['front', '置顶'], ['back', '置底'], ['delete', '删除']].map(([action, title]) => `<button data-action="${action}" ${(action === 'group' && objects.length < 2 || action.startsWith('align-') && unitCount < 2 || action.startsWith('distribute') && unitCount < 3) ? 'disabled' : ''}>${title}</button>`).join('') : '<span class="muted">点击图片选择，拖动调整位置；拖入文件自动加入图稿</span>';
  }
  const issues = allIssues(); $('#preflight-summary').textContent = !project.panels.length ? '导入后检查实际分辨率、编号和画布边界。' : issues.length ? `${issues.filter(item => item.severity === 'error').length} 项错误，${issues.filter(item => item.severity !== 'error').length} 项提示。点击“检查图稿”查看。` : '当前检查项通过；导出前仍请人工检查内容。';
}
function render() { selection = expandedSelection(project, selection); renderAssets(); renderCanvas(); renderProperties(); renderButtons(); }
function allIssues() { return [...figureIssues(project), ...fontIssues(project), ...journalPreflight(project)]; }
function issuesHtml(issues) { return issues.length ? `<div class="issue-list">${issues.map(issue => `<div class="issue ${issue.severity === 'error' ? 'error' : ''}">${e(issue.message)}</div>`).join('')}</div>` : '<div class="issue ok">当前检查项通过。</div>'; }
function download(blob, filename) { const url = URL.createObjectURL(blob), anchor = document.createElement('a'); anchor.href = url; anchor.download = filename; document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000); }
const filename = extension => `${Array.from(project.title).filter(character => character.charCodeAt(0) >= 32).join('').replace(/[\\/:*?"<>|]/g, '_').slice(0, 100) || 'figure'}.${extension}`;
async function importImages(files) {
  if (!files.length) return;
  if (!finishParameterEdit()) return;
  await withBusy(`正在导入 ${files.length} 份原件…`, async () => {
    assertWorkspaceReady();assertUngroupedLayout(project);
    if (project.panels.length + files.length > LIMITS.panels) throw new Error(`浏览器版最多 ${LIMITS.panels} 个面板`);
    const staged = [], proposedSources = new Map(sources), candidate = snapshot();
    try {
      for (const file of files) { let source; try { source = await createSource(new Uint8Array(await file.arrayBuffer()), file.name); } catch (error) { throw new Error(`${file.name}：${error.message}`); } staged.push(source); if (!proposedSources.has(source.asset.id)) proposedSources.set(source.asset.id, source); if (!candidate.assets.some(asset => asset.id === source.asset.id)) candidate.assets.push(source.asset); candidate.panels.push(panelForAsset(source.asset)); }
      if (candidate.assets.length > LIMITS.assets || sourceBudget(proposedSources) > LIMITS.totalBytes) throw new Error('浏览器版最多 256 份原件，累计 192 MiB。');
      const options = layoutOptions(candidate);
      applyPanelLayout(candidate, options); validateBrowserProject(candidate); remember(snapshot()); project = candidate; sources = proposedSources; selection = new Set(candidate.panels.slice(-files.length).map(panel => panel.id));
      for (const source of staged) if (sources.get(source.asset.id) !== source) URL.revokeObjectURL(source.url);
      markChanged(); message(`已加入 ${files.length} 个面板，完成等比排版与编号。`);
    } catch (error) { for (const source of staged) URL.revokeObjectURL(source.url); throw error; }
  });
}
async function installProject(incoming) {
  const staged = new Map();
  try {
    for (const original of incoming.originals) { const source = await createSource(original.bytes, original.name, original.asset.id); if (source.asset.width_px !== original.asset.width_px || source.asset.height_px !== original.asset.height_px || source.asset.kind !== original.asset.kind) throw new Error('项目记录尺寸与原件解码尺寸不一致，请在本机版核对'); staged.set(source.asset.id, source); }
    const candidate = validateBrowserProject(incoming.project); for (const asset of candidate.assets) { asset.preview_url = staged.get(asset.id).url; asset.metadata = {...asset.metadata, ...staged.get(asset.id).asset.metadata}; }
    releaseSources(sources); project = candidate; revisionClock = candidate.revision; sources = staged; selection = new Set(); history = []; future = []; lastSavedRevision = -1; lastBackupRevision = -1; fitted = true; return true;
  } catch (error) { releaseSources(staged); throw error; }
}
async function openFile(file) {
  if (!file) return; cancelParameterEdit(); await withBusy('校验项目与全部原件…', async () => {
    assertWorkspaceReady();
    if (hasWorkspaceContent(project) && !confirm('打开项目会替换当前工作区。请先用“保存项目”保留当前图稿。继续打开？')) return;
    if (file.size > LIMITS.totalBytes) throw new Error('项目超过浏览器版 192 MiB 上限');
    const incoming = await decodeProject(new Uint8Array(await file.arrayBuffer())); await installProject(incoming); markChanged(); message('项目及原件哈希已验证，图稿已恢复。');
  });
}
async function saveProject() { if (!finishParameterEdit()) return; await withBusy('正在校验并保存完整项目…', async () => { const blob = await encodeProject(project, sources); download(blob, filename('figwork')); lastBackupRevision = project.revision; message('项目下载已发起，包含原始文件、布局与哈希清单。'); }); }
function removeSelected() { mutate(value => { value.panels = value.panels.filter(item => !selection.has(item.id)); value.annotations = value.annotations.filter(item => !selection.has(item.id)); selection = new Set(); }); }
function duplicateSelected() {
  const originals = selected(); if (!originals.length) return;
  mutate(value => { if (value.panels.length + originals.filter(item => item.asset_id).length > LIMITS.panels) throw new Error('面板数量超限'); const next = new Set(), groups = new Map(); for (const original of originals) { const item = clone(original); item.id = uid(original.asset_id ? 'panel' : 'annotation'); item.x_mm += 3; item.y_mm += 3; if (item.asset_id) { item.label = ''; item.label_mode = 'auto'; } if (item.group_id) { if (!groups.has(item.group_id)) groups.set(item.group_id, uid('group')); item.group_id = groups.get(item.group_id); } (item.asset_id ? value.panels : value.annotations).push(item); next.add(item.id); } selection = next; });
}
function alignSelection(mode) {
  mutate(() => applyPlacements(planUnitAlignment(project, selection, mode)));
}
function addAnnotation(type) { mutate(value => { const item = {id: uid('annotation'), type, x_mm: 12, y_mm: 14, width_mm: type === 'line' ? 25 : 30, height_mm: type === 'line' ? 0 : type === 'rect' ? 18 : 5, text: type === 'text' ? '添加文字' : '', font_size_pt: 8, color: '#263c2d'}; value.annotations.push(item); selection = new Set([item.id]); }); }
function exportNote() {
  const format = $('#export-format').value, dpi = Number($('#export-dpi').value), w = Math.round(project.figure.width_mm / 25.4 * dpi), h = Math.round(project.figure.height_mm / 25.4 * dpi), preset = journalPresetById(project.settings.composition.journal_preset_id);
  $('#export-dpi-field').hidden = format === 'svg'; $('#export-note').textContent = format === 'svg' ? 'SVG 保留布局、可编辑标号文字及内嵌字体；照片仍为位图。源 SVG 自身字体请核验。' : `${w} × ${h} px；${formatLayoutNumber(project.figure.width_mm)} × ${formatLayoutNumber(project.figure.height_mm)} mm。${format === 'pdf' ? '此 PDF 为无损栅格页面，不保留可编辑曲线与文字。' : 'PNG 写入对应的物理分辨率元数据。'}${preset?.editable_vector_required ? '当前期刊模板要求可编辑矢量，请优先选 SVG 或在本机版导出矢量 PDF。' : ''}`;
}
function showExport() { if (!finishParameterEdit()) return; $('#export-issues').innerHTML = issuesHtml(allIssues()); exportNote(); $('#export-dialog').showModal(); }
async function performExport() {
  await withBusy('正在生成图稿…', async () => {
    const blocking = [...figureIssues(project), ...fontIssues(project)].filter(issue => issue.severity === 'error'); if (blocking.length) throw new Error('请先修复越界、重复编号或缺失字形，再导出图稿。');
    const format = $('#export-format').value, dpi = Number($('#export-dpi').value), current = snapshot();
    const blob = format === 'svg' ? await exportSvg(current, sources) : format === 'pdf' ? await exportRasterPdf(current, sources, dpi) : await exportPng(current, sources, dpi);
    download(blob, filename(format)); $('#export-dialog').close(); message(`${format.toUpperCase()} 已生成，${(blob.size / 1048576).toFixed(1)} MiB。`);
  });
}
function installEmptyWorkspace(empty) {
  clearTimeout(parameterTimer); pendingParameterForm = null; parameterSession = null; parameterDrafts.clear(); objectUi.clear();
  releaseSources(sources);project=empty;revisionClock=0;sources=new Map();selection=new Set();history=[];future=[];
  lastSavedRevision=0;lastBackupRevision=-1;fitted=true;recoveryPending=false;persistEnabled=true;
  $('#workspace-recovery-dialog').close();$('#save-status').className='';render();
}
async function restoreLocalWorkspace() {
  return withBusy('正在恢复本地工作区…',async()=>{
    const restored=await loadWorkspace();if(!restored)throw new Error('未找到可恢复的工作区，请新建项目。');
    await installProject(restored);recoveryPending=false;lastSavedRevision=project.revision;persistEnabled=true;
    $('#workspace-recovery-dialog').close();$('#save-status').textContent='已根据你的选择恢复本地工作区';$('#save-status').className='';render();
  });
}
async function resetProject() {
  if ((hasWorkspaceContent(project)||recoveryPending) && !confirm('新建会替换当前或上次自动保存的工作区。请先恢复并保存需要保留的项目。原图副本可通过“清除本地数据”删除。确定新建？')) return false;
  clearTimeout(autosaveTimer);saveSequence++;const empty=newProject();
  await saveWorkspace(empty,new Map(),{startNewGeneration:true});
  installEmptyWorkspace(empty);$('#save-status').textContent='空白工作区已创建';return true;
}
async function clearLocalWorkspace() {
  if(!confirm('将删除此应用在浏览器中保存的全部原图副本和工作区，无法撤销。电脑上的原文件、已下载的项目和导出文件不会删除。确认清除？'))return false;
  clearTimeout(autosaveTimer);saveSequence++;
  await clearWorkspace();
  installEmptyWorkspace(newProject());$('#save-status').textContent='此应用的本地工作区和原图副本已清除';message('本地数据已清除，撤销与重做记录已释放。');return true;
}
const actions = {
  import: () => $('#image-files').click(), open: () => $('#project-file').click(), save: saveProject, export: showExport, new: () => withBusy('正在新建工作区…', () => resetProject()), restore: restoreLocalWorkspace, clear: () => withBusy('正在清除本地数据…',clearLocalWorkspace),
  undo: () => { if (history.length) { future.push(snapshot()); project = history.pop(); markChanged(); } }, redo: () => { if (future.length) { history.push(snapshot()); project = future.pop(); markChanged(); } },
  fit: () => { fitted = true; renderCanvas(); }, 'select-all': () => { selection = new Set([...project.panels, ...project.annotations].map(item => item.id)); render(); },
  'auto-layout': () => mutate(value => { if (!value.panels.length) throw new Error('请先导入至少一张图片'); applyPanelLayout(value); }),
  delete: removeSelected, duplicate: duplicateSelected, text: () => addAnnotation('text'), rect: () => addAnnotation('rect'), line: () => addAnnotation('line'),
  group: () => mutate(() => { const objects = selected(); if (objects.length < 2 || objects.some(item => !item.asset_id)) throw new Error('编组仅支持至少两个图片面板。图片与标注可以多选移动，但不能混合编组。'); const id = uid('group'); for (const item of objects) item.group_id = id; }), ungroup: () => mutate(() => { const groups = new Set(selected().map(item => item.group_id).filter(Boolean)); for (const item of project.panels) if (selection.has(item.id) || groups.has(item.group_id)) delete item.group_id; }),
  'auto-label': () => mutate(() => { for (const item of selected()) if (item.asset_id) { item.label_mode = 'auto'; item.label = ''; } }),
  'flip-h': () => mutate(() => { for (const item of selected()) if (item.asset_id) item.flip_h = !item.flip_h; }), 'flip-v': () => mutate(() => { for (const item of selected()) if (item.asset_id) item.flip_v = !item.flip_v; }),
  front: () => mutate(value => { for (const key of ['panels', 'annotations']) value[key] = [...value[key].filter(item => !selection.has(item.id)), ...value[key].filter(item => selection.has(item.id))]; }),
  back: () => mutate(value => { for (const key of ['panels', 'annotations']) value[key] = [...value[key].filter(item => selection.has(item.id)), ...value[key].filter(item => !selection.has(item.id))]; }),
  check: () => { $('#check-results').innerHTML = issuesHtml(allIssues()); $('#check-dialog').showModal(); },
};
document.addEventListener('click', event => {
  const target = event.target.closest('[data-action]'); if (target) { event.preventDefault(); if (!busy) { const action = target.dataset.action; if(recoveryPending&&!['restore','new','clear'].includes(action)){$('#workspace-recovery-dialog').showModal();return;} if (['undo','redo','new','open','clear','restore'].includes(action)) cancelParameterEdit(); else if (!finishParameterEdit()) return; if (action.startsWith('align-') || action.startsWith('distribute-')) alignSelection(action); else actions[action]?.(); } return; }
  const row = event.target.closest('[data-select]'); if (row) { selectObject(row.dataset.select, event.shiftKey); return; }
  const grid = event.target.closest('[data-grid]'); if (grid && !busy) { if (!finishParameterEdit()) return; const [columns, rows] = grid.dataset.grid.split(',').map(Number); mutate(value => applyPanelLayout(value, {preset: 'grid', columns, rows})); }
});
$('#image-files').addEventListener('change', event => { const files = [...event.target.files]; event.target.value = ''; importImages(files); });
$('#project-file').addEventListener('change', event => { const file = event.target.files[0]; event.target.value = ''; openFile(file); });
$('#project-title').addEventListener('change', event => { const title = event.target.value.trim() || '未命名图稿'; if (finishParameterEdit()) mutate(value => { value.title = title; }); });
$('#zoom').addEventListener('input', event => { fitted = false; zoom = Number(event.target.value) / 100; renderCanvas(); });
$('#journal-preset').innerHTML += JOURNAL_PRESETS.map(preset => `<option value="${e(preset.id)}">${e(preset.title)}</option>`).join('');
function parameterForm(element) {
  if (!element?.matches('input,textarea,select') || ['number-style', 'number-order', 'number-enabled'].includes(element.id)) return null;
  return element.closest('#canvas-form,#layout-form,#object-form');
}
function parameterFeedback(form, text = '') {
  const feedback = form.querySelector('.parameter-feedback');
  if (feedback) { feedback.hidden = !text; feedback.textContent = text ? `${text} 未应用；可修改数值，或按 Esc 恢复。` : ''; }
  for (const control of form.elements) {
    if (text && control.matches('input,textarea,select') && (parameterDrafts.has(control) || !control.validity.valid)) control.setAttribute('aria-invalid', 'true');
    else control.removeAttribute('aria-invalid');
  }
}
function parameterContext(form) { return `${project.id}:${form.id}:${form.dataset.objectId || ''}`; }
function beginParameterEdit(form) {
  if (parameterSession?.form === form && parameterSession.context === parameterContext(form)) return parameterSession;
  if (parameterSession && !finishParameterEdit()) return null;
  parameterSession = {form, context: parameterContext(form), before: snapshot(), historyEntry: null};
  return parameterSession;
}
function cancelParameterEdit() {
  clearTimeout(parameterTimer); pendingParameterForm = null; parameterSession = null; parameterDrafts.clear(); composingParameter = false;
  for (const form of document.querySelectorAll('#canvas-form,#layout-form,#object-form')) parameterFeedback(form);
  renderProperties();
}
function finishParameterEdit({discardInvalid = false} = {}) {
  clearTimeout(parameterTimer);
  if (pendingParameterForm && !commitParameterForm(pendingParameterForm)) {
    if (discardInvalid) { cancelParameterEdit(); return true; }
    return false;
  }
  parameterSession = null; parameterDrafts.clear(); renderProperties(); return true;
}
function invalidParameterMessage(control) {
  const label = control.closest('label')?.childNodes[0]?.textContent?.trim() || '参数';
  if (control.value === '') return `${label}不能为空。`;
  if (control.validity.rangeUnderflow || control.validity.rangeOverflow) return `${label}须在 ${control.min || '允许的最小值'}–${control.max || '允许的最大值'} 范围内。`;
  if (control.validity.stepMismatch) return `${label}须为整数。`;
  return `${label}不是有效数值。`;
}
function commitParameterForm(form) {
  clearTimeout(parameterTimer);
  const session = parameterSession;
  if (busy || composingParameter) return false;
  if (!session || session.form !== form || !form.isConnected || session.context !== parameterContext(form)) { cancelParameterEdit(); return true; }
  if (form.id === 'object-form' && (selected().length !== 1 || selected()[0].id !== form.dataset.objectId)) { cancelParameterEdit(); return true; }
  const invalid = Array.from(form.elements).find(control => control.willValidate && !control.validity.valid);
  if (invalid) { parameterFeedback(form, invalidParameterMessage(invalid)); return false; }
  let cropChanged = false;
  const applied = mutate(value => {
    if (form.id === 'canvas-form') {
      const width = Number(form.elements.width.value), height = Number(form.elements.height.value), mode = $('#resize-mode').value;
      if (width !== value.figure.width_mm || height !== value.figure.height_mm) {
        // Recompute a continuous width/height edit from its starting geometry.
        // Incremental fit transforms would repeatedly shrink an existing layout.
        const base = session.before;
        value.figure = clone(base.figure); value.panels = clone(base.panels); value.annotations = clone(base.annotations);
        if (mode === 'reflow') assertUngroupedLayout(value);
        if (width !== value.figure.width_mm || height !== value.figure.height_mm) resizeFigure(value, width, height, {mode});
      }
      if (mode !== (value.settings.composition.resize_mode || 'fit')) value.settings.composition.resize_mode = mode;
    } else if (form.id === 'layout-form') {
      const options = Object.fromEntries([...new FormData(form)].map(([key, current]) => [key, key === 'preset' ? current : Number(current)]));
      if (Object.entries(options).some(([key, current]) => layoutOptions(value)[key] !== current)) applyPanelLayout(value, options);
    } else {
      const object = [...value.panels, ...value.annotations].find(item => item.id === form.dataset.objectId);
      if (!object) throw new Error('所选对象已变化，请重新选择。');
      for (const key of ['x_mm', 'y_mm', 'width_mm', 'height_mm', 'rotation', 'label_font_size_pt', 'title_font_size_pt', 'font_size_pt']) {
        const control = form.elements.namedItem(key); if (control) object[key] = readNumericInput(control);
      }
      for (const key of ['label', 'title', 'label_mode', 'text', 'color']) {
        const control = form.elements.namedItem(key); if (control) object[key] = control.value;
      }
      if (object.asset_id) {
        const crop = Object.fromEntries(['x', 'y', 'width', 'height'].map(key => [key, readNumericInput(form.elements.namedItem(`crop_${key}`))]));
        cropChanged = JSON.stringify(crop) !== JSON.stringify(object.crop); object.crop = crop;
        if (form.elements.lock_aspect.checked && cropChanged) object.height_mm = object.width_mm * crop.height / crop.width;
      }
    }
  }, {session, form});
  if (!applied) return false;
  parameterFeedback(form); pendingParameterForm = null;
  if (cropChanged && form.elements.lock_aspect.checked) {
    const object = selected()[0]; setDerivedParameter(form.elements.height_mm, object.height_mm);
    session.aspectRatio = object.width_mm / object.height_mm;
  }
  return true;
}
function setDerivedParameter(control, value) {
  control.value = formatLayoutNumber(value); control.dataset.layoutValue = String(value); control.dataset.layoutDisplay = control.value; delete control.dataset.layoutEdited;
  parameterDrafts.add(control);
}
function queueParameterEdit(control, immediate = false) {
  const form = parameterForm(control); if (!form || control.name === 'lock_aspect' || busy) return;
  const requestedValue = control.value;
  const session = beginParameterEdit(form); if (!session) return;
  if (control.value !== requestedValue) control.value = requestedValue;
  markLayoutNumberEdited(control); parameterDrafts.add(control); pendingParameterForm = form;
  if (form.id === 'object-form') {
    if (control.name === 'label') { form.elements.label_mode.value = control.value ? 'manual' : 'none'; parameterDrafts.add(form.elements.label_mode); }
    const object = selected()[0];
    if (object?.asset_id && form.elements.lock_aspect.checked && ['width_mm', 'height_mm'].includes(control.name) && control.value !== '' && control.validity.valid) {
      session.aspectRatio ||= object.width_mm / object.height_mm;
      setDerivedParameter(form.elements.namedItem(control.name === 'width_mm' ? 'height_mm' : 'width_mm'), control.name === 'width_mm' ? Number(control.value) / session.aspectRatio : Number(control.value) * session.aspectRatio);
    }
  }
  clearTimeout(parameterTimer);
  if (composingParameter) return;
  if (immediate) commitParameterForm(form); else parameterTimer = setTimeout(() => commitParameterForm(form), AUTO_APPLY_DELAY);
}
$('#journal-preset').addEventListener('change', event => {
  const id = event.target.value, preset = journalPresetById(id);
  if (!finishParameterEdit()) { renderProperties(); return; }
  if (id === (project.settings.composition.journal_preset_id || '')) return;
  mutate(value => {
    if (preset) {
      const width = preset.width_mm || value.figure.width_mm, height = Math.min(value.figure.height_mm, preset.max_height_mm || value.figure.height_mm), mode = value.settings.composition.resize_mode || 'fit';
      if (mode === 'reflow') assertUngroupedLayout(value);
      if (preset.label_style) value.settings.composition.numbering.style = preset.label_style;
      if (preset.label_font_size_pt) for (const panel of value.panels) if (panel.label_mode === 'auto') panel.label_font_size_pt = preset.label_font_size_pt;
      reconcilePanelLabels(value); resizeFigure(value, width, height, {mode});
    }
    value.settings.composition.journal_preset_id = id || null;
    value.settings.composition.journal_preset_snapshot = preset ? clone(preset) : null;
  });
});
for (const [id, key] of [['number-style', 'style'], ['number-order', 'order'], ['number-enabled', 'enabled']]) {
  $(`#${id}`).addEventListener('change', event => {
    const current = event.target.type === 'checkbox' ? event.target.checked : event.target.value;
    if (finishParameterEdit()) mutate(value => { value.settings.composition.numbering[key] = current; });
    else renderProperties();
  });
}
document.addEventListener('input', event => { if (!event.isComposing) queueParameterEdit(event.target); });
document.addEventListener('change', event => {
  const form = parameterForm(event.target); if (!form) return;
  if (event.target.name === 'lock_aspect') {
    const context = `${project.id}:${form.dataset.objectId}`;
    objectUi.set(context, {...objectUi.get(context), lockAspect: event.target.checked, cropOpen: !!form.querySelector('details')?.open});
    if (parameterSession?.form === form) delete parameterSession.aspectRatio;
    return;
  }
  queueParameterEdit(event.target, true);
});
document.addEventListener('toggle', event => {
  if (!event.target.matches('#object-form details')) return;
  const form = event.target.closest('form'), context = `${project.id}:${form.dataset.objectId}`;
  objectUi.set(context, {lockAspect: form.elements.lock_aspect.checked, cropOpen: event.target.open});
}, true);
document.addEventListener('compositionstart', event => { if (parameterForm(event.target)) { composingParameter = true; clearTimeout(parameterTimer); } });
document.addEventListener('compositionend', event => { if (parameterForm(event.target)) { composingParameter = false; queueParameterEdit(event.target); } });
document.addEventListener('focusout', event => {
  const form = parameterForm(event.target);
  if (form && form !== parameterForm(event.relatedTarget) && parameterSession?.form === form) finishParameterEdit();
});
document.addEventListener('submit', event => {
  if (!event.target.matches('#canvas-form,#layout-form,#object-form')) return;
  event.preventDefault(); const form = event.target;
  if (beginParameterEdit(form)) { pendingParameterForm = form; finishParameterEdit(); }
});
$('#export-format').addEventListener('change', exportNote); $('#export-dpi').addEventListener('change', exportNote); $('#export-confirm').addEventListener('click', performExport);
function canvasPoint(event) { const svg = $('#studio-svg'), matrix = svg.getScreenCTM(); return new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse()); }
$('#canvas-wrap').addEventListener('pointerdown', event => {
  if (busy || gesture || event.button !== 0) return; const target = event.target.closest('[data-object]'), resize = event.target.closest('[data-resize]');
  finishParameterEdit({discardInvalid: true});
  if (!target && !resize) { selection.clear(); render(); return; }
  if (target && !resize) { const id = target.dataset.object; if (event.shiftKey) { selectObject(id, true); return; } if (!selection.has(id)) selectObject(id); }
  event.preventDefault(); selected(); gesture = {before: snapshot(), ids: new Set(selection), pointerId: event.pointerId, start: canvasPoint(event), startClient:{x:event.clientX,y:event.clientY}, originals: selected().map(clone), bounds: selectionBounds(project, selection), resize: resize?.dataset.resize || null, moved: false, error: null,guides:[]};
});
function processGestureFrame() {
  const pointer = pendingPointer; pendingPointer = null; if (!gesture || !pointer) return;
  try {
    const point = canvasPoint(pointer), dx = point.x - gesture.start.x, dy = point.y - gesture.start.y;
    if (!gesture.moved && Math.hypot(pointer.clientX-gesture.startClient.x,pointer.clientY-gesture.startClient.y)<3) return; gesture.moved = true;
    let placements;
    if (gesture.resize) {
      if (gesture.originals.length === 1) {
        const original = gesture.originals[0], minimum = Math.max(.1 / original.width_mm, .1 / original.height_mm), maximum = Math.min(2000 / original.width_mm, 2000 / original.height_mm);
        const scaleX = Math.max(minimum, Math.min(maximum, 1 + dx / original.width_mm)), scaleY = pointer.shiftKey ? Math.max(.1 / original.height_mm, Math.min(2000 / original.height_mm, 1 + dy / original.height_mm)) : scaleX;
        placements = planSelectionTransform(gesture.before, gesture.ids, {scale_x: scaleX, scale_y: scaleY, pivot_x_mm: original.x_mm, pivot_y_mm: original.y_mm});
      } else {
        const bounds = gesture.bounds, minimum = Math.max(...gesture.originals.flatMap(item => [.1 / item.width_mm, .1 / item.height_mm])), maximum = Math.min(...gesture.originals.flatMap(item => [2000 / item.width_mm, 2000 / item.height_mm]));
        const scale = Math.max(minimum, Math.min(maximum, 1 + (dx * bounds.width + dy * bounds.height) / (bounds.width * bounds.width + bounds.height * bounds.height)));
        placements = planSelectionTransform(gesture.before, gesture.ids, {scale, pivot_x_mm: bounds.x, pivot_y_mm: bounds.y});
      }
    } else {
      const snapped=snapTranslation(gesture.before,gesture.originals,dx,dy,{enabled:$('#snap-enabled').checked&&!pointer.altKey,pixelsPerMm:svgPixelsPerMm($('#studio-svg'))});
      gesture.guides=snapped.guides;
      placements = planSelectionTransform(gesture.before, gesture.ids, {dx_mm: snapped.dx_mm, dy_mm: snapped.dy_mm});
    }
    applyPlacements(placements); gesture.error = null; renderCanvas();
  } catch (error) { gesture.error = error.message; }
}
function stopGestureFrame() { if (gestureFrame) cancelAnimationFrame(gestureFrame); gestureFrame = 0; }
function cancelGesture() { stopGestureFrame(); pendingPointer = null; if (gesture) { project = gesture.before; gesture = null; render(); } }
window.addEventListener('pointermove', event => {
  if (!gesture || busy || event.pointerId !== gesture.pointerId) return; pendingPointer = {clientX: event.clientX, clientY: event.clientY, shiftKey: event.shiftKey,altKey:event.altKey};
  if (!gestureFrame) gestureFrame = requestAnimationFrame(() => { gestureFrame = 0; processGestureFrame(); });
});
window.addEventListener('pointerup', event => {
  if (!gesture || event.pointerId !== gesture.pointerId) return; stopGestureFrame(); pendingPointer = {clientX: event.clientX, clientY: event.clientY, shiftKey: event.shiftKey,altKey:event.altKey}; processGestureFrame();
  const before = gesture.before, ids = gesture.ids, errorMessage = gesture.error; gesture = null;
  try { if (errorMessage) throw new Error(errorMessage); if (selectionGeometryChanged(before, project, ids)) { reconcilePanelLabels(project); validateBrowserProject(project); remember(before); markChanged(); } else render(); } catch (error) { project = before; render(); message(error.message, 'error'); }
});
window.addEventListener('pointercancel', event => { if (gesture?.pointerId === event.pointerId) cancelGesture(); });
window.addEventListener('blur', cancelGesture);
document.addEventListener('keydown', event => {
  if (event.isComposing || composingParameter) return;
  const form = parameterForm(event.target), command = event.ctrlKey || event.metaKey, key = event.key.toLowerCase();
  if (form && event.key === 'Escape') { event.preventDefault(); cancelParameterEdit(); return; }
  if (form && event.key === 'Enter' && (event.target.tagName !== 'TEXTAREA' || command)) { event.preventDefault(); finishParameterEdit(); return; }
  if (form && command && ['z', 'y', 's'].includes(key) && !busy && !$('dialog[open]')) {
    event.preventDefault();
    if (key === 's') saveProject();
    else { cancelParameterEdit(); actions[key === 'y' || event.shiftKey ? 'redo' : 'undo'](); }
    return;
  }
  if (gesture && event.key === 'Escape') { event.preventDefault(); cancelGesture(); return; }
  if (event.target.closest('input,textarea,select,[contenteditable="true"]') || $('dialog[open]') || busy) return;
  if (gesture) cancelGesture();
  if (command && ['z', 'y', 's', 'a', 'd', 'g'].includes(key)) { event.preventDefault(); const action = key === 'z' ? event.shiftKey ? 'redo' : 'undo' : key === 'y' ? 'redo' : key === 's' ? 'save' : key === 'a' ? 'select-all' : key === 'd' ? 'duplicate' : event.shiftKey ? 'ungroup' : 'group'; if (['undo','redo'].includes(action)) cancelParameterEdit(); else if (!finishParameterEdit()) return; actions[action]?.(); return; }
  if (event.key === 'Delete' || event.key === 'Backspace') { event.preventDefault(); if (selection.size && finishParameterEdit()) removeSelected(); }
  if (event.key === 'Escape') { cancelParameterEdit(); selection.clear(); render(); }
  if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key) && selection.size) { event.preventDefault(); if (!finishParameterEdit()) return; const step = event.shiftKey ? 1 : .1; mutate(() => { for (const item of selected()) { if (event.key === 'ArrowLeft') item.x_mm -= step; if (event.key === 'ArrowRight') item.x_mm += step; if (event.key === 'ArrowUp') item.y_mm -= step; if (event.key === 'ArrowDown') item.y_mm += step; } }); }
});
document.addEventListener('dragover', event => { if (event.dataTransfer.types.includes('Files')) { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; $('#canvas-viewport').classList.add('drag-over'); } });
document.addEventListener('dragleave', event => { if (!event.relatedTarget) $('#canvas-viewport').classList.remove('drag-over'); });
document.addEventListener('drop', event => { event.preventDefault(); $('#canvas-viewport').classList.remove('drag-over'); if (busy) return; const files = [...event.dataTransfer.files]; if (files.length === 1 && /\.figwork$/i.test(files[0].name)) openFile(files[0]); else importImages(files); });
document.addEventListener('paste', event => { if (event.target.closest('input,textarea,[contenteditable="true"]') || busy) return; const files = [...(event.clipboardData?.files || [])]; if (files.length) { event.preventDefault(); importImages(files); } });
new ResizeObserver(() => { if (fitted && !gesture) renderCanvas(); }).observe($('#canvas-viewport'));
window.addEventListener('beforeunload', event => {
  if (pendingParameterForm && !composingParameter) commitParameterForm(pendingParameterForm);
  if (pendingParameterForm || hasWorkspaceContent(project) && project.revision !== lastBackupRevision && (!persistEnabled || project.revision !== lastSavedRevision)) { event.preventDefault(); event.returnValue = ''; }
});

render();
$('#workspace-recovery-dialog').addEventListener('cancel',event=>{if(recoveryPending)event.preventDefault();});
await withBusy('正在检查本地工作区…', async () => {
  try {
    const status=await getWorkspaceStatus();recoveryPending=status.has_recovery;
    if(recoveryPending){$('#save-status').textContent='尚未恢复本地素材';$('#workspace-recovery-dialog').showModal();}
    else $('#save-status').textContent='空白工作区 · 导入后自动保存到此浏览器';
  } catch (error) {
    persistEnabled=false;$('#save-status').textContent='自动保存不可用，请下载项目';$('#save-status').className='error';
    message(`无法检查本地工作区：${error.message}。现有本地数据未被覆盖；可通过“清除本地数据”重试清理。`,'error');
  }
  render();
});
prepareFonts(project).then(renderCanvas).catch(() => {});

