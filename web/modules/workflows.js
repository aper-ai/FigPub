import {escapeHtml as e} from './domain.js';
import {icon} from './icons.js';

/** User tasks describe intent; none of them infer scientific sample identities. */
export const workbenchTasks = Object.freeze([
  {id:'wb',title:'WB 定量与出图',icon:'gel',description:'识别条带，确认目标蛋白和内参，生成定量结果与图稿。',input:'TIFF、PNG、JPG 原图',steps:['导入 WB 原图','核对条带与样本','生成定量和图稿']},
  {id:'microscopy',title:'显微与组织图像',icon:'microscope',description:'测量强度、分析染色或细胞，再将结果加入图稿。',input:'显微、IHC、多通道 TIFF',steps:['导入原始图像','选择方法与通道','查看结果并组图']},
  {id:'qpcr',title:'qPCR 数据分析',icon:'chart',description:'读取 Ct 表，指定内参和对照组，计算相对表达。',input:'CSV、TSV、TXT、XLSX',steps:['导入 Ct 数据表','确认内参和分组','计算并生成统计图']},
  {id:'compose',title:'组图与排版',icon:'layers',description:'拖入已有图片或图表，统一版式、编号和投稿尺寸。',input:'图片、PDF、SVG 图表',steps:['导入图片与图表','选择版式并编辑','检查并导出图稿']},
]);

export function taskById(id) {return workbenchTasks.find(task=>task.id===id) || null;}
export function hasProjectContent(project) {
  return Boolean(project && ['assets','panels','annotations','measurements'].some(key=>project[key]?.length));
}
export function taskForWorkspace(tab,kind) {
  if(tab==='compose')return 'compose';
  if(tab==='gel')return 'wb';
  if(tab==='channels')return 'microscopy';
  if(tab==='analysis')return ['wb','qpcr','microscopy'].includes(kind)?kind:null;
  return null;
}
export function acceptsTaskAsset(task,asset) {
  if(!asset)return false;
  if(task==='qpcr')return asset.kind==='table';
  if(task==='wb'||task==='microscopy')return asset.kind==='image';
  return asset.kind!=='table';
}
export function resolveTaskImport(task,assets=[]) {
  const requested=taskById(task)?.id || null;
  const effective=requested || (assets.length&&assets.every(asset=>asset.kind==='table')?'qpcr':null);
  const asset=effective?assets.find(item=>acceptsTaskAsset(effective,item)):assets[0];
  return {task:effective,assetId:asset?.id || null,tab:effective==='compose'?'compose':effective==='wb'?'gel':effective==='qpcr'||effective==='microscopy'?'analysis':'source',analysisKind:effective==='qpcr'||effective==='microscopy'?effective:null,
    compositionAssetIds:effective==='compose'?assets.filter(item=>acceptsTaskAsset('compose',item)).map(item=>item.id):[]};
}
export function preferredResumeTab(project) {
  if(project?.panels?.length||project?.annotations?.length)return 'compose';
  return 'source';
}
const taskButton=(task,label=task.title)=>`<button class="button" data-action="start-task" data-task="${task.id}">${icon(task.icon)}${e(label)}</button>`;

export function renderHome(state) {
  const project=state.project,hasContent=hasProjectContent(project),demo=Boolean(project.settings?.is_demo);
  return `<main class="home-workspace" aria-labelledby="home-title"><div class="home-content">
    <section class="home-heading"><div><span class="eyebrow">图研 · 本地科研工作台</span><h1 id="home-title">一站式科研图像分析与图稿制作</h1><p>面向科研人员，贯通图像定量、实验数据分析与论文图稿导出。</p></div><button class="button" data-action="help">${icon('help')}快速入门</button></section>
    ${hasContent?`<section class="resume-project" aria-label="继续当前项目"><span class="resume-icon">${icon('folder')}</span><div><span class="resume-caption">当前项目${demo?' · 合成示例':''}</span><h2>${e(project.title || '未命名项目')}</h2><p>${project.assets.length} 份素材 · ${project.panels.length} 个图片面板 · ${project.measurements?.length || 0} 次分析记录</p></div><button class="button primary" data-action="resume-workspace">继续编辑 ${icon('arrow')}</button></section>`:''}
    <section class="home-task-grid" aria-label="选择工作任务">${workbenchTasks.map(task=>`<button class="home-task" data-action="start-task" data-task="${task.id}"><span class="home-task-icon">${icon(task.icon)}</span><h2>${task.title}</h2><p>${task.description}</p><span class="home-task-input">${task.input}</span><span class="home-task-go">${project.assets.some(asset=>acceptsTaskAsset(task.id,asset))||task.id==='compose'&&(project.panels.length||project.annotations.length)?'进入工作台':'导入文件'} ${icon('arrow')}</span></button>`).join('')}</section>
    <section class="home-project-tools" aria-label="项目操作"><button data-action="open-project">${icon('folder')}<span><strong>打开已有项目</strong><small>.figwork、.gelfig、.sfig</small></span>${icon('chevron')}</button><button data-action="new-dialog">${icon('plus')}<span><strong>新建项目</strong><small>创建独立的分析与组图项目</small></span>${icon('chevron')}</button><button data-action="clear-workspace-dialog">${icon('trash')}<span><strong>清除本机工作区</strong><small>删除软件保存的素材副本与恢复记录</small></span>${icon('chevron')}</button></section>
    <p class="home-save-note">${icon('save')}当前项目自动保存在本机，再次启动时须主动选择恢复；「另存项目」可保存含原件的 .figwork 文件。</p>
  </div></main>`;
}

export function workspaceGuidance(state) {
  const project=state.project,asset=project.assets.find(item=>item.id===state.selectedAssetId),task=taskById(taskForWorkspace(state.tab,state.analysisKind));
  if(asset?.kind==='table'&&state.tableError&&['source','analysis'].includes(state.tab))return {title:'数据表读取未完成',text:'原文件已保留。可重新读取，或根据表格中的错误提示修正后再次导入。',action:'load-table',label:'重新读取数据表',task:task?.id || 'qpcr'};
  if(state.tab==='compose') {
    if(!project.panels.length&&!project.annotations.length) {
      const available=project.assets.some(item=>item.kind!=='table');
      return {title:'组图排版',text:available?'从素材库添加图片与图表，再设置期刊尺寸和布局。':'先导入图片或已有图表，导入后会自动加入画布。',action:available?'add-all-panels':'import',label:available?'将素材加入画布':'导入图片与图表',task:'compose'};
    }
    return {title:'组图排版',text:'在右侧设置期刊尺寸与版式，选择面板可编辑属性。导出前请检查图稿。',action:'preflight',label:'检查图稿',task:'compose'};
  }
  if(state.tab==='channels')return {title:'多通道显示',text:asset?.kind==='image'?'在右侧选择可见通道、颜色和显示范围；完成后可将通道视图加入组图。':'先导入原始多通道图像，或从素材库选择一张图像。',action:asset?.kind==='image'?'add-channel-panels':'import',label:asset?.kind==='image'?'将通道加入组图':'导入原始图像',task:'microscopy'};
  if(task?.id==='wb')return {title:'WB 定量与出图',text:asset?.kind==='image'?'识别条带后核对目标、内参和样本，生成定量 ROI、结果与图稿。':'导入 WB 原图后，向导会生成待核对的条带位置。',action:asset?.kind==='image'?'open-wb-workflow':'import-wb',label:asset?.kind==='image'?'打开自动出图向导':'导入 WB 原图',task:'wb'};
  if(task?.id==='qpcr')return {title:'qPCR 数据分析',text:asset?.kind==='table'?'核对数据表，在右侧选择内参基因与对照组；名称须与表中记录一致。':'导入 Ct 数据表，或下载模板查看必填字段。',action:asset?.kind==='table'?'focus-analysis-settings':'import',label:asset?.kind==='table'?'填写分析设置':'导入 Ct 表',task:'qpcr'};
  if(task?.id==='microscopy')return {title:'显微与组织图像',text:asset?.kind==='image'?'在右侧选择分析方法和原始通道；可在图上圈选区域，再运行分析。':'导入原始图像后，选择强度测量、细胞分割、IHC 或纤维分析。',action:asset?.kind==='image'?'focus-analysis-settings':'import',label:asset?.kind==='image'?'选择分析方法':'导入原始图像',task:'microscopy'};
  return {title:'素材库',text:asset?(asset.kind==='table'?'数据表已就绪，可以进入 qPCR 分析。':'可裁切、标注或将素材加入组图。分析入口位于顶部工作区导航。'):'导入图像、图表或数据表，也可以打开保存的项目。',action:asset?(asset.kind==='table'?'analyze-table':'add-panel'):'import',label:asset?(asset.kind==='table'?'进入 qPCR 分析':'加入组图'):'导入素材',task:null};
}
export function renderWorkspaceGuide(state) {
  if(state.tab==='home'||state.showGuide===false)return '';
  const guide=workspaceGuidance(state),task=taskById(guide.task);
  return `<section class="workspace-guide" aria-label="当前任务操作提示"><div class="workspace-guide-copy"><strong>${e(guide.title)}</strong><p>${e(guide.text)}</p>${task?`<ol class="task-steps">${task.steps.map((step,index)=>`<li><span>${index+1}</span>${step}</li>`).join('')}</ol>`:''}</div><div class="workspace-guide-actions"><button class="button small primary" data-action="${guide.action}" ${guide.task?`data-task="${guide.task}"`:''}>${e(guide.label)}</button><button class="icon-button small" data-action="toggle-guide" aria-label="收起操作提示" title="收起操作提示">${icon('close')}</button></div></section>`;
}

export function renderTaskEmpty(state) {
  const task=taskById(taskForWorkspace(state.tab,state.analysisKind));
  if(!task)return `<section class="empty-workspace task-empty" data-drop-zone="import"><span class="empty-emblem">${icon('image')}</span><h1>导入原始素材</h1><p>支持图像、图表和数据表。导入后可查看原始文件并选择处理方式。</p><div class="empty-actions"><button class="button primary" data-action="import">${icon('upload')}导入素材</button><button class="button" data-action="home">选择工作任务</button></div></section>`;
  return `<section class="empty-workspace task-empty" data-drop-zone="import"><span class="empty-emblem">${icon(task.icon)}</span><h1>${e(task.steps[0])}</h1><p>${e(task.description)}<br><span class="task-empty-formats">${e(task.input)}</span></p><div class="empty-actions"><button class="button primary" data-action="${task.id==='wb'?'import-wb':'import'}" data-task="${task.id}">${icon('upload')}选择文件</button>${task.id==='qpcr'?'<button class="button" data-action="qpcr-template">下载 Ct 表模板</button>':''}</div><button class="text-button" data-action="home">返回首页选择其他任务</button></section>`;
}

export function renderQuickStart() {
  return `<div class="quick-start"><p class="modal-intro">从首页选择任务，或通过顶部导航切换工作区。</p>${workbenchTasks.map(task=>`<section><h3>${icon(task.icon)}${task.title}</h3><ol>${task.steps.map(step=>`<li>${step}</li>`).join('')}</ol>${taskButton(task,'进入工作台')}</section>`).join('')}<section class="quick-start-save"><h3>${icon('save')}项目保存与图稿导出</h3><p><strong>本机自动保存</strong>：保留当前工作区，下次启动选择恢复后继续。<br><strong>另存项目</strong>：保存 .figwork，包含原图、参数和布局，支持继续编辑、备份和跨设备迁移。<br><strong>导出图稿</strong>：生成投稿或演示用的 PDF、PNG、SVG 等文件。</p><p>新建或打开项目会切换当前工作区；切换前可先另存项目。「清除本机工作区」用于删除软件保存的素材副本和恢复记录。</p></section><section class="quick-start-keys"><h3>常用快捷键</h3><p><kbd>Ctrl Z</kbd> 撤销　<kbd>Ctrl Y</kbd> 重做<br><kbd>Ctrl S</kbd> 保存到本机　<kbd>Ctrl Shift S</kbd> 另存项目<br><kbd>Shift</kbd> 点选多个面板　<kbd>Ctrl G</kbd> 编组<br><kbd>Alt</kbd> 拖动时临时暂停吸附<br><kbd>← ↑ ↓ →</kbd> 微调位置　<kbd>Delete</kbd> 删除选中对象</p></section></div>`;
}
