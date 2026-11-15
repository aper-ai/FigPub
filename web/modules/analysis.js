import {state, selectedAsset, emit, notify, flushSave, mutate, withBusy,scheduleSave,analysisSelection} from './state.js';
import {request, saveBlob, downloadMessage} from './api.js';
import {escapeHtml as e, tableCsv, assetFor, panelFromAsset} from './domain.js';
import {analysisSourceAssetIds,buildAnalysisChartRequest,wbScopedRois} from './analysis-domain.js';
import {icon} from './icons.js';
import {renderSourceCanvas, renderTable} from './source-view.js';
import {renderResult} from './analysis-results-view.js';
import {renderInstanceReview} from './instance-review.js';
import {renderAdvancedResults} from './histology-fibers.js';
import {renderWbEntry} from './wb-workflow.js';
import {renderTaskEmpty} from './workflows.js';
import {qpcrParameters,bindQpcrWorksheet,qpcrTableMatches} from './qpcr-workspace.js';
import {createAnalysisTaskController,isActiveAnalysisJob} from './analysis-controller.js';

export function currentResult() {
  return analysisSelection().result;
}

function renderRoiTable() {
  const scope=state.analysisParams.wb_draft_scope_id;
  const allRois=state.project.settings.analysis_rois || [],roiNumbers=new Map(allRois.map((roi,index)=>[roi.id,index+1]));
  const rois=wbScopedRois(allRois,scope);
  const field = (roi, key, placeholder, width = '') => `<input id="roi-${roi.id}-${key}" class="table-input ${width}" data-roi-field="${key}" data-roi-id="${roi.id}" value="${e(roi[key] ?? '')}" placeholder="${placeholder}" aria-label="${e(key)}">`;
  return `<section class="analysis-card roi-card"><div class="card-heading"><div><h3>测量区域 <span class="count-badge">${rois.length}</span></h3><p>先框选条带，再填写样本身份；目标蛋白与内参分别标记。</p></div><button class="button small" data-action="source-tool" data-tool="roi">${icon('plus')}框选 ROI</button></div>
    ${rois.length ? `<div class="data-table-wrap"><table class="data-table roi-table"><thead><tr><th>区域 / 来源</th><th>纳入</th><th>实验 ID</th><th>角色</th><th>样本</th><th>组别</th><th>生物学重复</th><th>技术重复</th><th>曝光 ID</th><th>蛋白名称</th><th>内参区域</th><th></th></tr></thead><tbody>${rois.map(roi => `<tr class="${state.selectedRoiId === roi.id ? 'selected-row' : ''}"><td><button class="roi-name" data-action="select-roi" data-id="${roi.id}"><span class="roi-square ${roi.role === 'control' ? 'control' : ''}"></span>R${roiNumbers.get(roi.id)}</button><small class="source-filename">${e(assetFor(state.project, roi.asset_id)?.name || '素材缺失')}</small></td><td><input id="roi-${roi.id}-included" type="checkbox" data-roi-field="included" data-roi-id="${roi.id}" ${roi.included!==false?'checked':''} aria-label="纳入区域 ${roiNumbers.get(roi.id)}"></td><td>${field(roi,'experiment_id','实验 ID')}</td><td><select id="roi-${roi.id}-role" data-roi-field="role" data-roi-id="${roi.id}"><option value="target" ${roi.role === 'target' ? 'selected' : ''}>目标</option><option value="control" ${roi.role === 'control' ? 'selected' : ''}>内参</option></select></td><td>${field(roi, 'sample_id', '样本 ID')}</td><td>${field(roi, 'group', '组名')}</td><td>${field(roi, 'biological_replicate', '独立实验 ID')}</td><td>${field(roi, 'technical_replicate', '可选')}</td><td>${field(roi, 'exposure_id', '如 10 s')}</td><td>${field(roi, 'protein', '蛋白名')}</td><td><select id="roi-${roi.id}-control_roi_id" data-roi-field="control_roi_id" data-roi-id="${roi.id}"><option value="">自动匹配</option>${rois.filter(item => item.role === 'control' && item.id !== roi.id).map(item => `<option value="${item.id}" ${item.id === roi.control_roi_id ? 'selected' : ''}>R${roiNumbers.get(item.id)} ${e(item.sample_id || '')}</option>`).join('')}</select></td><td><button class="icon-button small danger-hover" data-action="delete-roi" data-id="${roi.id}" title="删除测量区域">${icon('trash')}</button></td></tr>`).join('')}</tbody></table></div>` : '<div class="small-empty">在上方原图拖出一个矩形，点击「设为测量区域」。所有测量都使用原始像素。</div>'}
  </section>`;
}

export function renderAnalysis() {
  const selection=analysisSelection();
  const asset = selectedAsset(), running = isActiveAnalysisJob(state.job), submitting = state.analysisSubmission;
  const readiness=state.tableLoading&&state.analysisKind==='qpcr'?'正在读取 Ct 表，请稍候。':state.tableError&&state.analysisKind==='qpcr'?state.tableError:state.analysisKind!=='qpcr'&&asset&&asset.kind!=='image'?'分析需要原始图像，PDF 和 SVG 请用于组图。':selection.input.error || (state.analysisKind==='qpcr'&&!qpcrTableMatches(state.table,asset?.id,state.analysisParams)?'请先读取当前 Ct 工作表，核对后再分析。':null);
  const hasInput=state.analysisKind==='qpcr'?asset?.kind==='table':asset?.kind==='image';
  const resultLabel=!selection.persisted?'查看未保存结果':selection.stale?'查看上次结果':running?'查看已有结果':'查看结果';
  return `<section class="analysis-workspace"><div class="analysis-heading"><div><span class="eyebrow">原始数据 · 可复算结果</span><h2>数据分析</h2></div><div class="inline-actions result-actions">${selection.result?`<button class="button" id="analysis-results-link" data-action="view-analysis-results" data-focus-anchor data-measurement-id="${e(selection.record.id)}" aria-controls="analysis-results">${icon('chart')}${resultLabel}</button>`:''}<button class="button ${running ? '' : 'primary'}" data-action="${running ? 'cancel-job' : 'run-analysis'}" ${submitting?'disabled aria-busy="true"':!running&&readiness?'disabled aria-describedby="analysis-readiness"':''}>${submitting?'<span class="spinner tiny"></span>':icon(running ? 'stop' : 'play')}${submitting?'正在提交…':running ? '取消任务' : '运行分析'}</button></div></div>
    <div class="analysis-tabs"><button class="${state.analysisKind === 'wb' ? 'selected' : ''}" data-action="analysis-kind" data-kind="wb">${icon('gel')}WB 灰度定量</button><button class="${state.analysisKind === 'qpcr' ? 'selected' : ''}" data-action="analysis-kind" data-kind="qpcr">${icon('chart')}qPCR</button><button class="${state.analysisKind === 'microscopy' ? 'selected' : ''}" data-action="analysis-kind" data-kind="microscopy">${icon('microscope')}显微图像</button></div>
    ${submitting?`<div class="job-progress" role="status"><span class="spinner"></span><div><strong>${submitting.phase==='saving'?'正在保存分析设置':'正在提交分析任务'}</strong><span>任务提交后可查看状态或取消。</span></div></div>`:running?state.job.pollingError?`<div class="notice warning" role="alert"><strong>暂时无法读取任务状态</strong><p>任务可能仍在后台运行。请重试查询或取消任务；当前不会重复提交分析。</p><p>${e(state.job.pollingError)}</p><button class="button small" data-action="retry-job-status">重试查询</button></div>`:`<div class="job-progress" role="status"><span class="spinner"></span><div><strong>${state.job.synchronizing?'正在载入分析结果':state.job.statusFailures?'正在重新读取任务状态':state.job.status === 'queued' ? '任务已进入队列' : '正在分析原始数据'}</strong><span>计算独立运行，取消后不会写入结果。</span></div><span class="elapsed-label">${Math.round((Date.now() - state.job.startedAt) / 1000)} s</span></div>`:''}
    ${!running&&!submitting&&readiness?`<div class="analysis-readiness" id="analysis-readiness" role="status">${icon('info')}<div><strong>开始前还需要</strong><p>${e(readiness)}</p></div>${hasInput?'<button class="text-button" data-action="focus-analysis-settings">查看设置</button>':''}</div>`:''}
    ${state.analysisKind === 'wb' ? `${asset?.kind === 'image' ? renderSourceCanvas({analysis: true}) : renderTaskEmpty(state)}${renderRoiTable()}` : state.analysisKind === 'qpcr' ? `<div class="analysis-card qpcr-intro"><div>${icon('grid')}<h3>从 Ct 表格计算相对表达</h3><p>技术重复先在生物学样本内汇总，再计算 ΔCt、ΔΔCt 与相对表达。请在右侧指定内参基因和对照组。</p></div><button class="button small" data-action="qpcr-template">${icon('download')}下载字段模板</button></div>${asset?.kind === 'table' ? renderTable(asset) : renderTaskEmpty(state)}` : `${asset?.kind === 'image' ? renderSourceCanvas({analysis: true}) : renderTaskEmpty(state)}`}
    ${state.analysisKind==='wb'?renderWbEntry():''}
    ${renderResult(selection)}
    ${state.analysisKind==='microscopy'?(selection.result?renderAdvancedResults(selection.result,selection):'')+renderInstanceReview(selection):''}
  </section>`;
}

let tableRequest=0,tableAbort=null,cellposeRequest=null;
export function clearTableWorkspace() {tableRequest++;tableAbort?.abort();tableAbort=null;}
export async function loadTable() {
  const asset=selectedAsset();if(!asset||asset.kind!=='table')return;
  const id=asset.id,token=++tableRequest,canonical=state.tab==='analysis'&&state.analysisKind==='qpcr',requestedSheet=canonical?qpcrParameters(state.project,id).sheet:null;tableAbort?.abort();tableAbort=new AbortController();
  if(state.lastError?.startsWith('读取表格失败：'))state.lastError=null;
  state.tableLoading=true;state.table=null;state.tableError=null;emit();
  try {
    const query=new URLSearchParams({limit:canonical?'1000':'150'});if(requestedSheet)query.set('sheet',requestedSheet);
    const table=await request(`/api/assets/${encodeURIComponent(id)}/${canonical?'qpcr':'table'}?${query}`,{signal:tableAbort.signal});
    if(state.selectedAssetId===id&&token===tableRequest) {
      state.table={...table,canonical,asset_id:id};
      if(canonical&&Array.isArray(table.sheets))state.qpcrSheetNames.set(id,table.sheets);
      if(canonical&&requestedSheet===null&&table.sheet)mutate(project=>bindQpcrWorksheet(project,id,requestedSheet,table.sheet),'确认自动识别的 Ct 工作表');
    }
  }catch(error){if(error.name!=='AbortError'&&state.selectedAssetId===id&&token===tableRequest){state.tableError=error.message || '请检查表格格式后重新导入。';notify(`读取表格失败：${state.tableError}`,'error');}}
  finally{if(token===tableRequest){state.tableLoading=false;emit();}}
}
export async function loadCellposeStatus(refresh=false) {
  if(cellposeRequest)return cellposeRequest;
  state.cellposeStatusLoading=true;emit();
  cellposeRequest=request(`/api/cellpose/status?refresh=${refresh}`).then(status=>{state.cellposeStatus=status;return status;}).catch(error=>{state.cellposeStatus={available:false,reason:error.message,models:[]};notify(`本地模型检查失败：${error.message}`,'error');}).finally(()=>{cellposeRequest=null;state.cellposeStatusLoading=false;emit();});
  return cellposeRequest;
}

const analysisTasks = createAnalysisTaskController({state,request,emit,notify,prepare:flushSave,
  matchesInput:context=>analysisSelection(context.kind).input.signature===context.inputSignature,
  acceptResult:async(job,update)=>{
    const {kind,params,projectId,sourceRevision,inputSignature}=job.requestContext;
    const fresh=await request('/api/project');
    if(state.project.id!==projectId||state.job?.id!==job.id||!isActiveAnalysisJob(state.job))return;
    state.resultRecord={id:job.id,kind,params,project_id:projectId,source_project_revision:sourceRevision,source_asset_ids:analysisSourceAssetIds(kind,params),result:update.result};
    // Only a verified result-only revision may advance the local recipe version.
    const resultOnlyAppend=fresh.revision===sourceRevision+1&&(fresh.measurements || []).some(item=>item.id===job.id&&item.source_project_revision===sourceRevision);
    if(fresh.id===projectId&&(resultOnlyAppend||fresh.revision===state.project.revision)) {
      state.project.measurements=fresh.measurements || [];
      if(resultOnlyAppend&&fresh.revision>=state.project.revision){state.project.revision=fresh.revision;state.project.updated_at=fresh.updated_at;if(state.dirty)scheduleSave();}
    }
    const persisted=state.project.measurements.some(item=>item.id===job.id),sameInput=analysisSelection(kind).input.signature===inputSignature;
    notify(!persisted?'分析完成，但项目版本已变化，结果尚未写入项目；请重新运行':sameInput?'分析完成，结果已保存到项目':'分析完成；当前科学输入已变更，旧结果仅供核对',persisted&&sameInput?'success':'info');
  },
});

export async function runAnalysis() {
  if(state.analysisSubmission||isActiveAnalysisJob(state.job))return;
  const kind=state.analysisKind,selection=analysisSelection(),projectId=state.project.id,sourceAssetId=state.selectedAssetId;
  if(kind!=='qpcr'&&selectedAsset()?.kind!=='image'){notify('请先选择原始图像，PDF 和 SVG 请用于组图。','info');return;}
  if(kind==='qpcr'&&(state.tableLoading||state.tableError)){notify(state.tableLoading?'数据表仍在读取，请稍候。':state.tableError,'info');return;}
  if(kind==='qpcr'&&!qpcrTableMatches(state.table,sourceAssetId,state.analysisParams)){notify('当前工作表尚未读取，请先重新读取并核对。','info');return;}
  if(selection.input.error){notify(selection.input.error,'info');return;}
  return analysisTasks.submit({kind,params:selection.input.params,inputSignature:selection.input.signature,projectId,sourceAssetId});
}

export const cancelJob = () => analysisTasks.cancel();
export const retryJobStatus = () => analysisTasks.retry();

export function downloadResults(element=null) {
  const result = currentResult(); if (!result) return;
  const section=element?.dataset?.resultSection,rows=section&&Array.isArray(result[section])?result[section]:result.rows?.length?result.rows:result.pairs?.length?result.pairs:result.objects?.length?result.objects:result.summary?[result.summary]:[];
  return withBusy('正在保存分析结果…',async signal=>{const saved=await saveBlob(new Blob([tableCsv(rows)], {type: 'text/csv;charset=utf-8'}), `${result.kind || state.analysisKind}_${section || 'results'}.csv`,{signal});notify(downloadMessage(saved));});
}

export async function plotResults() {
  const selection=analysisSelection(),chartType=state.project.settings.analysis_chart_type || 'scatter';
  let body;
  try {body=buildAnalysisChartRequest(selection,chartType);}catch(error){notify(error.message,'info');return;}
  const measurement=selection.record,projectId=state.project.id;
  return withBusy('正在生成分析图表…',async()=>{try {
    await flushSave();
    const current=analysisSelection();
    if(state.project.id!==projectId||current.record?.id!==measurement.id||current.stale||!current.persisted)throw new Error('当前展示结果已改变，请重新选择后生成图表');
    const generated = await request('/api/charts', {method:'POST',body});
    const fresh = generated.project || await request('/api/project');
    state.project.assets = fresh.assets; state.project.revision = fresh.revision;
    const asset = generated.asset;
    if (!asset) throw new Error('服务未返回图表素材');
    mutate(project => {
      const panel = panelFromAsset(project,asset);
      panel.analysis_source = {measurement_id:measurement.id,source_asset_ids:measurement.source_asset_ids || [],params:measurement.params || {}};
      project.panels.push(panel); state.selectedPanelId=panel.id; state.selectedAssetId=asset.id;
    },'添加分析图表');
    state.tab='compose';emit();notify('已添加来自实际分析结果的图表，来源随项目保存');
  } catch(error) {notify(`生成结果图失败：${error.message}`,'error');}
  },{cancellable:false});
}
