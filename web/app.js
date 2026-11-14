import {request} from './modules/api.js';
import {state,replaceProject,subscribe,emit,flushSave} from './modules/state.js';
import {renderApp} from './modules/view.js';
import {bindInteractions} from './modules/interaction.js';
import {loadTable} from './modules/analysis.js';

subscribe(renderApp);
bindInteractions();

async function start() {
  try {
    const [health, project] = await Promise.all([request('/api/health'),request('/api/project')]);
    state.health=health;
    if(project.settings?.analysis_params)Object.assign(state.analysisParams,project.settings.analysis_params);
    replaceProject(project);
    state.tab='home';
    emit();
    if(state.tab==='source' && project.assets.find(asset=>asset.id===state.selectedAssetId)?.kind==='table')loadTable();
  } catch(error) {
    document.getElementById('app').innerHTML='<div class="boot-screen"><span class="boot-mark">图</span><h1>暂时无法连接本地工作台</h1><p id="boot-error"></p><button class="button primary" id="retry-start">重新连接</button></div>';
    document.getElementById('boot-error').textContent=error.message;
    document.getElementById('retry-start').addEventListener('click',start);
  }
}
start();
document.fonts?.ready.then(emit);
document.fonts?.addEventListener('loadingdone',emit);

// The native desktop shell waits for this Promise before it closes the window.
// A pending input must reach its change handler before the last save snapshot.
window.figureWorkbenchPrepareClose = async () => {
  const active=document.activeElement;
  try {
    if(active?.matches?.('[data-pending-edit]')) {
      if(active.checkValidity&&!active.checkValidity())return {saved:false,busy:false,message:'当前输入无效，请返回工作台修正后再退出。'};
      active.dispatchEvent(new Event('change',{bubbles:true}));
    }
    active?.blur?.();
    // Input lifecycle intentionally defers render until the native focus event
    // completes; let that commit finish before treating it as an active edit.
    if(state.formCommitPending)await new Promise(resolve=>setTimeout(resolve,0));
    if(state.interactionLock || state.formCommitPending || state.formPointerPending)return {saved:false,busy:true,message:'仍有未完成的编辑操作，请先结束拖动或输入。'};
    await flushSave();
    const busy=Boolean(state.busy || state.gelBusy || state.wbWorking || state.job&&['queued','running'].includes(state.job.status));
    if(['wb-workflow','wb-exposures','channel-composition','marker','new'].includes(state.modal?.type))return {saved:false,busy,message:'工作区已保存，但当前对话框中的设置尚未应用。请先完成或取消该操作。'};
    return {saved:!state.dirty&&state.saveState!=='error',busy,message:busy?'仍有任务在运行，退出会停止任务。':'当前工作区已保存到本机。'};
  } catch(error) {
    return {saved:false,busy:Boolean(state.busy || state.gelBusy || state.wbWorking || state.job&&['queued','running'].includes(state.job.status)),message:`保存未完成：${error.message || '请返回工作台另存项目后再退出。'}`};
  }
};
