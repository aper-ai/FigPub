import {request} from './api.js';
import {clone, ensureProject, assetFor, panelFor, defaultEdit} from './domain.js';
import {defaultAnalysisParams,restoredAnalysisParams,resolveAnalysisResult} from './analysis-domain.js';
import {analysisChannelChoices,analysisContextKey,descriptorKey,sourceIdentityKey} from './channel-domain.js';
import {reconcilePanelLabels} from './layout-engine.js';
import {initializeQpcrWorkspaces,qpcrParameters} from './qpcr-workspace.js';
import {isActiveAnalysisJob} from './analysis-controller.js';

export const state = {
  project: null, health: null, workspaceStatus: null, tab: 'home', workspaceTask: null, importIntent: null, showGuide: true, lastError: null, lastErrorScope: null, selectedAssetId: null, selectedPanelId: null, selectedPanelIds: [], selectedAnnotationId: null,
  selectedRoiId: null, sourceRoi: null, sourceTool: 'select', sourceZoom: 1, sourcePan:{x:0,y:0},spacePressed:false,figureZoom:1,
  filter: '', assetKind: 'all', history: [], future: [], mutationSequence: 0, dirty: false, saveState: 'saved',
  busy: null, busyController: null, modal: null, preflight: null, showLayers: true,
  analysisKind: 'wb', analysisParams: defaultAnalysisParams(),
  channelDescriptors:new Map(),channelDescriptorErrors:new Map(),channelRangeReport:null,
  sourcePointerIdentity:null,sourcePolygon:[],analysisRegionTarget:'roi',cellposeStatus:null,cellposeStatusLoading:false,
  job: null, analysisSubmission: null, resultRecord: null, resultStale: false, table: null, tableLoading: false, tableError: null, qpcrSheetNames:new Map(),gelWorker: null, gelBusy: false,gelTaskToken:null,wbWorking:false,wbTaskToken:null,
  showRoiTable: true, figureTool: 'select', snap: true, statusText: '本地运行 · 原始文件保持不变'
};
let listeners = [], saveTimer = null, savePromise = null, interactionWaiters = [];
const toastScopes = new WeakMap();
let activeQpcrContext=null;
function qpcrContext() {
  if(selectedAsset()?.kind!=='table')return null;
  return JSON.stringify([state.project.id,state.selectedAssetId,state.project.settings.qpcr_workspaces?.[state.selectedAssetId]?.sheet || null]);
}
function restoreActiveAnalysisParams() {
  state.analysisParams=restoredAnalysisParams(state.project?.settings);
  if(selectedAsset()?.kind==='table'&&state.project.settings.qpcr_workspaces)Object.assign(state.analysisParams,qpcrParameters(state.project,state.selectedAssetId));
  activeQpcrContext=qpcrContext();
}
export function subscribe(listener) { listeners.push(listener); return () => {listeners = listeners.filter(item => item !== listener);}; }
export function analysisSelection(kind=state.analysisKind) {
  const values=kind==='qpcr'&&selectedAsset()?.kind==='table'&&state.project.settings.qpcr_workspaces?{...state.analysisParams,...qpcrParameters(state.project,state.selectedAssetId)}:state.analysisParams;
  return resolveAnalysisResult({project:state.project,kind,assetId:state.selectedAssetId,values,roi:state.sourceRoi,
    channels:kind==='microscopy'?selectedAnalysisChannels():null,source:kind==='qpcr'?null:kind==='microscopy'?(selectedAnalysisChannels()[0]?.source || currentRawSource()):currentRawSource(),
    geometry:kind==='microscopy'?analysisGeometry():null,transientRecord:state.resultRecord});
}
export function emit() {
  reconcileTaskNotices();
  if(qpcrContext()!==activeQpcrContext)restoreActiveAnalysisParams();
  const existing=new Set((state.project?.panels || []).map(panel=>panel.id));
  if(!existing.has(state.selectedPanelId))state.selectedPanelId=null;
  state.selectedPanelIds=selectedPanels().map(panel=>panel.id);
  if(!state.interactionLock && interactionWaiters.length){const waiting=interactionWaiters;interactionWaiters=[];waiting.forEach(resolve=>resolve());}
  state.resultStale=analysisSelection().stale;for (const listener of listeners) listener(state);
}
function taskNoticeResolved(scope) {
  if(!scope)return false;
  const sameProject=scope.projectId===state.project?.id;
  if(scope.code==='analysis-switch-submission')return !sameProject||state.analysisSubmission!==scope.submission;
  if(scope.code==='workspace-switch-gel')return !sameProject||!state.gelBusy||state.gelTaskToken!==scope.taskToken;
  if(scope.code==='workspace-switch-wb')return !sameProject||!state.wbWorking||state.wbTaskToken!==scope.taskToken;
  if(['analysis-switch-job','analysis-cancel-error','analysis-status-unavailable'].includes(scope.code)) {
    if(!sameProject||state.job?.id!==scope.jobId||!isActiveAnalysisJob(state.job))return true;
    if(scope.code==='analysis-switch-job')return Boolean(state.job.pollingError)!==scope.statusUnknown;
    if(scope.code==='analysis-status-unavailable')return !state.job.pollingError;
    return false;
  }
  if(['analysis-submit-error','analysis-task-error'].includes(scope.code)) {
    const next=state.analysisSubmission,previous=scope.context;
    if(previous&&previous.projectId!==state.project?.id)return true;
    return Boolean(next&&next!==previous&&previous&&['projectId','kind','sourceAssetId','inputSignature'].every(key=>next[key]===previous[key]));
  }
  return false;
}
function reconcileTaskNotices() {
  if(state.lastError===null||taskNoticeResolved(state.lastErrorScope)){state.lastError=null;state.lastErrorScope=null;}
  // Removing from a live HTMLCollection can skip an adjacent expired toast.
  const toasts=Array.from(globalThis.document?.getElementById?.('toast-region')?.children || []);
  for(const toast of toasts)if(taskNoticeResolved(toastScopes.get(toast)))toast.remove();
}
export function notifyError(error, fallback = '操作未完成') {
  notify(error.message || fallback,'error',error.noticeScope);
}
export function notify(message, tone = 'success', scope = null) {
  // A guard can resolve before an asynchronous action's rejection is handled.
  if(taskNoticeResolved(scope))return;
  if(tone==='error'){state.lastError=String(message);state.lastErrorScope=scope;emit();}
  const region = document.getElementById('toast-region'), toast = document.createElement('div');
  toast.className = `toast ${tone}`; toast.textContent = message;
  if(scope){toastScopes.set(toast,scope);toast.dataset.noticeCode=scope.code;}
  // Repeated completion notices should not cover the inspector as a stack.
  for(const previous of Array.from(region.children))if(previous.className===toast.className&&previous.textContent===toast.textContent)previous.remove();
  if(tone==='success') {
    const completed=[...region.children].filter(element=>element.className==='toast success');
    while(completed.length>=2)completed.shift().remove();
  }
  while(region.children.length>=3)([...region.children].find(element=>element.className!=='toast error') || region.firstElementChild).remove();
  region.append(toast);
  setTimeout(() => toast.remove(), tone === 'error' ? 9000 : 3000);
}
export function selectedAsset() { return assetFor(state.project, state.selectedAssetId); }
export function selectedPanel() { return panelFor(state.project, state.selectedPanelId); }
export function selectedPanels() {
  if(!state.selectedPanelId)return [];
  const ids=new Set(state.selectedPanelIds?.includes(state.selectedPanelId)?state.selectedPanelIds:[state.selectedPanelId]);
  const panels=state.project?.panels || [],groups=new Set(panels.filter(panel=>ids.has(panel.id)&&panel.group_id).map(panel=>panel.group_id));
  return panels.filter(panel=>ids.has(panel.id)||panel.group_id&&groups.has(panel.group_id));
}
export function selectedEdit() {
  const panel = selectedPanel(); if (panel && panel.asset_id === state.selectedAssetId) return panel;
  return state.project?.settings.asset_edits[state.selectedAssetId] || defaultEdit(selectedAsset());
}
export function editableSource(project) {
  const panel = panelFor(project, state.selectedPanelId);
  if (panel && panel.asset_id === state.selectedAssetId) return panel;
  const edits = project.settings.asset_edits;
  edits[state.selectedAssetId] ||= defaultEdit(assetFor(project, state.selectedAssetId)); return edits[state.selectedAssetId];
}
export function sourceChannelView() {
  const view=selectedEdit()?.channel_view;
  const source=view?.channels?.[0]?.source || {asset_id:state.selectedAssetId,series:0,frame:0,channel:0};
  return view || state.channelDescriptors.get(descriptorKey(source))?.default_view || null;
}
export function currentRawSource() {
  const view=sourceChannelView();
  return clone(state.sourcePointerIdentity || view?.channels?.find(item=>item.id===view.active_channel)?.source || view?.channels?.[0]?.source || {asset_id:state.selectedAssetId,series:0,frame:0,channel:Number(state.analysisParams.channel || 0)});
}
export function availableAnalysisChannels() {
  const asset=selectedAsset(),view=sourceChannelView();
  const source=view?.channels?.[0]?.source || {asset_id:asset?.id,series:0,frame:0,channel:0};
  return analysisChannelChoices(asset,view,state.channelDescriptors.get(descriptorKey(source)));
}
export function analysisWorkspaceKey() {
  return analysisContextKey(state.selectedAssetId,{channels:availableAnalysisChannels()});
}
export function selectedAnalysisChannels() {
  const options=availableAnalysisChannels(),saved=state.project?.settings.analysis_channel_selections?.[analysisWorkspaceKey()];
  if(saved)return saved.filter(item=>options.some(option=>sourceIdentityKey(option.source)===sourceIdentityKey(item.source))).map(clone);
  const preferred=options.find(item=>item.source.channel===Number(state.analysisParams.channel || 0)) || options[0];
  return preferred?[clone(preferred)]:[];
}
export function analysisRegionKey() {
  if(['ihc','ihc_advanced'].includes(state.analysisParams.method)) {const source=selectedAnalysisChannels()[0]?.source || currentRawSource();return `${source.asset_id}:${source.series}:${source.frame}:rgb`;}
  return selectedAnalysisChannels().map(item=>sourceIdentityKey(item.source)).sort().join('|') || analysisWorkspaceKey();
}
export function analysisGeometry() {
  return state.project?.settings.analysis_regions?.[analysisRegionKey()] || (state.sourceRoi?{roi:state.sourceRoi}:{});
}
export function restoreAnalysisGeometry() {
  state.sourceRoi=clone(state.project?.settings.analysis_regions?.[analysisRegionKey()]?.roi || null);
  state.sourcePolygon=[];
}
export function setAnalysisGeometry(field,geometry) {
  const key=analysisRegionKey();
  mutate(project=>{project.settings.analysis_regions ||= {};const record=project.settings.analysis_regions[key] ||= {};
    if(geometry)record[field]=clone(geometry);else delete record[field];
    if(field==='roi')state.sourceRoi=geometry?clone(geometry):null;
  },field==='line'?'编辑原始采样线':field==='background_roi'?'编辑背景区域':'编辑显微分析区域');
}
export function setAnalysisChannel(id,selected,{single=false}={}) {
  const key=analysisWorkspaceKey(),options=availableAnalysisChannels(),choice=options.find(item=>item.id===id);if(!choice)return;
  let channels=selectedAnalysisChannels().filter(item=>item.id!==id);if(selected)channels.push(clone(choice));if(single)channels=selected?[clone(choice)]:[];
  mutate(project=>{project.settings.analysis_channel_selections ||= {};project.settings.analysis_channel_selections[key]=channels;state.sourceRoi=null;},'选择原始分析通道');
  restoreAnalysisGeometry();emit();
}
export function replaceProject(project, {selectFirst = true} = {}) {
  clearTimeout(saveTimer); state.project = ensureProject(project); state.history = []; state.future = [];
  state.channelDescriptors.clear();state.channelDescriptorErrors.clear();state.filter='';state.assetKind='all';state.importIntent=null;
  state.gelAbort?.abort();state.gelWorker?.terminate();state.gelWorker=null;state.gelAbort=null;state.gelReject=null;state.gelBusy=false;state.gelTaskToken=null;
  state.selectedAssetId = selectFirst ? project.assets[0]?.id || null : state.selectedAssetId;
  state.selectedPanelId = null; state.selectedPanelIds=[]; state.selectedAnnotationId = null; state.selectedRoiId = null; state.sourceRoi = null;state.sourcePan={x:0,y:0};state.sourceZoom=1;
  state.compositionPresetDraft=null;state.compositionInspectorTab=null;
  const migrated=initializeQpcrWorkspaces(state.project,state.selectedAssetId);
  state.dirty = migrated; state.saveState = migrated?'pending':'saved'; state.preflight = null; state.job=null;state.analysisSubmission=null;state.resultRecord=null;state.resultStale=false;state.table = null;state.tableError=null;state.tableLoading=false;state.qpcrSheetNames.clear();state.lastError=null;state.workspaceTask=null;state.gelSuggestions=null;state.markerEstimate=null;
  restoreActiveAnalysisParams();
  state.sourcePointerIdentity=null;state.sourcePolygon=[];state.channelRangeReport=null;if(state.tab==='analysis'&&state.analysisKind==='microscopy')restoreAnalysisGeometry();
  state.mutationSequence++;if(migrated)scheduleSave();emit();
}
export function selectAsset(id, {keepPanel = false} = {}) {
  if(initializeQpcrWorkspaces(state.project,state.selectedAssetId)){state.dirty=true;state.saveState='pending';state.mutationSequence++;scheduleSave();}
  state.selectedAssetId = id; if (!keepPanel) state.selectedPanelId = null;
  state.selectedAnnotationId = null; state.selectedRoiId = null; state.sourceRoi = null; state.table = null; state.tableError=null; state.sourceZoom = 1;state.sourcePan={x:0,y:0};state.gelSuggestions=null;state.markerEstimate=null;state.sourcePointerIdentity=null;state.sourcePolygon=[];
  if(state.tab==='analysis'&&state.analysisKind==='microscopy')restoreAnalysisGeometry();
  emit();
}
export function selectPanel(id, {toggle=false, preserve=false}={}) {
  const target=panelFor(state.project,id);if(!target)return;
  const group=target.group_id?state.project.panels.filter(panel=>panel.group_id===target.group_id).map(panel=>panel.id):[id];
  const ids=new Set(selectedPanels().map(panel=>panel.id));
  if(toggle){if(group.every(item=>ids.has(item)))group.forEach(item=>ids.delete(item));else group.forEach(item=>ids.add(item));}
  else if(!preserve || !ids.has(id)){ids.clear();group.forEach(item=>ids.add(item));}
  state.selectedPanelIds=[...ids];state.selectedPanelId=ids.has(id)?id:state.selectedPanelIds.at(-1) || null;
  state.compositionInspectorTab='selection';
  state.selectedAnnotationId = null;state.sourcePointerIdentity=null;state.selectedRoiId=null;
  const panel = selectedPanel(); if (panel) state.selectedAssetId = panel.asset_id;
  emit();
}
export function scheduleSave() {
  clearTimeout(saveTimer); saveTimer = setTimeout(() => persistProject().catch(() => {}), 650);
}
/** Undo stores editable recipes only. Large source arrays and completed results stay shared. */
export function captureRecipe(project=state.project) {
  return {title:project.title,figure:clone(project.figure),panels:clone(project.panels),annotations:clone(project.annotations),settings:clone(project.settings)};
}
export function restoreRecipe(recipe) {
  Object.assign(state.project,clone(recipe));
  ensureProject(state.project);
  restoreActiveAnalysisParams();
  state.sourcePointerIdentity=null;if(state.tab==='analysis'&&state.analysisKind==='microscopy')restoreAnalysisGeometry();
}
export function changed() {
  reconcilePanelLabels(state.project);
  restoreActiveAnalysisParams();
  state.mutationSequence++; state.dirty = true; state.saveState = 'pending'; state.preflight = null; scheduleSave(); emit();
}
export function mutate(change, label = '编辑项目') {
  if (!state.project) return;
  if(state.workspaceStatus?.has_recovery||state.workspaceStatus?.cleanup_pending)throw new Error('请先处理本机工作区，再进行编辑。');
  const before=captureRecipe();
  try {change(state.project);reconcilePanelLabels(state.project);}
  catch(error){restoreRecipe(before);throw error;}
  state.history.push({recipe:before,label}); if (state.history.length > 60) state.history.shift();
  state.future = []; changed();
}
export function commitGesture(before, label = '调整位置') {
  if (JSON.stringify(before) === JSON.stringify(captureRecipe())) return;
  reconcilePanelLabels(state.project);
  state.history.push({recipe:before,label}); if (state.history.length > 60) state.history.shift(); state.future = []; changed();
}
export function undo() {
  if (!state.history.length) return;
  const entry = state.history.pop();
  state.future.push({recipe:captureRecipe(),label:entry.label});restoreRecipe(entry.recipe);
  changed();
}
export function redo() {
  if (!state.future.length) return;
  const entry = state.future.pop();
  state.history.push({recipe:captureRecipe(),label:entry.label});restoreRecipe(entry.recipe);changed();
}
export async function persistProject() {
  clearTimeout(saveTimer); if (!state.project || !state.dirty) return state.project;
  // Pointer gestures update live geometry for preview. Persist only after the
  // gesture has committed or restored its original recipe, never a mid-drag draft.
  if(state.interactionLock){await new Promise(resolve=>interactionWaiters.push(resolve));return persistProject();}
  if (savePromise) { await savePromise; if (state.dirty) return persistProject(); return state.project; }
  const sequence = state.mutationSequence, projectId = state.project.id;
  // request() serializes synchronously; immutable assets/results need no deep copy.
  const snapshot={...state.project,...captureRecipe()};
  state.saveState = 'saving'; emit();
  savePromise = request('/api/project', {method: 'PUT', body: snapshot});
  try {
    const saved = await savePromise;
    if (state.project?.id === projectId) {
      state.project.revision = saved.revision; state.project.updated_at = saved.updated_at;
      if (sequence === state.mutationSequence) {state.dirty = false; state.saveState = 'saved';}
      else {state.saveState = 'pending'; scheduleSave();}
    }
    return state.project;
  } catch (error) {
    state.saveState = 'error'; notify(`保存失败：${error.message}`, 'error'); throw error;
  } finally {savePromise = null; emit();}
}
export async function flushSave() { while (state.dirty || savePromise) {await persistProject();} }
/** A confirmed cleanup discards edits; wait for in-flight writes without saving a new copy. */
export async function settleWorkspaceWrites() {
  clearTimeout(saveTimer);
  if(savePromise)await savePromise.catch(()=>{});
  clearTimeout(saveTimer);
}
export async function withBusy(label, operation, {cancellable=true}={}) {
  if (state.busy) return;
  state.lastError=null;state.busy = label; state.busyController = cancellable?new AbortController():null; emit();
  try {return await operation(state.busyController?.signal);}
  catch (error) {if (error.name === 'AbortError') notify('操作已取消', 'info'); else notify(error.message || '操作未完成，请重试', 'error');}
  finally {state.busy = null; state.busyController = null; emit();}
}

