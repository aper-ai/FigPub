import {state,emit,notify,notifyError,mutate,selectedAsset,selectedPanel,selectedPanels,selectedEdit,editableSource,replaceProject,selectAsset,selectPanel,undo,redo,persistProject,flushSave,settleWorkspaceWrites,withBusy,currentRawSource,availableAnalysisChannels,selectedAnalysisChannels,analysisWorkspaceKey,setAnalysisChannel,setAnalysisGeometry,restoreAnalysisGeometry,analysisSelection} from './state.js';
import {request,importAssets,openProject,exportFile,saveBlob,downloadOriginal,downloadMessage} from './api.js';
import {clone,uid,number,clamp,round,fullCrop,emptyDisplay,panelFromAsset,ensureProject,updateScaleBar,scaleBarGeometry,isAnalysisChartType} from './domain.js';
import {loadTable,runAnalysis,cancelJob,retryJobStatus,downloadResults,plotResults,loadCellposeStatus,clearTableWorkspace} from './analysis.js';
import {detectGel,cancelGel,evenlySpaceLanes} from './gel.js';
import {bindGestures,finishPolygon} from './gestures.js';
import {enterChannels,changeChannelField,changeChannelMode,changeSplitSetting,changeSourcePlane,resetChannelView,autoRangeChannels,openChannelComposition,addCompositionSource,editCompositionSource,confirmChannelComposition,addChannelPanels,loadChannelDescriptor,currentSourceIdentity,currentChannelView,channelSourceDimensions,channelPreviews,clearChannelWorkspace} from './channels.js';
import {markInputDraft,beginInputCommit,holdPointerDraft,releasePointerDraft,releasePointerDraftSoon,discardInputDraft} from './input-lifecycle.js';
import {openWbWorkflow,handleWbAction,handleWbInput} from './wb-workflow.js';
import {advancedActions,handleAdvancedChange} from './histology-fibers.js';
import {bindInstanceReview,clearInstanceReviewSessions} from './instance-review.js';
import {compositionActions,changeCompositionOption,compositionIssues,addAssetsToComposition,arrangeComposition,resizeComposition,alignComposition,duplicateComposition,groupComposition,selectAllComposition} from './composition.js';
import {requestComposerFit} from './composer-viewport.js';
import {taskById,taskForWorkspace,acceptsTaskAsset,resolveTaskImport,preferredResumeTab,hasProjectContent} from './workflows.js';
import {qpcrParameterFields,editableQpcrParameters,selectQpcrWorksheet,qpcrTableMatches} from './qpcr-workspace.js';
import {readNumericInput} from './numeric-input.js';
import {exportSizeIssue} from './export-options.js';

function editQpcrParameters(change,label) {
  const asset=selectedAsset();if(asset?.kind!=='table')throw new Error('请先选择 Ct 数据表');
  mutate(project=>change(editableQpcrParameters(project,asset.id)),label);
}
function changeHistory(change) {
  change();
  if(state.tab==='analysis'&&state.analysisKind==='qpcr'&&selectedAsset()?.kind==='table'&&!qpcrTableMatches(state.table,state.selectedAssetId,state.analysisParams))return loadTable();
}

function assertWorkspaceReady() {
  if(state.workspaceStatus?.cleanup_pending)throw new Error('本机数据尚未清理完成，请先重试清理。');
  if(state.workspaceStatus?.has_recovery)throw new Error('请先恢复上次工作区，或明确新建、清除本机工作区。');
}
function closeModal() {
  state.modal=state.workspaceStatus?.cleanup_pending?{type:'workspace-cleanup'}:state.workspaceStatus?.has_recovery?{type:'workspace-recovery'}:null;emit();
}
function replaceWorkspace(project) {
  clearChannelWorkspace();clearInstanceReviewSessions();clearTableWorkspace();replaceProject(project);
  state.workspaceStatus={has_recovery:false,cleanup_pending:false};state.lastWorkspaceTab=null;
}
async function restoreWorkspace() {
  assertWorkspaceCanSwitch();
  await withBusy('正在恢复本机工作区…',async()=>{
    const project=await request('/api/workspace/restore',{method:'POST'});replaceWorkspace(project);
    state.modal=null;state.tab='home';emit();notify('已根据你的选择恢复本机工作区。');
  },{cancellable:false});
}
async function clearLocalWorkspace() {
  assertWorkspaceCanSwitch();
  await withBusy('正在清除本机工作区副本…',async()=>{
    await settleWorkspaceWrites();
    let result;
    try{result=await request('/api/workspace/clear',{method:'POST',body:{confirm:true}});}
    catch(error){
      try{state.workspaceStatus=await request('/api/workspace/status');}catch{}
      if(state.workspaceStatus?.cleanup_pending){
        try{const empty=await request('/api/project');clearChannelWorkspace();clearInstanceReviewSessions();clearTableWorkspace();replaceProject(empty);}catch{}
        state.modal={type:'workspace-cleanup'};
      }
      emit();throw error;
    }
    if(result?.cleared!==true||!result.project)throw new Error('未取得清理完成确认，请重试。');
    replaceWorkspace(result.project);state.tab='home';
    state.modal=result.requires_restart?{type:'workspace-cleanup-exit'}:null;emit();
    notify(result.requires_restart?'工作区副本已删除；请完成对话框中的预览缓存清理步骤。':'软件保存的工作区副本已清除。',result.requires_restart?'info':'success');
  },{cancellable:false});
}

function requestImport(task=null) {
  assertWorkspaceReady();
  const intent=taskById(task)?.id || taskForWorkspace(state.tab,state.analysisKind);
  if(intent==='wb'){state.importIntent='wb';document.getElementById('import-wb-files').click();return;}
  state.importIntent=intent;
  const input=document.getElementById('import-files');
  input.accept=intent==='qpcr'?'.csv,.tsv,.txt,.xlsx':intent==='microscopy'?'.tif,.tiff,.png,.jpg,.jpeg,.bmp,.webp':'.tif,.tiff,.png,.jpg,.jpeg,.bmp,.webp,.pdf,.svg,.csv,.tsv,.txt,.xlsx';
  input.click();
}

async function startTask(id) {
  const task=taskById(id);if(!task)return;
  state.modal=null;state.workspaceTask=id;state.showGuide=true;
  const asset=acceptsTaskAsset(id,selectedAsset())?selectedAsset():state.project.assets.find(item=>acceptsTaskAsset(id,item));
  const route=resolveTaskImport(id,asset?[asset]:[]);
  state.tab=route.tab;if(route.analysisKind)state.analysisKind=route.analysisKind;
  selectAsset(asset?.id || null);emit();
  if(!asset){if(id==='compose'&&(state.project.panels.length||state.project.annotations.length))return;requestImport(id);return;}
  if(id==='qpcr')await loadTable();
  if(id==='microscopy'){restoreAnalysisGeometry();if(state.analysisParams.method==='cellpose')loadCellposeStatus();}
}

function showRunningAnalysis() {
  const task=state.analysisSubmission || state.job;if(!task)return;
  state.tab='analysis';state.analysisKind=task.kind || state.analysisKind;
  // Source selection clears the former table and geometry before rendering the
  // task source. Merely changing its ID would relabel the old table's rows.
  if(task.sourceAssetId&&state.selectedAssetId!==task.sourceAssetId)selectAsset(task.sourceAssetId);
  if(state.analysisKind==='qpcr'&&selectedAsset()?.kind==='table'&&!qpcrTableMatches(state.table,state.selectedAssetId,state.analysisParams))return loadTable();
  if(state.analysisKind==='microscopy')restoreAnalysisGeometry();
  emit();
}

async function importSelectedFiles(files,{wb=false,compose=false,task=null}={}) {
  if(!files.length)return;
  assertWorkspaceReady();
  if(state.busy){notify('请等当前操作完成后再导入文件','info');return;}
  const intent=wb?'wb':compose?'compose':task || taskForWorkspace(state.tab,state.analysisKind);
  let imported=false;
  await withBusy(`正在导入 ${files.length} 份素材…`,async()=>{
    await flushSave(); const result=await importAssets(files),fresh=await request('/api/project');
    const added=result.assets || [],route=resolveTaskImport(intent,added);
    state.project=ensureProject(fresh);state.selectedAssetId=route.assetId;state.selectedPanelId=null;state.selectedAnnotationId=null;state.selectedRoiId=null;state.sourcePointerIdentity=null;state.sourcePolygon=[];state.sourceRoi=null;state.preflight=null;state.table=null;state.tableError=null;
    state.history=[];state.future=[];state.tab=route.tab;state.workspaceTask=route.task;if(route.analysisKind)state.analysisKind=route.analysisKind;
    state.dirty=false;state.saveState='saved';state.mutationSequence++;state.modal=null;imported=true;emit();
    notify(`已导入 ${added.length || files.length} 份素材`);
    if(route.task==='compose'){
      try{addAssetsToComposition(route.compositionAssetIds,{layout:state.project.settings.composition.auto_import!==false});}catch(error){emit();notify(`素材已保留在素材库，自动排版未应用：${error.message}`,'error');}
      if(added.some(asset=>asset.kind==='table'))notify('数据表已保留在素材库；请先分析并生成图表，再加入组图','info');
    } else if(route.task&& !route.assetId)notify(route.task==='qpcr'?'当前任务需要 Ct 数据表。已导入的其他文件保留在素材库。':'当前任务需要原始图像。PDF、SVG 和数据表可在素材库或组图中使用。','info');
    else if(selectedAsset()?.kind==='table')await loadTable();
    else if(route.task==='microscopy')restoreAnalysisGeometry();
  },{cancellable:false});
  if(intent==='wb'&&imported&&selectedAsset()?.kind==='image')await openWbWorkflow({autoDetect:true});
}

function assertWorkspaceCanSwitch() {
  if(state.analysisSubmission)throw Object.assign(new Error('分析任务正在提交，请等待提交完成后再切换项目。'),
    {noticeScope:{code:'analysis-switch-submission',projectId:state.project.id,submission:state.analysisSubmission}});
  if(state.wbWorking)throw Object.assign(new Error('WB 图组仍在生成，请先等待完成，再切换项目。'),
    {noticeScope:{code:'workspace-switch-wb',projectId:state.project.id,taskToken:state.wbTaskToken}});
  if(state.job&&['queued','running'].includes(state.job.status))throw Object.assign(new Error(state.job.pollingError?
    '分析任务状态尚未确认，请先重试查询或取消任务，再切换项目。':'分析仍在运行，请先等待完成或取消任务，再切换项目。'),
    {noticeScope:{code:'analysis-switch-job',projectId:state.project.id,jobId:state.job.id,statusUnknown:Boolean(state.job.pollingError)}});
  if(state.gelBusy)throw Object.assign(new Error('条带识别仍在运行，请先等待完成或取消识别，再切换项目。'),
    {noticeScope:{code:'workspace-switch-gel',projectId:state.project.id,taskToken:state.gelTaskToken}});
}

async function importProjectFile(file,{confirmed=false}={}) {
  if(!file)return;
  assertWorkspaceReady();
  assertWorkspaceCanSwitch();
  if(!confirmed&&hasProjectContent(state.project)){state.modal={type:'switch-project',file};emit();return;}
  await withBusy('正在校验并恢复项目…',async()=>{await flushSave();const project=await openProject(file);replaceWorkspace(project);state.tab=preferredResumeTab(project);const warnings=project.settings?.migration_warnings || [];state.modal=warnings.length?{type:'migration',warnings}:null;emit();notify(warnings.length?`项目已导入，${warnings.length} 项迁移提示需要核对`:'项目已恢复，原件与编辑状态已载入',warnings.length?'info':'success');if(state.tab==='source'&&selectedAsset()?.kind==='table')await loadTable();},{cancellable:false});
}

async function saveProjectCopy({keepModal=false}={}) {
  const previous=keepModal?{...state.modal}:null;
  if(previous?.type==='new')previous.titleDraft=document.getElementById('new-project-title')?.value || previous.titleDraft;
  return withBusy('正在准备项目文件…',async signal=>{
    await flushSave();const result=await exportFile({project:state.project,format:'project'},signal);
    state.modal=previous;emit();notify(downloadMessage(result)+(keepModal?(result.delivery==='saved'?'。可以继续切换项目。':'。请确认副本已保存，再继续切换。'):''));
  });
}

async function runPreflight() {
  await withBusy('正在检查图稿…',async signal=>{state.preflight=await request('/api/preflight',{method:'POST',body:{project:state.project,min_ppi:state.project.settings.min_ppi || 300},signal});state.preflight.issues.push(...compositionIssues(state.project));emit();if(!state.modal)notify(state.preflight.issues?.length?`检查完成：${state.preflight.issues.length} 项提示`:'当前检查项通过',state.preflight.issues?.length?'info':'success');});
}

function addPanel() {
  const asset=selectedAsset();if(!asset || asset.kind==='table'){notify('请先选择图片、PDF 或 SVG 素材','info');return;}
  if(asset.kind==='image'&&(state.tab==='channels'||selectedEdit().channel_view))return addChannelPanels();
  if(selectedPanel() && selectedPanel().asset_id===asset.id && !state.sourceRoi){state.tab='compose';emit();return;}
  mutate(project=>{const edit=clone(selectedEdit());if(state.sourceRoi)edit.crop=clone(state.sourceRoi);const panel=panelFromAsset(project,asset,edit);project.panels.push(panel);state.selectedPanelId=panel.id;state.selectedAnnotationId=null;},'添加面板');
  state.sourceRoi=null;state.tab='compose';emit();
}

function addRoi() {
  const asset=selectedAsset(),roi=state.sourceRoi;if(!asset || !roi){notify('请先框选区域','info');return;}
  const source=currentRawSource(),entry={...clone(roi),id:uid('roi'),asset_id:source.asset_id,source,sample_id:'',group:'',biological_replicate:'',technical_replicate:'',exposure_id:`${source.asset_id}:S${source.series}:F${source.frame}`,role:'target',protein:''};
  if(state.analysisParams.wb_draft_scope_id)entry.wb_draft_id=state.analysisParams.wb_draft_scope_id;
  mutate(project=>{project.settings.analysis_rois.push(entry);},'添加测量区域');state.sourceRoi=null;state.selectedRoiId=entry.id;state.sourceTool='select';emit();
}

function deleteSelection() {
  if(state.selectedPanelId){const ids=new Set(selectedPanels().map(panel=>panel.id));mutate(project=>{project.panels=project.panels.filter(panel=>!ids.has(panel.id));},'删除选中面板');state.selectedPanelId=null;state.selectedPanelIds=[];}
  else if(state.selectedAnnotationId){const id=state.selectedAnnotationId;mutate(project=>{project.annotations=project.annotations.filter(item=>item.id!==id);},'删除标注');state.selectedAnnotationId=null;}
  emit();
}

function arrangePanels() {
  arrangeComposition();
}

function alignPanel(direction) {
  alignComposition(direction);
}

function addAnnotation(type) {
  const annotation={id:uid('annotation'),type,x_mm:12,y_mm:12,width_mm:type==='line'?25:28,height_mm:type==='rect'?16:type==='line'?0:6,text:type==='text'?'添加文字':'',font_size_pt:8,color:'#222222'};
  mutate(project=>project.annotations.push(annotation),'添加标注');state.selectedAnnotationId=annotation.id;state.selectedPanelId=null;state.tab='compose';state.compositionInspectorTab='selection';emit();
}

function switchAnalysisKind(kind) {
  state.analysisKind=kind;state.workspaceTask=kind;
  const asset=selectedAsset();if(kind==='qpcr' && asset?.kind!=='table')state.selectedAssetId=state.project.assets.find(item=>item.kind==='table')?.id || null;
  if(kind!=='qpcr' && asset?.kind!=='image')state.selectedAssetId=state.project.assets.find(item=>item.kind==='image')?.id || null;
  state.selectedPanelId=null;state.selectedRoiId=null;state.sourceRoi=null;state.sourcePointerIdentity=null;state.table=null;state.tableError=null;state.sourcePolygon=[];state.analysisRegionTarget='roi';
  if(kind==='microscopy'){restoreAnalysisGeometry();if(state.analysisParams.method==='cellpose')loadCellposeStatus();}emit();if(kind==='qpcr'&&selectedAsset())loadTable();
}

function viewAnalysisResults(element) {
  const selection=analysisSelection(),id=selection.record?.id;
  if(state.tab!=='analysis'||!selection.result||element.dataset.measurementId!==id)return;
  const result=document.getElementById('analysis-results'),heading=document.getElementById('analysis-results-title');
  const workspace=result?.closest('.analysis-workspace');
  if(!workspace||!heading||result.dataset.measurementId!==id)return;
  // Navigation is explicit and local to the analysis pane. Completing a job or
  // refreshing its status never scrolls or moves focus to the result.
  const top=workspace.scrollTop+result.getBoundingClientRect().top-workspace.getBoundingClientRect().top-12;
  workspace.scrollTo({top:Math.max(0,top),behavior:'auto'});
  heading.focus({preventScroll:true});
}

const actions={
  ...advancedActions,
  ...compositionActions,
  'home':()=>{if(state.tab!=='home')state.lastWorkspaceTab=state.tab;state.tab='home';state.modal=null;emit();},
  'start-task':element=>startTask(element.dataset.task),
  'resume-workspace':()=>{state.tab=state.lastWorkspaceTab&&state.lastWorkspaceTab!=='home'?state.lastWorkspaceTab:preferredResumeTab(state.project);emit();if((state.tab==='source'||state.tab==='analysis'&&state.analysisKind==='qpcr')&&selectedAsset()?.kind==='table')return loadTable();},
  'toggle-guide':()=>{state.showGuide=state.showGuide===false;emit();},
  'dismiss-error':()=>{state.lastError=null;emit();},
  'retry-save':()=>flushSave().then(()=>{state.lastError=null;emit();notify('已保存到本机工作区');}),
  'focus-analysis-settings':()=>{const field=document.getElementById(state.analysisKind==='qpcr'?'analysis-reference_gene':'analysis-method');field?.focus();field?.scrollIntoView?.({block:'center',behavior:'smooth'});},
  'add-all-panels':()=>addAssetsToComposition(state.project.assets.filter(asset=>asset.kind!=='table').map(asset=>asset.id),{layout:true}),
  'show-running-analysis':showRunningAnalysis,
  'backup-before-switch':()=>saveProjectCopy({keepModal:true}),
  'confirm-project-switch':()=>importProjectFile(state.modal?.file,{confirmed:true}),
  'tab':element=>{const tab=element.dataset.tab;if(tab==='home')return actions.home();state.sourceRoi=null;state.sourcePolygon=[];state.tab=tab;state.workspaceTask=taskForWorkspace(tab,state.analysisKind);if(state.tab==='channels')return enterChannels();if(state.tab==='gel'&&selectedAsset()?.kind!=='image')state.selectedAssetId=state.project.assets.find(asset=>asset.kind==='image')?.id || null;if(state.tab==='analysis'){const asset=selectedAsset();if(state.analysisKind==='qpcr'&&asset?.kind!=='table'||state.analysisKind!=='qpcr'&&asset?.kind!=='image'){switchAnalysisKind(state.analysisKind);return;}if(state.analysisKind==='microscopy')restoreAnalysisGeometry();}emit();if((state.tab==='source'||state.tab==='analysis'&&state.analysisKind==='qpcr')&&selectedAsset()?.kind==='table')return loadTable();},
  'import':element=>requestImport(element?.dataset?.task),
  'import-wb':()=>requestImport('wb'),
  'open-project':()=>document.getElementById('open-project').click(),
  'project-menu':()=>{state.modal={type:'project'};emit();},
  'new-dialog':()=>{assertWorkspaceCanSwitch();state.modal={type:'new'};emit();},
  'create-project':async()=>{assertWorkspaceCanSwitch();const title=document.getElementById('new-project-title').value.trim() || '未命名 Figure';await withBusy('正在创建项目…',async()=>{if(!state.workspaceStatus?.has_recovery)await flushSave();replaceWorkspace(await request('/api/project/new',{method:'POST',body:{title}}));state.tab='home';state.lastWorkspaceTab=null;state.modal=null;emit();},{cancellable:false});},
  'restore-workspace':restoreWorkspace,
  'clear-workspace-dialog':()=>{assertWorkspaceCanSwitch();state.modal={type:state.workspaceStatus?.cleanup_pending?'workspace-cleanup':'clear-workspace'};emit();},
  'clear-workspace':clearLocalWorkspace,
  'undo':()=>changeHistory(undo),'redo':()=>changeHistory(redo),
  'save-project':()=>saveProjectCopy(),
  'export-dialog':()=>{state.modal={type:'export',format:'pdf',dpi:300,outlined:false};emit();},
  'export-file':()=>{const options=clone(state.modal),sizeIssue=exportSizeIssue(options.format,state.project.figure);if(sizeIssue){notify(sizeIssue,'info');return;}if(options.format!=='sources'&&!state.project.panels.length&&!state.project.annotations.length){notify('请先将图片、图表或标注加入画布，再导出图稿。','info');return;}return withBusy('正在生成并保存导出文件…',async signal=>{await flushSave();const result=await exportFile({project:state.project,format:options.format,dpi:options.dpi,outlined_text:options.outlined},signal);state.modal=null;emit();notify(downloadMessage(result));});},
  'close-modal':closeModal,
  'cancel-busy':()=>state.busyController?.abort(),
  'help':()=>{state.modal={type:'help'};emit();},
  'select-asset':element=>{selectAsset(element.dataset.id);const asset=selectedAsset();if(state.tab==='compose')state.tab='source';if(['gel','channels','analysis'].includes(state.tab)&&asset?.kind!=='image'&&asset?.kind!=='table')state.tab='source';if(state.tab==='analysis'&&asset?.kind==='table')state.analysisKind='qpcr';if(state.tab==='analysis'&&asset?.kind==='image'&&state.analysisKind==='qpcr')state.tab='source';state.workspaceTask=taskForWorkspace(state.tab,state.analysisKind);emit();if(asset?.kind==='table')return loadTable();if(state.tab==='channels')return enterChannels();},
  'asset-filter':element=>{state.assetKind=element.dataset.kind;emit();},
  'download-original':()=>{const asset=selectedAsset();if(!asset)return;return withBusy('正在保存原始文件…',async signal=>{const result=await downloadOriginal(asset.id,asset.name,{signal});notify(downloadMessage(result));});},
  'add-panel':addPanel,
  'source-tool':element=>{state.sourceTool=element.dataset.tool;state.sourcePolygon=[];emit();},
  'clear-roi':()=>{if(state.tab==='analysis'&&state.analysisKind==='microscopy')setAnalysisGeometry('roi',null);else state.sourceRoi=null;state.sourceTool='select';emit();},
  'apply-crop':()=>{if(!state.sourceRoi)return;const crop=clone(state.sourceRoi);mutate(project=>{editableSource(project).crop=crop;},'裁切源区域');state.sourceRoi=null;state.sourceTool='select';emit();},
  'reset-crop':()=>{const asset=selectedAsset();if(asset)mutate(project=>{editableSource(project).crop=fullCrop(channelSourceDimensions(asset));},'恢复完整图像');},
  'reset-display':()=>mutate(project=>{editableSource(project).display=emptyDisplay();},'重置显示'),
  'zoom-in':()=>{const key=state.tab==='compose'?'figureZoom':'sourceZoom';state[key]=clamp(round(state[key]+.2,1),.2,4);emit();},
  'zoom-out':()=>{const key=state.tab==='compose'?'figureZoom':'sourceZoom';state[key]=clamp(round(state[key]-.2,1),.2,4);emit();},
  'zoom-fit':()=>{if(state.tab==='compose')requestComposerFit();else{state.sourceZoom=1;state.sourcePan={x:0,y:0};}emit();},
  'gel-mode':element=>{state.gelMode=element.dataset.mode;mutate(project=>{if(selectedAsset())editableSource(project).gel.marker_unit=state.gelMode==='dna'?'bp':'kDa';},'选择凝胶类型');},
  'detect-gel':detectGel,'cancel-gel':cancelGel,'even-lanes':evenlySpaceLanes,
  'add-lane':()=>mutate(project=>{const edit=editableSource(project),centers=edit.gel.lane_centers;centers.push(edit.crop.x+edit.crop.width/2);edit.gel.lane_names.push('');},'添加泳道线'),
  'delete-lane':element=>mutate(project=>{const gel=editableSource(project).gel,index=Number(element.dataset.index);gel.lane_centers.splice(index,1);gel.lane_names.splice(index,1);gel.hidden_lanes=[];},'移除泳道标注'),
  'delete-marker':element=>mutate(project=>{editableSource(project).gel.marker_points.splice(Number(element.dataset.index),1);},'删除 Marker 校准点'),
  'confirm-marker':()=>{const size=Number(document.getElementById('new-marker-size').value),y=Number(document.getElementById('new-marker-y').value);if(!Number.isFinite(size)||size<=0||!Number.isFinite(y)){notify('请填写有效的已知大小和 Y 坐标','info');return;}mutate(project=>{const points=editableSource(project).gel.marker_points;points.push({y:clamp(y,0,selectedAsset().height_px),size,manual:true});points.sort((a,b)=>a.y-b.y);},'添加 Marker 校准');state.modal=null;state.sourceTool='select';emit();},
  'marker-candidate':element=>{const candidate=state.gelSuggestions?.marker_candidates?.[Number(element.dataset.index)];if(candidate){state.modal={type:'marker-candidate',candidate};emit();}},
  'confirm-candidate':()=>{const candidate=clone(state.modal.candidate);mutate(project=>{const gel=editableSource(project).gel;gel.marker_points=candidate.points.map(point=>({y:point.y,size:point.size,manual:false,confirmed:true}));gel.marker_unit=candidate.unit;gel.marker_ladder_id=candidate.ladder_id;gel.marker_lane_index=candidate.lane_index;},'确认 Marker 型号与条带');state.modal=null;emit();},
  'select-panel':(element,event)=>{selectPanel(element.dataset.id,{toggle:event.shiftKey||event.ctrlKey||event.metaKey});state.tab='compose';emit();},
  'select-annotation':element=>{state.selectedAnnotationId=element.dataset.id;state.selectedPanelId=null;emit();},
  'figure-tool':element=>{state.figureTool=element.dataset.tool;emit();},
  'add-annotation':element=>addAnnotation(element.dataset.type),
  'toggle-snap':()=>{state.snap=!state.snap;emit();},
  'figure-preset':element=>resizeComposition(Number(element.dataset.width),state.project.figure.height_mm),
  'auto-layout':arrangePanels,
  'delete-selection':deleteSelection,
  'align-panel':element=>alignPanel(element.dataset.align),
  'edit-panel-source':()=>{if(!selectedPanel())return;state.tab=selectedPanel().gel?.lane_centers?.length?'gel':'source';state.sourceRoi=null;emit();},
  'duplicate-panel':duplicateComposition,
  'flip-panel':element=>{const panel=selectedPanel();if(panel)mutate(project=>{const current=project.panels.find(item=>item.id===panel.id),key=element.dataset.direction==='h'?'flip_h':'flip_v';current[key]=!current[key];},'翻转面板');},
  'preflight':runPreflight,
  'locate-issue':element=>{const issue=state.preflight?.issues?.[Number(element.dataset.index)];if(issue?.panel_id){state.selectedPanelId=issue.panel_id;state.selectedAssetId=state.project.panels.find(panel=>panel.id===issue.panel_id)?.asset_id;state.tab='compose';}else if(issue?.asset_id){selectAsset(issue.asset_id);state.tab='source';}emit();},
  'analysis-kind':element=>switchAnalysisKind(element.dataset.kind),
  'enter-channels':enterChannels,
  'channel-mode':element=>changeChannelMode(element.dataset.mode),
  'channel-single':element=>changeChannelMode('single',element.dataset.id),
  'auto-range-channels':()=>autoRangeChannels(),
  'auto-range-channel':element=>autoRangeChannels(element.dataset.id),
  'reset-channels':resetChannelView,
  'retry-channels':()=>loadChannelDescriptor(currentSourceIdentity(),{retry:true}),
  'retry-channel-previews':()=>{channelPreviews.retry();emit();},
  'compose-channels':openChannelComposition,
  'add-composition-source':addCompositionSource,
  'remove-composition-source':element=>{state.modal.entries.splice(Number(element.dataset.index),1);emit();},
  'confirm-channel-composition':confirmChannelComposition,
  'add-channel-panels':addChannelPanels,
  'analysis-region-target':element=>{state.analysisRegionTarget=element.dataset.target;state.sourceTool='roi';state.sourcePolygon=[];emit();},
  'clear-background-roi':()=>setAnalysisGeometry('background_roi',null),
  'clear-analysis-line':()=>setAnalysisGeometry('line',null),
  'finish-polygon':finishPolygon,
  'refresh-cellpose':()=>loadCellposeStatus(true),
  'analyze-table':()=>{state.tab='analysis';switchAnalysisKind('qpcr');},
  'load-table':loadTable,'run-analysis':runAnalysis,'cancel-job':cancelJob,'retry-job-status':retryJobStatus,
  'view-analysis-results':viewAnalysisResults,
  'add-roi':addRoi,
  'select-roi':async element=>{const roi=state.project.settings.analysis_rois.find(item=>item.id===element.dataset.id);if(roi){state.selectedRoiId=roi.id;state.selectedAssetId=roi.source?.asset_id || roi.asset_id;state.selectedPanelId=null;state.sourcePointerIdentity=clone(roi.source || {asset_id:roi.asset_id,series:0,frame:0,channel:0});state.sourceTool='select';state.sourceRoi=null;emit();const descriptor=await loadChannelDescriptor(state.sourcePointerIdentity);const active=descriptor.default_view.channels.find(item=>item.source.channel===state.sourcePointerIdentity.channel);if(active)mutate(project=>{editableSource(project).channel_view={...clone(descriptor.default_view),mode:'single',active_channel:active.id};},'定位测量原始平面');}},
  'delete-roi':element=>{const id=element.dataset.id;mutate(project=>{project.settings.analysis_rois=project.settings.analysis_rois.filter(item=>item.id!==id);for(const roi of project.settings.analysis_rois)if(roi.control_roi_id===id)delete roi.control_roi_id;},'删除测量区域');if(state.selectedRoiId===id)state.selectedRoiId=null;emit();},
  'download-results':downloadResults,'plot-results':plotResults,
  'reset-wb-exposure-selection':()=>mutate(project=>{project.settings.analysis_params={...state.analysisParams,selected_exposures:{}};},'清除旧曝光选择'),
  'toggle-micro-overlay':()=>{state.showMicroOverlay=state.showMicroOverlay===false;emit();},
  'qpcr-template':()=>withBusy('正在保存 Ct 表模板…',async signal=>{const result=await saveBlob(new Blob(['\ufeffsample,target,ct,group,biological_replicate,technical_replicate\r\n'],{type:'text/csv;charset=utf-8'}),'qpcr_template.csv',{signal});notify(downloadMessage(result));})
};

function handleField(element) {
  const {field,scope}=element.dataset;if(!field || !scope)return;
  let value=element.type==='checkbox'?element.checked:element.type==='number'?readNumericInput(element):element.value;
  if(element.type==='number' && value!==null && !Number.isFinite(value)){notify('请输入有效数字','info');return;}
  if(element.type==='number' && element.dataset.layoutValue!==undefined && value===Number(element.dataset.layoutValue))return;
  if(scope==='crop') {
    const asset=channelSourceDimensions(selectedAsset());if(!selectedAsset())return;
    mutate(project=>{const crop=editableSource(project).crop;crop[field]=Math.round(value??(field==='width'||field==='height'?1:0));crop.x=clamp(crop.x,0,asset.width_px-1);crop.y=clamp(crop.y,0,asset.height_px-1);crop.width=clamp(crop.width,1,asset.width_px-crop.x);crop.height=clamp(crop.height,1,asset.height_px-crop.y);},'调整裁切坐标');return;
  }
  if(scope==='display') {
    const current=selectedEdit().display || {},next={...current,[field]:value};if(next.low!=null&&next.high!=null&&next.low>=next.high){notify('白点必须大于黑点','info');emit();return;}
    mutate(project=>{editableSource(project).display[field]=value;},'调整图像显示');return;
  }
  if(scope==='scale-bar') {
    const panel=selectedPanel(),asset=selectedAsset();if(!panel||asset?.kind!=='image'||!['um_per_px','length_um','color'].includes(field))return;
    if((field!=='color'&&value!==null&&value<=0)||(field==='color'&&!/^#[0-9a-f]{6}$/i.test(value))){notify('请输入大于 0 的标定尺度或长度，并选择有效颜色','info');emit();return;}
    const wasEnabled=Boolean(panel.scale_bar);
    mutate(project=>{updateScaleBar(project.panels.find(item=>item.id===panel.id),asset,{[field]:value});},'编辑比例尺');
    if(wasEnabled&&!selectedPanel().scale_bar)notify('比例尺已关闭；填写有效标定与长度后可重新显示','info');return;
  }
  if(scope==='gel'){if(field==='font_size_pt')value=clamp(value||7,1,144);mutate(project=>{editableSource(project).gel[field]=value;},'调整凝胶标注');return;}
  if(scope==='figure'){if(value===null)return;resizeComposition(field==='width_mm'?clamp(value,20,2000):state.project.figure.width_mm,field==='height_mm'?clamp(value,20,2000):state.project.figure.height_mm);return;}
  if(scope==='settings'){mutate(project=>{project.settings[field]=value;},'调整全图样式');return;}
  if(scope==='annotation') {const id=state.selectedAnnotationId;if(!id)return;if(field==='font_size_pt')value=clamp(value||8,1,144);if(['width_mm','height_mm'].includes(field)&&state.project.annotations.find(item=>item.id===id)?.type!=='line')value=Math.max(.1,value||.1);mutate(project=>{const item=project.annotations.find(item=>item.id===id);item[field]=value===null?0:value;},'编辑标注');return;}
  if(scope==='panel') {
    const id=state.selectedPanelId;if(!id)return;
    mutate(project=>{const panel=project.panels.find(item=>item.id===id);if(['width_mm','height_mm'].includes(field)){value=Math.max(1,value||1);if(state.lockAspect!==false){const ratio=value/panel[field],other=field==='width_mm'?'height_mm':'width_mm';panel[other]=round(panel[other]*ratio);} }if(['label_font_size_pt','title_font_size_pt'].includes(field))value=clamp(value||(field==='label_font_size_pt'?10:8),1,144);panel[field]=value===null?0:value;if(field==='label')panel.label_mode=String(value).trim()?'manual':'none';},'编辑面板属性');
  }
}

function changeHandler(event) {
  const element=event.target;
  if(element.dataset.discardedEdit)return;
  beginInputCommit(element);
  if(changeCompositionOption(element))return;
  if(handleWbInput(element)||handleAdvancedChange(element))return;
  if(element.matches('[data-channel-field]')){changeChannelField(element.dataset.channelId,element.dataset.channelField,element.type==='checkbox'?element.checked:element.value);return;}
  if(element.matches('[data-channel-plane]')){changeSourcePlane(element.dataset.channelPlane,element.value);return;}
  if(element.matches('[data-split-field]')){changeSplitSetting(element.dataset.splitField,element.type==='checkbox'?element.checked:element.value);return;}
  if(element.matches('[data-composition-field]')){editCompositionSource(Number(element.dataset.index),element.dataset.compositionField,element.value);return;}
  if(element.matches('[data-analysis-channel]')){setAnalysisChannel(element.dataset.analysisChannel,element.checked,{single:element.dataset.analysisChannelMultiple!=='true'});return;}
  if(element.matches('[data-analysis-rgb]')){setAnalysisChannel(element.value,true,{single:true});return;}
  if(element.matches('[data-wb-source]')){const choice=availableAnalysisChannels().find(item=>item.id===element.value);if(choice){state.sourcePointerIdentity=clone(choice.source);state.sourceRoi=null;const view=currentChannelView();if(view?.channels.some(item=>item.id===choice.id))changeChannelMode('single',choice.id);else emit();}return;}
  if(element.matches('[data-background-channel]')){mutate(project=>{const params=project.settings.analysis_params ||= clone(state.analysisParams);params.background_values ||= {};params.background_values[element.dataset.backgroundChannel]=element.value;},'设置原始背景强度');return;}
  if(element.matches('[data-channel-threshold]')){mutate(project=>{const params=project.settings.analysis_params ||= clone(state.analysisParams);params.thresholds ||= {};if(element.value==='')delete params.thresholds[element.dataset.channelThreshold];else params.thresholds[element.dataset.channelThreshold]={method:'manual',value:element.value};},'设置逐通道原始阈值');return;}
  if(element.matches('[data-qpcr-group-key]')){if(!qpcrTableMatches(state.table,state.selectedAssetId,state.analysisParams))throw new Error('请先读取当前 Ct 工作表');editQpcrParameters(params=>{params.group_mapping ||= {};const value=element.value.trim();if(value)params.group_mapping[element.dataset.qpcrGroupKey]=value;else delete params.group_mapping[element.dataset.qpcrGroupKey];},'设置当前 Ct 表样本组别');return;}
  if(element.matches('[data-wb-exposure]')){mutate(project=>{const params=project.settings.analysis_params ||= clone(state.analysisParams);params.selected_exposures ||= {};if(element.value)params.selected_exposures[element.dataset.wbExposure]=element.value;else delete params.selected_exposures[element.dataset.wbExposure];},'明确选择 WB 曝光');return;}
  if(element.matches('[data-qpcr-include]')){if(!qpcrTableMatches(state.table,state.selectedAssetId,state.analysisParams))throw new Error('请先读取当前 Ct 工作表');const row=Number(element.dataset.qpcrInclude);editQpcrParameters(params=>{const excluded=new Set(params.excluded_source_rows || []);if(element.checked)excluded.delete(row);else excluded.add(row);params.excluded_source_rows=[...excluded].sort((a,b)=>a-b);},'设置当前 Ct 表原始孔纳入');return;}
  if(element.matches('[data-field]')){handleField(element);return;}
  if(element.matches('[data-project-title]')){const title=element.value.trim() || '未命名 Figure';if(title!==state.project.title)mutate(project=>{project.title=title;},'修改项目名称');return;}
  if(element.matches('[data-lock-aspect]')){state.lockAspect=element.checked;return;}
  if(element.matches('[data-scale-bar-visible]')){
    const panel=selectedPanel(),asset=selectedAsset();if(!panel)return;
    const geometry=scaleBarGeometry(panel,asset);if(element.checked&&!geometry.valid){notify(geometry.message,'info');emit();return;}
    mutate(project=>{updateScaleBar(project.panels.find(item=>item.id===panel.id),asset,{},element.checked);},element.checked?'显示比例尺':'关闭比例尺');return;
  }
  if(element.matches('[data-chart-type]')){if(!isAnalysisChartType(element.value)){notify('请选择已支持的图表类型','info');emit();return;}mutate(project=>{project.settings.analysis_chart_type=element.value;},'选择结果图类型');return;}
  if(element.matches('[data-gel-lane-count]')){state.gelLaneCount=element.value;return;}
  if(element.matches('[data-lane-field]')){const index=Number(element.dataset.index),field=element.dataset.laneField;mutate(project=>{const gel=editableSource(project).gel;if(field==='name')gel.lane_names[index]=element.value;else gel.lane_centers[index]=clamp(number(element.value),0,selectedAsset().width_px);},'编辑泳道');return;}
  if(element.matches('[data-marker-field]')){const index=Number(element.dataset.index),field=element.dataset.markerField,value=Number(element.value);if(!Number.isFinite(value)||(field==='size'&&value<=0)){notify('请输入有效 Marker 数值','info');return;}mutate(project=>{const points=editableSource(project).gel.marker_points;points[index][field]=value;},'修改 Marker 校准');return;}
  if(element.matches('[data-roi-field]')){
    const id=element.dataset.roiId,key=element.dataset.roiField;
    const numeric=['x','y','width','height'].includes(key),value=element.type==='checkbox'?element.checked:numeric?Number(element.value):element.value;
    if(numeric&&(!Number.isFinite(value)||element.value===''||value<(['x','y'].includes(key)?0:1))){notify('请输入有效的原始像素坐标和尺寸','info');emit();return;}
    if(numeric){const roi=state.project.settings.analysis_rois.find(item=>item.id===id),dimensions=channelSourceDimensions(selectedAsset());if(roi){const next={...roi,[key]:value};if(next.x+next.width>dimensions.width_px||next.y+next.height>dimensions.height_px){notify('测量框不能超出原始图像边界','info');emit();return;}}}
    mutate(project=>{const roi=project.settings.analysis_rois.find(item=>item.id===id);if(roi)roi[key]=value;},numeric?'调整原始测量区域':'编辑样本信息');return;
  }
  if(element.matches('[data-analysis-field]')){
    const field=element.dataset.analysisField,selected=selectedAnalysisChannels(),workspaceKey=analysisWorkspaceKey();
    if(state.analysisKind==='qpcr'&&selectedAsset()?.kind==='table') {
      if(field==='sheet'){mutate(project=>selectQpcrWorksheet(project,state.selectedAssetId,element.value),'切换 Ct 工作表');loadTable();return;}
      if(qpcrParameterFields.includes(field)){editQpcrParameters(params=>{params[field]=element.type==='checkbox'?element.checked:element.value;if(field==='reference_gene')params.reference_genes=element.value.split(/[,，;；\n]/).map(value=>value.trim()).filter(Boolean);},'调整当前 Ct 表分析参数');return;}
    }
    mutate(project=>{const params=project.settings.analysis_params=clone(state.analysisParams);params[field]=element.type==='checkbox'?element.checked:element.value;
      if(field==='reference_gene')params.reference_genes=element.value.split(/[,，;；\n]/).map(value=>value.trim()).filter(Boolean);
      if(field==='method'&&!['channel_intensity','threshold','channel_overlap','line_profile','fiber_cross_section','myotube'].includes(element.value)&&selected.length>1){project.settings.analysis_channel_selections[workspaceKey]=selected.slice(0,1);}
    },'调整分析参数');
    if(field==='method'){state.sourceTool=element.value==='line_profile'?'analysis-line':'select';state.analysisRegionTarget='roi';restoreAnalysisGeometry();if(element.value==='cellpose')loadCellposeStatus();emit();}
    if(field==='sheet')loadTable();return;
  }
  if(element.matches('[data-export-format]')){const issue=exportSizeIssue(element.value,state.project.figure);if(issue){notify(issue,'info');emit();return;}state.modal.format=element.value;emit();return;}
  if(element.matches('[data-export-dpi]')){state.modal.dpi=Number(element.value);return;}
  if(element.matches('[data-export-outlined]')){state.modal.outlined=element.checked;return;}
}

function keyHandler(event) {
  // IME confirmation belongs to the native input, including browsers that
  // identify its final Enter only with the legacy composition key code.
  if(event.isComposing || event.keyCode===229)return;
  // Native controls and accessible activation targets own Enter and Space.
  // Source drawing shortcuts apply only when focus is outside those controls.
  if((event.key==='Enter'||event.code==='Space')&&event.target.closest('button,a[href],summary,[role="button"],[role="link"]'))return;
  const editing=event.target.closest('input,textarea,select,[contenteditable=true]');
  if(event.code==='Space'&&!editing&&!state.modal&&state.tab!=='compose'&&document.getElementById('source-svg')){event.preventDefault();state.spacePressed=true;document.querySelectorAll('[data-source-canvas]').forEach(svg=>svg.classList.add('pannable'));return;}
  if(state.modal&&!state.busy&&event.key==='Tab'){
    const focusable=[...document.querySelectorAll('.modal button:not(:disabled),.modal input:not(:disabled),.modal select:not(:disabled),.modal textarea:not(:disabled)')].filter(element=>element.offsetParent!==null);
    const first=focusable[0],last=focusable.at(-1),dialog=document.querySelector('.modal');
    if(!dialog?.contains(document.activeElement)){event.preventDefault();(event.shiftKey?last:first)?.focus();}
    else if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}return;
  }
  if(state.modal&&event.key==='Enter'&&editing){if(state.modal.type==='marker'){event.preventDefault();beginInputCommit(event.target);actions['confirm-marker']();return;}if(state.modal.type==='new'){event.preventDefault();beginInputCommit(event.target);return actions['create-project']();}}
  if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='s'){event.preventDefault();if(state.busy)return;if(editing&&event.target.matches('[data-pending-edit]'))changeHandler({target:event.target});if(event.shiftKey){saveProjectCopy();return;}persistProject().then(()=>notify('已保存到本机工作区；备份或换电脑请使用「另存项目」')).catch(()=>{});return;}
  if(event.key==='Escape'){event.preventDefault();discardInputDraft(event.target);if(state.modal){closeModal();return;}else if(state.tab==='compose'){state.selectedPanelId=null;state.selectedPanelIds=[];state.selectedAnnotationId=null;}else{state.sourceTool='select';state.sourcePolygon=[];if(!(state.tab==='analysis'&&state.analysisKind==='microscopy'))state.sourceRoi=null;}emit();return;}
  if(event.key==='Enter'&&editing&&!state.modal&&event.target.matches('[data-pending-edit]')){event.preventDefault();changeHandler({target:event.target});event.target.blur();return;}
  if(editing || state.modal || state.interactionLock)return;
  if(event.key==='Enter'&&state.sourceTool==='polygon'){event.preventDefault();finishPolygon();return;}
  if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='z'){event.preventDefault();changeHistory(event.shiftKey?redo:undo);return;}
  if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='y'){event.preventDefault();changeHistory(redo);return;}
  if(state.tab==='compose'&&(event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='a'){event.preventDefault();selectAllComposition();return;}
  if(state.tab==='compose'&&(event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='g'){event.preventDefault();groupComposition(event.shiftKey);return;}
  if(state.tab==='compose'&&(event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='d'){event.preventDefault();duplicateComposition();return;}
  if((event.key==='Delete'||event.key==='Backspace')&&state.tab==='compose'){event.preventDefault();deleteSelection();return;}
  if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)&&state.tab==='compose'&&(state.selectedPanelId||state.selectedAnnotationId)){
    event.preventDefault();const delta=event.shiftKey?1:.1;
    const ids=new Set(selectedPanels().map(panel=>panel.id));mutate(project=>{const items=state.selectedPanelId?project.panels.filter(panel=>ids.has(panel.id)):project.annotations.filter(annotation=>annotation.id===state.selectedAnnotationId);for(const item of items){if(event.key==='ArrowLeft')item.x_mm=round(item.x_mm-delta);if(event.key==='ArrowRight')item.x_mm=round(item.x_mm+delta);if(event.key==='ArrowUp')item.y_mm=round(item.y_mm-delta);if(event.key==='ArrowDown')item.y_mm=round(item.y_mm+delta);}},'微调选中对象');
  }
}

function bindDragDrop() {
  document.addEventListener('dragstart',event=>{const asset=event.target.closest('[draggable=true][data-id]');if(asset){event.dataTransfer.setData('application/x-figure-asset',asset.dataset.id);event.dataTransfer.effectAllowed='copy';}});
  document.addEventListener('dragover',event=>{const zone=event.target.closest('[data-drop-zone]');if(zone){event.preventDefault();zone.classList.add('drop-active');event.dataTransfer.dropEffect='copy';}});
  document.addEventListener('dragleave',event=>{const zone=event.target.closest('[data-drop-zone]');if(zone && !zone.contains(event.relatedTarget))zone.classList.remove('drop-active');});
  document.addEventListener('drop',event=>{const zone=event.target.closest('[data-drop-zone]');if(!zone)return;event.preventDefault();document.querySelectorAll('.drop-active').forEach(element=>element.classList.remove('drop-active'));const assetId=event.dataTransfer.getData('application/x-figure-asset');try{if(assetId && zone.dataset.dropZone==='canvas'){const svg=document.getElementById('figure-svg'),matrix=svg?.getScreenCTM(),point=matrix?new DOMPoint(event.clientX,event.clientY).matrixTransform(matrix.inverse()):null;addAssetsToComposition([assetId],{layout:false,position:point});return;}if(event.dataTransfer.files.length)importSelectedFiles([...event.dataTransfer.files],{compose:zone.dataset.dropZone==='canvas'||state.tab==='compose'}).catch(error=>notify(error.message || '导入未完成','error'));}catch(error){notify(error.message,'error');}});
}

export function bindInteractions() {
  document.addEventListener('click',event=>{const element=event.target.closest('[data-action]');if(!element || element.disabled)return;event.preventDefault();if(state.busy&&element.dataset.action!=='cancel-busy')return;if((state.workspaceStatus?.has_recovery||state.workspaceStatus?.cleanup_pending)&&!['restore-workspace','clear-workspace-dialog','clear-workspace','new-dialog','create-project','close-modal','dismiss-error'].includes(element.dataset.action)){closeModal();return;}const action=actions[element.dataset.action];Promise.resolve().then(()=>action?action(element,event):handleWbAction(element.dataset.action,element)).catch(error=>notifyError(error));});
  document.addEventListener('click',event=>{if(!state.busy&&event.target.matches('[data-modal-backdrop]'))closeModal();});
  document.addEventListener('change',event=>{try{changeHandler(event);}catch(error){notify(error.message || '设置未应用','error');emit();}});
  document.addEventListener('input',event=>{
    const element=event.target;
    if(element.matches('[data-asset-search]')){state.filter=element.value;emit();return;}
    markInputDraft(element);
  });
  document.addEventListener('focusout',event=>{
    const element=event.target;
    // Also commit a draft if a browser/tool changed focus before native change.
    if(element.matches?.('[data-pending-edit]')){try{changeHandler({target:element});}catch(error){notify(error.message || '设置未应用','error');emit();}}
    if(state.renderDeferred&&!state.formCommitPending&&!state.formPointerPending)queueMicrotask(emit);
  });
  document.addEventListener('pointerdown',event=>holdPointerDraft(event),true);
  document.addEventListener('pointerup',releasePointerDraftSoon);
  document.addEventListener('pointercancel',releasePointerDraft);
  document.addEventListener('click',releasePointerDraft);
  document.addEventListener('keydown',event=>{try{const pending=keyHandler(event);pending?.catch(error=>{notifyError(error);emit();});}catch(error){notifyError(error);emit();}});
  document.addEventListener('keyup',event=>{if(event.code==='Space'){state.spacePressed=false;document.querySelectorAll('[data-source-canvas]').forEach(svg=>svg.classList.remove('pannable'));}});
  window.addEventListener('blur',()=>{releasePointerDraft();state.spacePressed=false;document.querySelectorAll('[data-source-canvas]').forEach(svg=>svg.classList.remove('pannable'));});
  document.getElementById('import-files').addEventListener('change',event=>{const files=[...event.target.files],task=state.importIntent;state.importIntent=null;event.target.value='';importSelectedFiles(files,{task,compose:task==='compose'||!task&&state.tab==='compose'}).catch(error=>notify(error.message || '导入未完成','error'));});
  document.getElementById('import-files').addEventListener('cancel',()=>{state.importIntent=null;});
  document.getElementById('import-wb-files').addEventListener('change',event=>{const files=[...event.target.files];state.importIntent=null;event.target.value='';importSelectedFiles(files,{wb:true}).catch(error=>notify(error.message || 'WB 导入未完成','error'));});
  document.getElementById('import-wb-files').addEventListener('cancel',()=>{state.importIntent=null;});
  document.getElementById('open-project').addEventListener('change',event=>{const file=event.target.files[0];event.target.value='';importProjectFile(file).catch(error=>notifyError(error,'项目未打开'));});
  window.addEventListener('beforeunload',event=>{if(state.dirty){event.preventDefault();event.returnValue='';}});
  let resizeTimer;window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(emit,150);});
  bindGestures();bindDragDrop();
  bindInstanceReview({state,emit,mutate,request,notify,flushSave,saveBlob,currentSelection:()=>analysisSelection('microscopy')});
}

