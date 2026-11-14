import {applyPanelLayout as applySharedLayout, resizeFigure, layoutOptions, ensureCompositionSettings, reconcilePanelLabels} from './modules/layout-engine.js';
import {JOURNAL_PRESETS, journalPresetById, journalPreflight} from './modules/journal-presets.js';
import {LIMITS, encodeProject, decodeProject} from './browser/archive.js';
import {newProject, panelForAsset, validateBrowserProject, uid, clone, recipeSnapshot, round, escapeXml as e, selectedObjects, expandedSelection, selectionUnits, selectionBounds, planUnitAlignment, planSelectionTransform, selectionGeometryChanged, assertUngroupedLayout, captureCommittedWorkspace, figureIssues, panelBounds} from './browser/model.js';
import {createSource, releaseSources, sourceBudget} from './browser/sources.js';
import {figureMarkup, exportSvg, exportPng, exportRasterPdf, prepareFonts, fontIssues} from './browser/render.js';
import {loadWorkspace, saveWorkspace, clearWorkspace} from './browser/persistence.js';
import {numberField,readNumericInput,markLayoutNumberEdited} from './browser/forms.js';
import {hasWorkspaceContent} from './browser/model.js';
import {formatLayoutNumber} from './modules/numeric-input.js';

const $ = selector => document.querySelector(selector);
if (!globalThis.crypto?.subtle || !globalThis.crypto?.randomUUID || typeof structuredClone !== 'function') {
  $('#studio-workspace').innerHTML = '<section style="padding:36px;grid-column:1/-1"><h1>当前打开方式不支持此工作台</h1><p style="margin-top:16px">请使用新版 Chrome 或 Edge，直接打开完整 HTML 文件，或通过 HTTPS 网站访问。</p><p style="margin-top:8px">图片尚未读取；无需安装 Python、Node 或本机服务器。</p></section>';
  $('#save-status').textContent = '浏览器能力不足，工作区尚未启动'; for (const control of document.querySelectorAll('button,input,select')) control.disabled = true;
  throw new Error('浏览器组图需要安全上下文中的 WebCrypto 和 structuredClone。');
}
let project = newProject(), sources = new Map(), selection = new Set(), history = [], future = [], busy = false, autosaveTimer, messageTimer, zoom = 1, fitted = true, gesture = null, gestureFrame = 0, pendingPointer = null, saveSequence = 0, revisionClock = 0, persistEnabled = true, lastSavedRevision = -1, lastBackupRevision = -1;
const selected = () => { selection = expandedSelection(project, selection); return selectedObjects(project, selection); };
const assetOf = panel => project.assets.find(asset => asset.id === panel.asset_id);
const snapshot = () => recipeSnapshot(project);
function applyPanelLayout(value, overrides) { assertUngroupedLayout(value); return applySharedLayout(value, overrides); }
function applyPlacements(placements) { const objects = new Map([...project.panels, ...project.annotations].map(item => [item.id, item])); for (const placement of placements) Object.assign(objects.get(placement.id), placement); }
function message(text, tone = '') { clearTimeout(messageTimer); $('#messages').innerHTML = `<div class="${tone === 'error' ? 'error' : ''}">${e(text)}</div>`; messageTimer = setTimeout(() => { $('#messages').innerHTML = ''; }, tone === 'error' ? 11000 : 5000); }
function remember(before) { history.push(before); if (history.length > 50) history.shift(); future = []; }
function markChanged() {
  revisionClock = Math.max(revisionClock, project.revision) + 1; project.revision = revisionClock; project.updated_at = new Date().toISOString(); selection = expandedSelection(project, selection); render();
  const current = project; prepareFonts(current).then(() => { if (project === current && !gesture) renderCanvas(); }).catch(() => {});
  // Capture the last committed recipe now: pointer drags mutate live geometry between commits.
  const {project: committed, sources: committedSources} = captureCommittedWorkspace(project, sources), sequence = ++saveSequence; clearTimeout(autosaveTimer); $('#save-status').textContent = persistEnabled ? '等待本地保存…' : '自动保存不可用，请下载项目';
  if (persistEnabled) autosaveTimer = setTimeout(() => {
    $('#save-status').textContent = '正在本地保存…';
    saveWorkspace(committed, committedSources).then(() => { if (sequence === saveSequence) { lastSavedRevision = committed.revision; $('#save-status').textContent = '已自动保存到此浏览器'; $('#save-status').className = ''; } }).catch(error => { persistEnabled = false; $('#save-status').textContent = '自动保存失败，请下载项目'; $('#save-status').className = 'error'; message(`本地保存不可用：${error.message}。请点击“保存项目”备份。`, 'error'); });
  }, 650);
}
function mutate(operation) {
  if (busy) return; const before = snapshot();
  try { operation(project); ensureCompositionSettings(project); reconcilePanelLabels(project); validateBrowserProject(project); remember(before); markChanged(); }
  catch (error) { project = before; render(); message(error.message, 'error'); }
}
async function withBusy(label, operation) {
  if (busy) return; busy = true; document.body.classList.add('busy'); $('#busy-status').textContent = label; const exportButton = $('#export-confirm'); exportButton.disabled = true;
  try { return await operation(); } catch (error) { message(error.message || String(error), 'error'); }
  finally { busy = false; document.body.classList.remove('busy'); $('#busy-status').textContent = ''; exportButton.disabled = false; renderButtons(); }
}
function selectObject(id, toggle = false) {
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
  $('#empty-state').hidden = !!(project.panels.length || project.annotations.length); $('#zoom').value = String(Math.round(zoom * 100)); $('#zoom-value').textContent = `${Math.round(zoom * 100)}%`;
}
function renderAssets() {
  $('#panel-count').textContent = project.panels.length;
  $('#asset-list').innerHTML = project.panels.length ? project.panels.map(panel => { const asset = assetOf(panel); return `<div class="asset-row ${selection.has(panel.id) ? 'selected' : ''}" data-select="${e(panel.id)}" tabindex="0" role="button" aria-label="选择面板 ${e(panel.label)} ${e(asset.name)}"><img src="${e(sources.get(asset.id)?.url || '')}" alt=""><div><span class="asset-label">${e(panel.label || '无编号')}${panel.label_mode === 'manual' ? ' · 锁定' : ''}${panel.group_id ? ' · 编组' : ''}</span><strong title="${e(asset.name)}">${e(asset.name)}</strong><small>${asset.width_px} × ${asset.height_px} px</small></div></div>`; }).join('') : '<div class="asset-list-empty">原件导入后显示在这里</div>';
}
function renderJournalNote() {
  const preset = journalPresetById($('#journal-preset').value);
  $('#journal-note').innerHTML = preset ? `${e(preset.notes?.[0] || '')}<details><summary>规格说明与来源</summary>${(preset.notes || []).slice(1).map(note => `<p>${e(note)}</p>`).join('')}${preset.source_url ? ` <a href="${e(preset.source_snapshot_url || preset.source_url)}" target="_blank" rel="noopener noreferrer">查看依据</a>` : ''}</details>` : '按毫米设置最终图稿尺寸。';
}
function renderProperties() {
  $('#project-title').value = project.title; $('#canvas-width').value = project.figure.width_mm; $('#canvas-height').value = project.figure.height_mm;
  $('#journal-preset').value = project.settings.composition.journal_preset_id || ''; renderJournalNote();
  const layout = layoutOptions(project); for (const [key, value] of Object.entries(layout)) $(`#layout-form [name="${key}"]`).value = value; $('#number-style').value = project.settings.composition.numbering.style;
  $('#number-enabled').checked = project.settings.composition.numbering.enabled; $('#number-order').value = project.settings.composition.numbering.order;
  const objects = selected(), unitCount = selectionUnits(project, selection).length, panel = objects.length === 1 && objects[0].asset_id ? objects[0] : null, annotation = objects.length === 1 && !objects[0].asset_id ? objects[0] : null;
  $('#selection-info').textContent = objects.length ? `已选择 ${objects.length} 个对象` : '未选中对象'; $('#figure-size').textContent = `${formatLayoutNumber(project.figure.width_mm)} × ${formatLayoutNumber(project.figure.height_mm)} mm`; $('#demo-badge').hidden = !project.settings.demo;
  let content = '';
  if (panel) {
    const asset = assetOf(panel), ppi = asset.kind === 'svg' ? null : Math.min(panel.crop.width / panel.width_mm, panel.crop.height / panel.height_mm) * 25.4;
    content = `<section class="property-section"><div class="property-title"><h3>选中面板</h3><span>${e(panel.label || '·')}</span></div><form id="object-form"><div class="field-grid"><label class="field">编号<textarea name="label" rows="1" maxlength="240">${e(panel.label)}</textarea></label><label class="field">编号模式<select name="label_mode"><option value="auto" ${panel.label_mode === 'auto' ? 'selected' : ''}>自动</option><option value="manual" ${panel.label_mode === 'manual' ? 'selected' : ''}>手工锁定</option><option value="none" ${panel.label_mode === 'none' ? 'selected' : ''}>不显示</option></select></label></div><label class="field">标题<textarea name="title" rows="2" maxlength="240">${e(panel.title)}</textarea></label><div class="field-grid">${numberField('X / mm', 'x_mm', panel.x_mm)}${numberField('Y / mm', 'y_mm', panel.y_mm)}${numberField('宽 / mm', 'width_mm', panel.width_mm, 'min=".1" max="2000"')}${numberField('高 / mm', 'height_mm', panel.height_mm, 'min=".1" max="2000"')}${numberField('旋转 / °', 'rotation', panel.rotation || 0)}${numberField('编号 / pt', 'label_font_size_pt', panel.label_font_size_pt || 10, 'min="1" max="100"')}${numberField('标题 / pt', 'title_font_size_pt', panel.title_font_size_pt || 8, 'min="1" max="100"')}</div><label class="compact-check"><input type="checkbox" name="lock_aspect" checked>锁定宽高比</label><details><summary class="note">原图裁切 / px</summary><div class="field-grid">${numberField('X', 'crop_x', panel.crop.x, 'min="0"')}${numberField('Y', 'crop_y', panel.crop.y, 'min="0"')}${numberField('宽', 'crop_width', panel.crop.width, 'min="1"')}${numberField('高', 'crop_height', panel.crop.height, 'min="1"')}</div></details><button class="wide" type="submit">应用面板设置</button></form><p class="note ${ppi != null && ppi < 300 ? 'warning' : ''}">${ppi == null ? '来源为 SVG；源文件内嵌位图仍需核验。' : `原图实际分辨率 ${Math.round(ppi)} ppi`}</p><div class="selection-actions"><button data-action="flip-h">水平翻转</button><button data-action="flip-v">垂直翻转</button><button data-action="auto-label">恢复自动编号</button></div></section>`;
  } else if (annotation) {
    content = `<section class="property-section"><h3>选中标注</h3><form id="object-form">${annotation.type === 'text' ? `<label class="field">文字<textarea name="text" rows="3" maxlength="2000">${e(annotation.text)}</textarea></label>` : ''}<div class="field-grid">${numberField('X / mm', 'x_mm', annotation.x_mm)}${numberField('Y / mm', 'y_mm', annotation.y_mm)}${numberField('宽 / mm', 'width_mm', annotation.width_mm)}${numberField('高 / mm', 'height_mm', annotation.height_mm)}${numberField('字号 / pt', 'font_size_pt', annotation.font_size_pt || 8, 'min="1" max="100"')}<label class="field">颜色<input type="color" name="color" value="${e(annotation.color)}"></label></div><button type="submit" class="wide">应用标注设置</button></form></section>`;
  } else if (objects.length) content = `<section class="property-section"><h3>已选择 ${objects.length} 个对象</h3><p class="muted">拖动任一选中对象可一起移动。图片多选时可拖右下角整体等比缩放。编组仅支持图片；对齐与分布按整个编组计算。</p><button class="wide" data-action="auto-label">所选面板恢复自动编号</button></section>`;
  $('#selection-properties').innerHTML = content;
  $('#selection-toolbar').innerHTML = objects.length ? [['duplicate', '复制'], ['group', '编组'], ['ungroup', '解组'], ['align-left', '左齐'], ['align-top', '顶齐'], ['align-center', '水平居中'], ['align-middle', '垂直居中'], ['distribute-x', '水平等距'], ['distribute-y', '垂直等距'], ['front', '置顶'], ['back', '置底'], ['delete', '删除']].map(([action, title]) => `<button data-action="${action}" ${(action === 'group' && objects.length < 2 || action.startsWith('align-') && unitCount < 2 || action.startsWith('distribute') && unitCount < 3) ? 'disabled' : ''}>${title}</button>`).join('') : '<span class="muted">点击图片选择，拖动调整位置；拖入文件自动加入图稿</span>';
  const issues = allIssues(); $('#preflight-summary').textContent = !project.panels.length ? '导入后检查实际分辨率、编号和画布边界。' : issues.length ? `${issues.filter(item => item.severity === 'error').length} 项错误，${issues.filter(item => item.severity !== 'error').length} 项提示。点击“检查图稿”查看。` : '当前检查项通过；导出前仍请人工检查内容。';
}
function render() { selection = expandedSelection(project, selection); renderAssets(); renderCanvas(); renderProperties(); renderButtons(); }
function allIssues() { return [...figureIssues(project), ...fontIssues(project), ...journalPreflight(project)]; }
function issuesHtml(issues) { return issues.length ? `<div class="issue-list">${issues.map(issue => `<div class="issue ${issue.severity === 'error' ? 'error' : ''}">${e(issue.message)}</div>`).join('')}</div>` : '<div class="issue ok">当前检查项通过。</div>'; }
function download(blob, filename) { const url = URL.createObjectURL(blob), anchor = document.createElement('a'); anchor.href = url; anchor.download = filename; document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000); }
const filename = extension => `${Array.from(project.title).filter(character => character.charCodeAt(0) >= 32).join('').replace(/[\\/:*?"<>|]/g, '_').slice(0, 100) || 'figure'}.${extension}`;
async function importImages(files) {
  if (!files.length) return;
  await withBusy(`正在导入 ${files.length} 份原件…`, async () => {
    assertUngroupedLayout(project);
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
  if (!file) return; await withBusy('校验项目与全部原件…', async () => {
    if (hasWorkspaceContent(project) && !confirm('打开项目会替换当前工作区。请先用“保存项目”保留当前图稿。继续打开？')) return;
    if (file.size > LIMITS.totalBytes) throw new Error('项目超过浏览器版 192 MiB 上限');
    const incoming = await decodeProject(new Uint8Array(await file.arrayBuffer())); await installProject(incoming); markChanged(); message('项目及原件哈希已验证，图稿已恢复。');
  });
}
async function saveProject() { await withBusy('正在校验并保存完整项目…', async () => { const blob = await encodeProject(project, sources); download(blob, filename('figwork')); lastBackupRevision = project.revision; message('项目下载已发起，包含原始文件、布局与哈希清单。'); }); }
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
function showExport() { $('#export-issues').innerHTML = issuesHtml(allIssues()); exportNote(); $('#export-dialog').showModal(); }
async function performExport() {
  await withBusy('正在生成图稿…', async () => {
    const blocking = [...figureIssues(project), ...fontIssues(project)].filter(issue => issue.severity === 'error'); if (blocking.length) throw new Error('请先修复越界、重复编号或缺失字形，再导出图稿。');
    const format = $('#export-format').value, dpi = Number($('#export-dpi').value), current = snapshot();
    const blob = format === 'svg' ? await exportSvg(current, sources) : format === 'pdf' ? await exportRasterPdf(current, sources, dpi) : await exportPng(current, sources, dpi);
    download(blob, filename(format)); $('#export-dialog').close(); message(`${format.toUpperCase()} 已生成，${(blob.size / 1048576).toFixed(1)} MiB。`);
  });
}
async function demo() {
  if (hasWorkspaceContent(project) && !confirm('示例会替换当前工作区。请先保存项目。继续打开合成示例？')) return;
  const files = await withBusy('正在生成合成示例…', async () => {
  await resetProject(false); project.title = '合成示例 · 规则与自由组图'; project.settings.demo = true; const generated = [];
  for (let index = 0; index < 6; index++) {
    const canvas = document.createElement('canvas'); canvas.width = 1200; canvas.height = 800; const context = canvas.getContext('2d');
    if (index < 3) { context.fillStyle = '#f1f0ec'; context.fillRect(0, 0, 1200, 800); for (let lane = 0; lane < 6; lane++) { context.fillStyle = `rgba(34,38,31,${.25 + ((lane * 7 + index * 3) % 10) / 16})`; context.beginPath(); context.ellipse(130 + lane * 185, 330 + index * 30 + (lane % 2) * 14, 55, 20 + lane % 3 * 5, 0, 0, Math.PI * 2); context.fill(); } }
    else { context.fillStyle = '#10251e'; context.fillRect(0, 0, 1200, 800); for (let cell = 0; cell < 55; cell++) { const x = 45 + (cell * 137 + index * 39) % 1120, y = 40 + (cell * 83) % 720; context.fillStyle = ['#62b38e', '#9fb2e7', '#d9b47d'][(index + cell) % 3]; context.globalAlpha = .4 + cell % 4 * .15; context.beginPath(); context.ellipse(x, y, 14 + cell % 13, 11 + cell % 7, cell, 0, Math.PI * 2); context.fill(); } context.globalAlpha = 1; }
    context.fillStyle = index < 3 ? '#888c80' : '#a6b9ab'; context.font = '24px sans-serif'; context.fillText('SYNTHETIC DEMO — NOT EXPERIMENTAL DATA', 35, 755);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png')); if (!blob) throw new Error('合成示例生成失败'); generated.push(new File([blob], `SYNTHETIC_${index + 1}.png`, {type: 'image/png'})); canvas.width = 0;
  }
  return generated;
  });
  if (files) { await importImages(files); selection = new Set(); render(); }
}
async function resetProject(ask = true) {
  if (ask && hasWorkspaceContent(project) && !confirm('新建会清空当前工作区。请先下载项目备份。确定新建？')) return false;
  clearTimeout(autosaveTimer); saveSequence++; await clearWorkspace().catch(() => {}); releaseSources(sources); project = newProject(); revisionClock = 0; sources = new Map(); selection = new Set(); history = []; future = []; lastSavedRevision = -1; lastBackupRevision = -1; fitted = true; render(); return true;
}
const actions = {
  import: () => $('#image-files').click(), open: () => $('#project-file').click(), save: saveProject, export: showExport, new: () => withBusy('正在新建工作区…', () => resetProject()), demo,
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
  const target = event.target.closest('[data-action]'); if (target) { event.preventDefault(); if (!busy) { const action = target.dataset.action; if (action.startsWith('align-') || action.startsWith('distribute-')) alignSelection(action); else actions[action]?.(); } return; }
  const row = event.target.closest('[data-select]'); if (row) { selectObject(row.dataset.select, event.shiftKey); return; }
  const grid = event.target.closest('[data-grid]'); if (grid && !busy) { const [columns, rows] = grid.dataset.grid.split(',').map(Number); mutate(value => applyPanelLayout(value, {preset: 'grid', columns, rows})); }
});
$('#image-files').addEventListener('change', event => { const files = [...event.target.files]; event.target.value = ''; importImages(files); });
$('#project-file').addEventListener('change', event => { const file = event.target.files[0]; event.target.value = ''; openFile(file); });
$('#project-title').addEventListener('change', event => mutate(value => { value.title = event.target.value.trim() || '未命名图稿'; }));
$('#zoom').addEventListener('input', event => { fitted = false; zoom = Number(event.target.value) / 100; renderCanvas(); });
$('#journal-preset').innerHTML += JOURNAL_PRESETS.map(preset => `<option value="${e(preset.id)}">${e(preset.title)}</option>`).join('');
$('#journal-preset').addEventListener('change', () => { const preset = journalPresetById($('#journal-preset').value); if (preset?.width_mm) $('#canvas-width').value = preset.width_mm; if (preset?.max_height_mm && Number($('#canvas-height').value) > preset.max_height_mm) $('#canvas-height').value = preset.max_height_mm; renderJournalNote(); });
$('#canvas-form').addEventListener('submit', event => { event.preventDefault(); const id = $('#journal-preset').value, preset = journalPresetById(id), width = Number($('#canvas-width').value), height = Number($('#canvas-height').value), mode = $('#resize-mode').value; mutate(value => { if (mode === 'reflow') assertUngroupedLayout(value); if (preset?.label_style) value.settings.composition.numbering.style = preset.label_style; if (preset?.label_font_size_pt) for (const panel of value.panels) if (panel.label_mode === 'auto') panel.label_font_size_pt = preset.label_font_size_pt; reconcilePanelLabels(value); resizeFigure(value, width, height, {mode}); value.settings.composition.journal_preset_id = id || null; value.settings.composition.journal_preset_snapshot = preset ? clone(preset) : null; }); });
$('#layout-form').addEventListener('submit', event => { event.preventDefault(); const data = new FormData(event.target), options = Object.fromEntries([...data].map(([key, value]) => [key, key === 'preset' ? value : Number(value)])), style = $('#number-style').value; mutate(value => { value.settings.composition.numbering.style = style; applyPanelLayout(value, options); }); });
$('#number-style').addEventListener('change', event => { const style = event.target.value; mutate(value => { value.settings.composition.numbering.style = style; }); });
$('#number-enabled').addEventListener('change', event => { const enabled = event.target.checked; mutate(value => { value.settings.composition.numbering.enabled = enabled; }); });
$('#number-order').addEventListener('change', event => { const order = event.target.value; mutate(value => { value.settings.composition.numbering.order = order; }); });
document.addEventListener('input', event => {
  const form = event.target.closest('#object-form'); if (!form) return;
  markLayoutNumberEdited(event.target);
  if (event.target.name === 'label') form.elements.label_mode.value = event.target.value ? 'manual' : 'none';
  const objects = selected(); if (objects.length !== 1 || !objects[0].asset_id || !form.elements.lock_aspect?.checked) return;
  const panel = objects[0], ratio = panel.width_mm / panel.height_mm;
  if (event.target.name === 'width_mm') { form.elements.height_mm.value = round(Number(event.target.value) / ratio); markLayoutNumberEdited(form.elements.height_mm); }
  if (event.target.name === 'height_mm') { form.elements.width_mm.value = round(Number(event.target.value) * ratio); markLayoutNumberEdited(form.elements.width_mm); }
});
document.addEventListener('submit', event => {
  if (event.target.id !== 'object-form') return; event.preventDefault(); const values = new FormData(event.target), object = selected()[0]; if (!object) return;
  mutate(() => {
    for (const key of ['x_mm', 'y_mm', 'width_mm', 'height_mm', 'rotation', 'label_font_size_pt', 'title_font_size_pt', 'font_size_pt']) if (values.has(key)) object[key] = readNumericInput(event.target.elements.namedItem(key));
    for (const key of ['label', 'title', 'label_mode', 'text', 'color']) if (values.has(key)) object[key] = String(values.get(key));
    if (object.asset_id) { const oldCrop = JSON.stringify(object.crop); object.crop = Object.fromEntries(['x', 'y', 'width', 'height'].map(key => [key, Number(values.get(`crop_${key}`))])); if (values.has('lock_aspect') && oldCrop !== JSON.stringify(object.crop)) object.height_mm = object.width_mm * object.crop.height / object.crop.width; }
  });
});
$('#export-format').addEventListener('change', exportNote); $('#export-dpi').addEventListener('change', exportNote); $('#export-confirm').addEventListener('click', performExport);
function canvasPoint(event) { const svg = $('#studio-svg'), matrix = svg.getScreenCTM(); return new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse()); }
$('#canvas-wrap').addEventListener('pointerdown', event => {
  if (busy || gesture || event.button !== 0) return; const target = event.target.closest('[data-object]'), resize = event.target.closest('[data-resize]');
  if (!target && !resize) { selection.clear(); render(); return; }
  if (target && !resize) { const id = target.dataset.object; if (event.shiftKey) { selectObject(id, true); return; } if (!selection.has(id)) selectObject(id); }
  event.preventDefault(); selected(); gesture = {before: snapshot(), ids: new Set(selection), pointerId: event.pointerId, start: canvasPoint(event), originals: selected().map(clone), bounds: selectionBounds(project, selection), resize: resize?.dataset.resize || null, moved: false, error: null};
});
function processGestureFrame() {
  const pointer = pendingPointer; pendingPointer = null; if (!gesture || !pointer) return;
  try {
    const point = canvasPoint(pointer), dx = point.x - gesture.start.x, dy = point.y - gesture.start.y;
    if (!gesture.moved && dx * dx + dy * dy < .0025) return; gesture.moved = true;
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
      let moveX = dx, moveY = dy;
      if ($('#snap-enabled').checked) {
        const bounds = gesture.bounds, left = bounds.x, top = bounds.y, right = left + bounds.width, bottom = top + bounds.height;
        const targetsX = [0, project.figure.width_mm / 2, project.figure.width_mm], targetsY = [0, project.figure.height_mm / 2, project.figure.height_mm];
        for (const item of gesture.before.panels) if (!gesture.ids.has(item.id)) { const box = panelBounds(item); targetsX.push(box.x, box.x + box.width / 2, box.x + box.width); targetsY.push(box.y, box.y + box.height / 2, box.y + box.height); }
        const adjustment = (moving, targets) => { let best = 1, result = 0; for (const position of moving) for (const target of targets) if (Math.abs(target - position) < best) { best = Math.abs(target - position); result = target - position; } return result; };
        moveX += adjustment([left + dx, (left + right) / 2 + dx, right + dx], targetsX); moveY += adjustment([top + dy, (top + bottom) / 2 + dy, bottom + dy], targetsY);
      }
      placements = planSelectionTransform(gesture.before, gesture.ids, {dx_mm: moveX, dy_mm: moveY});
    }
    applyPlacements(placements); gesture.error = null; renderCanvas();
  } catch (error) { gesture.error = error.message; }
}
function stopGestureFrame() { if (gestureFrame) cancelAnimationFrame(gestureFrame); gestureFrame = 0; }
function cancelGesture() { stopGestureFrame(); pendingPointer = null; if (gesture) { project = gesture.before; gesture = null; render(); } }
window.addEventListener('pointermove', event => {
  if (!gesture || busy || event.pointerId !== gesture.pointerId) return; pendingPointer = {clientX: event.clientX, clientY: event.clientY, shiftKey: event.shiftKey};
  if (!gestureFrame) gestureFrame = requestAnimationFrame(() => { gestureFrame = 0; processGestureFrame(); });
});
window.addEventListener('pointerup', event => {
  if (!gesture || event.pointerId !== gesture.pointerId) return; stopGestureFrame(); pendingPointer = {clientX: event.clientX, clientY: event.clientY, shiftKey: event.shiftKey}; processGestureFrame();
  const before = gesture.before, ids = gesture.ids, errorMessage = gesture.error; gesture = null;
  try { if (errorMessage) throw new Error(errorMessage); if (selectionGeometryChanged(before, project, ids)) { reconcilePanelLabels(project); validateBrowserProject(project); remember(before); markChanged(); } else render(); } catch (error) { project = before; render(); message(error.message, 'error'); }
});
window.addEventListener('pointercancel', event => { if (gesture?.pointerId === event.pointerId) cancelGesture(); });
window.addEventListener('blur', cancelGesture);
document.addEventListener('keydown', event => {
  if (gesture && event.key === 'Escape') { event.preventDefault(); cancelGesture(); return; }
  if (event.target.closest('input,textarea,select,[contenteditable="true"]') || $('dialog[open]') || busy) return;
  if (gesture) cancelGesture();
  const command = event.ctrlKey || event.metaKey, key = event.key.toLowerCase();
  if (command && ['z', 'y', 's', 'a', 'd', 'g'].includes(key)) { event.preventDefault(); const action = key === 'z' ? event.shiftKey ? 'redo' : 'undo' : key === 'y' ? 'redo' : key === 's' ? 'save' : key === 'a' ? 'select-all' : key === 'd' ? 'duplicate' : event.shiftKey ? 'ungroup' : 'group'; actions[action]?.(); return; }
  if (event.key === 'Delete' || event.key === 'Backspace') { event.preventDefault(); if (selection.size) removeSelected(); }
  if (event.key === 'Escape') { selection.clear(); render(); }
  if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key) && selection.size) { event.preventDefault(); const step = event.shiftKey ? 1 : .1; mutate(() => { for (const item of selected()) { if (event.key === 'ArrowLeft') item.x_mm -= step; if (event.key === 'ArrowRight') item.x_mm += step; if (event.key === 'ArrowUp') item.y_mm -= step; if (event.key === 'ArrowDown') item.y_mm += step; } }); }
});
document.addEventListener('dragover', event => { if (event.dataTransfer.types.includes('Files')) { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; $('#canvas-viewport').classList.add('drag-over'); } });
document.addEventListener('dragleave', event => { if (!event.relatedTarget) $('#canvas-viewport').classList.remove('drag-over'); });
document.addEventListener('drop', event => { event.preventDefault(); $('#canvas-viewport').classList.remove('drag-over'); if (busy) return; const files = [...event.dataTransfer.files]; if (files.length === 1 && /\.figwork$/i.test(files[0].name)) openFile(files[0]); else importImages(files); });
document.addEventListener('paste', event => { if (event.target.closest('input,textarea,[contenteditable="true"]') || busy) return; const files = [...(event.clipboardData?.files || [])]; if (files.length) { event.preventDefault(); importImages(files); } });
new ResizeObserver(() => { if (fitted && !gesture) renderCanvas(); }).observe($('#canvas-viewport'));
window.addEventListener('beforeunload', event => { if (hasWorkspaceContent(project) && project.revision !== lastBackupRevision && (!persistEnabled || project.revision !== lastSavedRevision)) { event.preventDefault(); event.returnValue = ''; } });

render();
await withBusy('恢复浏览器工作区…', async () => {
  try { const restored = await loadWorkspace(); if (restored) { await installProject(restored); lastSavedRevision = project.revision; message('已恢复此浏览器上次保存的图稿。'); } $('#save-status').textContent = '自动保存已就绪 · 请定期下载项目备份'; }
  catch (error) { persistEnabled = false; $('#save-status').textContent = '自动保存不可用，请下载项目'; $('#save-status').className = 'error'; message(`未恢复本地工作区：${error.message}。可使用“打开项目”恢复备份。`, 'error'); }
  render();
});
prepareFonts(project).then(renderCanvas).catch(() => {});
