import {state,currentRawSource,analysisRegionKey,sourceChannelView} from './state.js';
import {escapeHtml as e, assetFor} from './domain.js';
import {icon} from './icons.js';
import {renderSourceCanvas} from './source-view.js';
import {renderComposer} from './composer-view.js';
import {syncComposerViewport} from './composer-viewport.js';
import {renderAnalysis} from './analysis.js';
import {renderInspector} from './inspector.js';
import {renderChannelComposition} from './channel-view.js';
import {channelPreviews} from './channels.js';
import {draftBlocksRender} from './input-lifecycle.js';
import {renderMarkup,sameControlBinding} from './dom-render.js';
import {sourceIdentityKey} from './channel-domain.js';
import {renderWbWorkflow} from './wb-workflow.js';
import {renderHome,renderWorkspaceGuide,renderQuickStart,hasProjectContent} from './workflows.js';
import {formatLayoutNumber} from './numeric-input.js';
import {exportSizeIssue} from './export-options.js';
import {createModalFocusLifecycle} from './modal-focus.js';

function header() {
  const saveLabels = {saved: '本机已保存', pending: '等待保存', saving: '正在保存…', error: '本机保存失败'};
  return `<header class="app-header"><a class="brand" href="#" data-action="home" aria-label="FigPub 首页"><span class="brand-symbol">${icon('logo')}</span><div><strong>FigPub</strong><span>SCIENTIFIC IMAGE &amp; FIGURE STUDIO</span></div></a><div class="project-title-wrap"><input id="project-title" class="project-title" data-project-title value="${e(state.project.title || '未命名 Figure')}" aria-label="项目名称"><button class="icon-button small" data-action="project-menu" title="项目操作">${icon('down')}</button><span class="save-indicator ${state.saveState}" title="编辑后的工作区自动保存在本机；再次启动须主动选择恢复；另存项目可创建 .figwork 文件" role="status">${state.saveState === 'saving' ? '<i class="spinner tiny"></i>' : '<i></i>'}${state.workspaceStatus?.has_recovery?'待选择工作区':state.workspaceStatus?.cleanup_pending?'待完成清理':saveLabels[state.saveState]}</span></div><div class="header-actions"><button class="icon-button" data-action="undo" title="撤销 Ctrl+Z" ${state.history.length ? '' : 'disabled'}>${icon('undo')}</button><button class="icon-button" data-action="redo" title="重做 Ctrl+Y" ${state.future.length ? '' : 'disabled'}>${icon('redo')}</button><span class="toolbar-divider"></span><button class="button subtle" data-action="save-project" title="另存 .figwork 项目，包含全部原件、参数和布局 · Ctrl+Shift+S">${icon('save')}<span>另存项目</span></button><button class="button primary" data-action="export-dialog">${icon('download')}导出图稿</button></div></header>`;
}

function workflowNavigation() {
  const tabs = [['home','folder','首页'],['source','image','素材库'],['gel','gel','凝胶 / WB'],['channels','microscope','多通道'],['analysis','chart','分析'],['compose','layers','组图']];
  const demo = state.project.title?.toLowerCase().includes('demo') || state.project.title?.includes('示例') || state.project.settings?.is_demo;
  return `<nav class="workflow-nav" aria-label="工作台功能"><div class="workflow-tabs">${tabs.map(([value,iconName,label]) => `<button data-action="tab" data-tab="${value}" class="workflow-tab ${state.tab === value ? 'active' : ''}" aria-current="${state.tab === value ? 'page' : 'false'}">${icon(iconName)}<span>${label}</span></button>`).join('')}</div><div class="workflow-meta">${demo ? '<span class="demo-label">合成示例 · 非实验数据</span>' : '<span class="local-label"><i class="status-dot"></i>本地工作区</span>'}${state.showGuide===false&&state.tab!=='home'?'<button class="text-button" data-action="toggle-guide">显示操作提示</button>':''}<button class="icon-button small" data-action="help" title="快速入门" aria-label="快速入门">${icon('help')}</button></div></nav>`;
}

function sidebar() {
  const project = state.project, query = state.filter.toLowerCase();
  const assets = project.assets.filter(asset => (!query || asset.name.toLowerCase().includes(query)) && (state.assetKind === 'all' || (state.assetKind === 'table' ? asset.kind === 'table' : asset.kind !== 'table')));
  return `<aside class="asset-sidebar"><div class="sidebar-heading"><h2>素材库 <span class="count-badge">${project.assets.length}</span></h2><button class="icon-button small" data-action="import" title="导入素材">${icon('plus')}</button></div><div class="sidebar-search">${icon('search')}<input id="asset-search" data-asset-search placeholder="搜索素材" value="${e(state.filter)}" aria-label="搜索素材"></div><div class="asset-filters"><button class="${state.assetKind === 'all' ? 'selected' : ''}" data-action="asset-filter" data-kind="all">全部</button><button class="${state.assetKind === 'image' ? 'selected' : ''}" data-action="asset-filter" data-kind="image">图像</button><button class="${state.assetKind === 'table' ? 'selected' : ''}" data-action="asset-filter" data-kind="table">数据表</button></div><div class="asset-list" data-drop-zone="import">${assets.length ? assets.map(asset => `<button class="asset-card ${state.selectedAssetId === asset.id ? 'selected' : ''}" data-action="select-asset" data-id="${asset.id}" draggable="${asset.kind !== 'table'}" title="${e(asset.name)}"><span class="asset-thumb">${asset.kind === 'table' ? icon('grid') : `<img src="${e(asset.preview_url || `/api/assets/${asset.id}/preview`)}?max_size=180" alt="" loading="lazy">`}</span><span class="asset-details"><strong>${e(asset.name)}</strong><small>${asset.kind === 'table' ? '数据表' : `${asset.width_px || '—'} × ${asset.height_px || '—'} px`}</small><span class="asset-badges"><i>${e(asset.kind === 'image' ? asset.name.split('.').at(-1).toUpperCase() : asset.kind.toUpperCase())}</i>${asset.bit_depth ? `<i>${asset.bit_depth}-bit</i>` : ''}${asset.metadata?.derived_from ? '<i>分析结果</i>' : ''}</span></span>${project.panels.some(panel => panel.asset_id === asset.id) ? '<span class="asset-used" title="已用于组图">'+icon('check')+'</span>' : ''}</button>`).join('') : `<div class="asset-empty">${icon('folder')}<p>${project.assets.length ? '没有匹配的素材' : '尚未导入素材'}</p><button class="button small" data-action="import">${icon('plus')}导入素材</button></div>`}</div>
    ${state.tab === 'compose' ? `<section class="layers-panel"><div class="sidebar-heading"><h2>图层 <span class="count-badge">${project.panels.length + project.annotations.length}</span></h2><button class="icon-button small" data-action="auto-layout" title="自动排列">${icon('grid')}</button></div><div class="layer-list">${project.panels.map(panel => `<button class="layer-row ${state.selectedPanelId === panel.id ? 'selected' : ''}" data-action="select-panel" data-id="${panel.id}"><span class="layer-letter">${e(panel.label || '·')}</span><span>${e(panel.title || assetFor(project,panel.asset_id)?.name || '素材缺失')}</span>${icon('image')}</button>`).join('')}${project.annotations.map(annotation => `<button class="layer-row ${state.selectedAnnotationId === annotation.id ? 'selected' : ''}" data-action="select-annotation" data-id="${annotation.id}">${icon(annotation.type === 'text' ? 'text' : annotation.type === 'rect' ? 'rect' : 'line')}<span>${e(annotation.text || (annotation.type === 'rect' ? '区域框' : '线条'))}</span></button>`).join('')}${!project.panels.length && !project.annotations.length ? '<p class="small-empty">画布还没有对象</p>' : ''}</div></section>` : ''}
    <div class="sidebar-bottom">${state.tab==='gel'?`<button class="button full primary" data-action="import-wb">${icon('gel')}导入 WB 原图</button>`:''}<button class="button full ${state.tab==='gel'?'subtle':'primary'}" data-action="import">${icon('upload')}${state.tab==='analysis'&&state.analysisKind==='qpcr'?'导入 Ct 数据表':state.tab==='compose'?'导入图片 / 图表':'导入素材'}</button><div><button class="text-button" data-action="open-project">打开项目</button><span>·</span><button class="text-button" data-action="clear-workspace-dialog">清除本机数据</button></div></div></aside>`;
}

function footer() {
  const project = state.project, used = new Set(project.panels.map(panel => panel.asset_id));
  return `<footer class="app-status"><span><i class="status-dot"></i>${e(state.statusText)}</span><div><span>${project.assets.length} 份素材</span><b>·</b><span>${used.size} 份已使用</span><b>·</b><span>项目修订 ${project.revision || 0}</span></div></footer>`;
}

function modal() {
  if (!state.modal) return '';
  const type = state.modal.type || state.modal;
  let title = '', content = '', footerContent = '';
  if (type === 'export') {
    title = '导出图稿'; const format = state.modal.format || 'pdf';
    const formatIssue = exportSizeIssue(format,state.project.figure);
    const pptxIssue = exportSizeIssue('pptx',state.project.figure);
    const disabledReason = formatIssue || (!state.project.panels.length&&!state.project.annotations.length&&format!=='sources'?'请先将素材加入画布':'');
    const formats = [['pdf','PDF','矢量布局与嵌入字体'],['svg','SVG','矢量标注与嵌入图像'],['png','PNG','通用无损图像'],['tiff','TIFF','期刊常用位图'],['pptx','PPTX','独立图像与可编辑标注'],['sources','原始资料包','完整源文件与记录']];
    content = `<p class="modal-intro">按最终版面尺寸导出，保留源图与矢量标注的对应关系。</p><div class="export-format-grid">${formats.map(([value,name,description]) => `<label class="export-format ${format === value ? 'selected' : ''} ${value==='pptx'&&pptxIssue?'disabled':''}"><input type="radio" id="export-format-${value}" name="export-format" data-export-format value="${value}" ${format === value ? 'checked' : ''} ${value==='pptx'&&pptxIssue?'disabled aria-describedby="export-format-hint"':''}><strong>${name}</strong><span>${description}</span>${format === value ? icon('check') : ''}</label>`).join('')}</div>${pptxIssue||format==='pptx'?`<p class="microcopy ${formatIssue?'export-format-error':''}" id="export-format-hint" ${formatIssue?'role="alert"':''}>${e(pptxIssue || 'PPTX 保留画布实际尺寸；宽、高均需为 25.4–1422.4 mm。')}</p>`:''}<div class="export-settings"><label class="field"><span>位图导出分辨率</span><select id="export-dpi" data-export-dpi>${[300,600,1200].map(dpi => `<option value="${dpi}" ${(state.modal.dpi || 300) === dpi ? 'selected' : ''}>${dpi} dpi</option>`).join('')}</select></label><div class="export-size-summary"><span>画布尺寸</span><strong>${formatLayoutNumber(state.project.figure.width_mm)} × ${formatLayoutNumber(state.project.figure.height_mm)} mm</strong><small>源图有效分辨率单独检查</small></div></div>${['pdf','svg'].includes(format) ? `<label class="checkbox-label"><input type="checkbox" data-export-outlined ${state.modal.outlined ? 'checked' : ''}>文字转轮廓（固定外观，文字将不可编辑）</label>` : ''}${state.preflight ? `<div class="export-check"><strong>${icon(state.preflight.issues?.length ? 'info' : 'check')}${state.preflight.issues?.length ? `${state.preflight.issues.length} 项检查提示` : '检查项通过'}</strong>${(state.preflight.issues || []).slice(0,5).map(issue => `<p class="${issue.severity}">${e(issue.message)}</p>`).join('')}</div>` : '<button class="button full subtle" data-action="preflight">'+icon('check')+'检查当前图稿</button>'}`;
    footerContent = `<button class="button" data-action="close-modal">取消</button><button class="button primary" data-action="export-file" ${disabledReason?`disabled title="${e(disabledReason)}"`:''}>${icon('download')}开始导出</button>`;
  } else if (type === 'wb-workflow' || type === 'wb-exposures') {
    ({title,content,footerContent}=renderWbWorkflow());
  } else if (type === 'channel-composition') {
    ({title,content,footerContent}=renderChannelComposition());
  } else if (type === 'migration') {
    title = '项目已导入，请核对迁移结果';
    content = `<p class="modal-intro">已读取旧项目中可恢复的原件和编辑设置。以下内容需要与原软件或原图对照核查。</p><div class="migration-warnings">${state.modal.warnings.map((warning,index)=>`<div><span>${index+1}</span><p>${e(warning)}</p></div>`).join('')}</div>`;
    footerContent = '<button class="button primary" data-action="close-modal">进入工作台核对</button>';
  } else if (type === 'marker') {
    title = '添加 Marker 校准点';
    const asset = assetFor(state.project,state.selectedAssetId);
    content = `<p class="modal-intro">输入该条带的已知大小。校准使用原图坐标，原始像素保持不变。</p><div class="field-grid"><label class="field"><span>原图 Y 坐标 / px</span><input id="new-marker-y" type="number" min="0" max="${asset?.height_px || 100000}" value="${state.modal.y}"></label><label class="field"><span>已知大小</span><input id="new-marker-size" type="number" min="0.001" step="any" placeholder="填入 Marker 标注值"></label></div>`;
    footerContent = '<button class="button" data-action="close-modal">取消</button><button class="button primary" data-action="confirm-marker">添加校准点</button>';
  } else if (type === 'marker-candidate') {
    title = '核对 Marker 候选'; const candidate=state.modal.candidate;
    content = `<p class="modal-intro">识别结果提示泳道 ${candidate.lane_index + 1} 可能使用以下 Marker。请核对试剂型号与原图条带；匹配分数不代表识别正确率。</p><h3 class="candidate-title">${e(candidate.ladder_name)}</h3><div class="data-table-wrap"><table class="data-table"><thead><tr><th>原图 Y / px</th><th>已知大小 / ${e(candidate.unit)}</th><th>拟合偏差 / px</th></tr></thead><tbody>${candidate.points.map(point=>`<tr><td>${Math.round(point.y)}</td><td>${e(point.size)}</td><td>${Number((point.residual_px || 0).toFixed(2))}</td></tr>`).join('')}</tbody></table></div>`;
    footerContent = '<button class="button" data-action="close-modal">不采用</button><button class="button primary" data-action="confirm-candidate">确认型号与条带，应用校准</button>';
  } else if (type === 'project') {
    title = '项目'; content = `<div class="project-modal-summary"><span class="project-icon">${icon('folder')}</span><div><h3>${e(state.project.title)}</h3><p>${state.project.assets.length} 份原始素材 · ${state.project.panels.length} 个面板</p></div></div><div class="menu-actions"><button data-action="save-project">${icon('save')}<span><strong>另存项目文件</strong><small>.figwork 包含原件、设置与分析记录</small></span>${icon('chevron')}</button><button data-action="open-project">${icon('folder')}<span><strong>打开已有项目</strong><small>恢复源图、布局与分析</small></span>${icon('chevron')}</button><button data-action="new-dialog">${icon('plus')}<span><strong>新建项目</strong><small>开始一份独立的科研图稿</small></span>${icon('chevron')}</button><button data-action="clear-workspace-dialog">${icon('trash')}<span><strong>清除本机工作区</strong><small>删除软件保存的原图副本、恢复备份及预览</small></span>${icon('chevron')}</button></div>`;
    footerContent = '<button class="button" data-action="close-modal">关闭</button>';
  } else if (type === 'new') {
    title = '新建项目'; content = `<p class="modal-intro">${state.workspaceStatus?.has_recovery?'新建会替换上次自动保存的工作区；若需保留，请返回并先恢复、另存项目。软件保存的原图副本仍需通过「清除本机工作区」删除。':hasProjectContent(state.project)?'新项目会替换当前工作区。需要继续保留当前图稿时，请先另存一份 .figwork 项目。新建不会删除软件保存的原图副本。':'输入项目名称，再选择分析或组图任务。'}</p><label class="field"><span>项目名称</span><input id="new-project-title" placeholder="例如：Figure 2 · 蛋白表达" value="${e(state.modal.titleDraft || '未命名 Figure')}"></label>`;
    footerContent = `<button class="button" data-action="close-modal">取消</button>${hasProjectContent(state.project)?'<button class="button" data-action="backup-before-switch">先另存当前项目</button>':''}<button class="button primary" data-action="create-project">创建项目</button>`;
  } else if (type === 'workspace-recovery') {
    title='发现本机保存的工作区';
    content=`<p class="modal-intro">当前未加载任何旧素材。可主动恢复上次工作区，或新建空白项目。需要删除软件保存的图片副本时，请选择清除本机数据。</p><p class="microcopy">新建会替换上次自动保存记录。需要保留旧图稿时，请先恢复并另存项目。</p>${state.lastError?`<p role="alert">${e(state.lastError)}</p>`:''}`;
    footerContent='<button class="button" data-action="clear-workspace-dialog">清除本机数据</button><button class="button" data-action="new-dialog">新建空白项目</button><button class="button primary" data-action="restore-workspace">恢复上次工作区</button>';
  } else if (type === 'clear-workspace' || type === 'workspace-cleanup') {
    title=type==='workspace-cleanup'?'本机数据清理未完成':'清除本机工作区';
    content=`<p class="modal-intro">${type==='workspace-cleanup'?'部分软件副本尚未删除，已暂停素材加载。请重试清理；若文件被占用，请关闭其他客户端窗口后重试。':'此操作会删除软件保存的原图副本、当前工作区、恢复备份和分析缓存，无法通过撤销恢复。需要保留的图稿请先另存 .figwork 项目。'}</p><p>用户原始文件，以及自行另存到其他位置的 .figwork 和导出文件不在清理范围内。</p>${state.lastError?`<p role="alert">${e(state.lastError)}</p>`:''}`;
    footerContent=`${type==='workspace-cleanup'?'':'<button class="button" data-action="close-modal">取消</button>'}${hasProjectContent(state.project)&&type!=='workspace-cleanup'?'<button class="button" data-action="backup-before-switch">先另存当前项目</button>':''}<button class="button primary" data-action="clear-workspace">${type==='workspace-cleanup'?'重试清理':'确认清除软件副本'}</button>`;
  } else if (type === 'workspace-cleanup-exit') {
    title='完成预览缓存清理';
    content='<p class="modal-intro">工作区和软件保存的原图副本已删除。</p><p>请退出桌面客户端，使其完成预览缓存清理。若通过浏览器访问，请同时清除此应用站点的缓存。</p><p class="microcopy">完成上述步骤前，不应视为所有预览缓存均已清除。</p>';
    footerContent='<button class="button primary" data-action="close-modal">知道了</button>';
  } else if(type === 'switch-project') {
    title='打开项目';content=`<p class="modal-intro">即将打开 <strong>${e(state.modal.file?.name || '所选项目')}</strong>，并替换当前工作区。需要保留当前内容时，请先另存项目；保存完成后再继续打开。</p>`;
    footerContent='<button class="button" data-action="close-modal">取消</button><button class="button" data-action="backup-before-switch">先另存当前项目</button><button class="button primary" data-action="confirm-project-switch">继续打开</button>';
  } else if (type === 'help') {
    title = '快速入门'; content = renderQuickStart();
    footerContent = '<button class="button primary" data-action="close-modal">关闭</button>';
  }
  return `<div class="modal-backdrop" data-modal-backdrop><section class="modal ${type === 'export' ? 'export-modal' : ''}" role="dialog" aria-modal="true" aria-labelledby="modal-title" ${state.busy?'inert':''}><header><h2 id="modal-title">${title}</h2>${['workspace-recovery','workspace-cleanup'].includes(type)?'':`<button class="icon-button" data-action="close-modal" aria-label="关闭">${icon('close')}</button>`}</header><div class="modal-body">${content}</div><footer>${footerContent}</footer></section></div>`;
}

function captureFocus() {
  const element = document.activeElement;
  const modalButton=element?.matches('.modal button')?[...document.querySelectorAll('.modal button')].indexOf(element):null;
  if(modalButton===null&&(!element?.id||!['INPUT','TEXTAREA','SELECT'].includes(element.tagName)&&!element.matches('[data-focus-anchor]')))return null;
  return {element,id: element.id, modalButton, value: element.value, start: element.selectionStart, end: element.selectionEnd};
}

let renderedScope=null,renderedModal=null;
const modalFocus=createModalFocusLifecycle();
export function renderApp() {
  if (state.interactionLock || !state.project) return;
  // Replacing a dirty native input discards its pending `change` event, even if its
  // value is copied back. Preview/save completions wait until that edit commits.
  if(draftBlocksRender(document.activeElement)){state.renderDeferred=true;return;}
  modalFocus.beforeRender(state);
  state.renderDeferred=false;
  channelPreviews.beginFrame();
  const openDetails=new Set([...document.querySelectorAll('details[id][open]')].map(element=>element.id));
  const focused = state.discardInputDraft?null:captureFocus(), scrollPositions = [...document.querySelectorAll('.home-workspace,.asset-list,.inspector-scroll,.analysis-workspace,.artboard-viewport,.data-table-wrap')].map((element,index) => [element.className,index,element.scrollTop,element.scrollLeft]);
  state.discardInputDraft=false;
  const main = state.tab === 'home' ? renderHome(state) : state.tab === 'compose' ? renderComposer() : state.tab === 'analysis' ? renderAnalysis() : renderSourceCanvas();
  const scope=JSON.stringify([state.project.id,state.tab,state.selectedAssetId,state.selectedPanelId,state.selectedAnnotationId,state.analysisKind,state.modal?.type || '',sourceIdentityKey(currentRawSource()),sourceChannelView()?.channels.map(channel=>[channel.id,sourceIdentityKey(channel.source)]),state.tab==='analysis'?analysisRegionKey():'',state.modal?.entries?.map(sourceIdentityKey)]);
  const sameContext=scope===renderedScope&&state.modal===renderedModal;
  const workspace=state.tab==='home'?main:`<div class="workspace-grid">${sidebar()}<main class="workspace-main" id="workspace-main">${main}</main>${renderInspector()}</div>`;
  const operationError=state.lastError?`<div class="operation-error" role="alert"><span>${icon('alert')}<strong>操作未完成</strong> ${e(state.lastError)}</span>${state.saveState==='error'?'<button class="button small" data-action="retry-save">重试保存</button>':''}<button class="icon-button small" data-action="dismiss-error" aria-label="关闭错误提示">${icon('close')}</button></div>`:'';
  const activity=(state.analysisSubmission||state.job&&['queued','running'].includes(state.job.status))&&state.tab!=='analysis'?`<div class="background-activity" role="status">${state.job?.pollingError?icon('alert'):'<span class="spinner tiny"></span>'}<span>${state.analysisSubmission?'分析任务正在提交。':state.job.pollingError?'暂时无法读取分析任务状态，请查看任务后重试查询或取消。':'分析正在后台运行，完成后可在「分析」中查看结果。'}</span><button class="text-button" data-action="show-running-analysis">查看任务</button></div>`:'';
  const retained=renderMarkup(document.getElementById('app'),`<div class="app-shell ${state.tab==='home'?'home-shell':''}" ${state.busy||state.modal?'inert':''} ${state.busy?'aria-busy="true"':''}>${header()}${workflowNavigation()}${operationError}${activity}${renderWorkspaceGuide(state)}${workspace}${footer()}</div>${modal()}${state.busy ? `<div class="busy-banner" role="status" aria-live="polite"><span class="spinner"></span><strong>${e(state.busy)}</strong>${state.busyController ? '<button data-action="cancel-busy">取消</button>' : ''}</div>` : ''}`,!state.busy&&sameContext?focused?.element:null);
  renderedScope=scope;renderedModal=state.modal;
  channelPreviews.endFrame();
  for(const id of openDetails){const element=document.getElementById(id);if(element?.tagName==='DETAILS')element.open=true;}
  const newScroll = [...document.querySelectorAll('.home-workspace,.asset-list,.inspector-scroll,.analysis-workspace,.artboard-viewport,.data-table-wrap')];
  for (const [,index,top,left] of scrollPositions) {const element = newScroll[index]; if (element) {element.scrollTop = top; element.scrollLeft = left;}}
  let restored=retained;
  // Preserve an explicitly focused control or result anchor across redraws.
  // A different measurement has different bindings and does not inherit focus.
  if (focused&&!retained&&sameContext) {const element = focused.modalButton===null?document.getElementById(focused.id):document.querySelectorAll('.modal button')[focused.modalButton]; if (element && !element.disabled && sameControlBinding(focused.element,element) && element.value===focused.value && !state.busy && (!state.modal || element.closest('.modal'))) {element.focus({preventScroll:true});restored=true;try {if(focused.start != null) element.setSelectionRange(focused.start,focused.end);} catch {}}}
  if(state.modal&&!state.busy&&!restored){const target=document.getElementById('new-marker-size')||document.getElementById('new-project-title')||document.querySelector('.modal input[type=radio]:checked')||document.querySelector('.modal input:not([type=radio])')||document.querySelector('.modal footer .primary')||document.querySelector('.modal button');target?.focus({preventScroll:true});}
  syncComposerViewport();
  modalFocus.afterRender(state);
}

