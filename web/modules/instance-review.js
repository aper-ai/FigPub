/** Explicit mask edits; immutable automatic results remain the replay base. */
import {isActiveAnalysisJob} from './analysis-controller.js';
let deps = null, bound = false;
const sessions = new Map();
const escape = value => String(value ?? '').replace(/[&<>"']/g, token => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[token]));
const copy = value => JSON.parse(JSON.stringify(value));
const MAX_PIXELS = 16000000, MAX_OPERATIONS = 200;
const operationId = () => `ir_${globalThis.crypto?.randomUUID?.() || `${Date.now()}_${Math.random().toString(16).slice(2)}`}`;

export function clearInstanceReviewSessions() {
  for(const session of sessions.values()){
    session.disposed=true;session.controller?.abort();session.labels?.fill(0);session.labels=null;session.result=null;session.draft=null;session.selected.clear();session.seeds=[];session.polygon=[];
  }
  sessions.clear();
  if(rectangle){try{rectangle.svg.releasePointerCapture(rectangle.pointerId);}catch{}rectangle=null;if(deps)deps.state.interactionLock=false;}
}

export function normalizeReviewDraft(value, baseHash) {
  if (!value) return {base_mask_sha256:baseHash,operations:[],cursor:0};
  if (value.base_mask_sha256 !== baseHash) throw new Error('保存的复核记录不属于当前原始分割。');
  if (!Array.isArray(value.operations) || value.operations.length > MAX_OPERATIONS || !Number.isInteger(value.cursor) || value.cursor < 0 || value.cursor > value.operations.length) throw new Error('保存的复核步骤或游标无效。');
  const identifiers = new Set();
  for (const operation of value.operations) {
    if (!operation || !['merge','split','delete','add'].includes(operation.type) || !/^[A-Za-z0-9_-]{1,100}$/.test(operation.id) || identifiers.has(operation.id)) throw new Error('实例复核步骤包含无效或重复的操作。');
    identifiers.add(operation.id);
  }
  return {base_mask_sha256:baseHash,operations:copy(value.operations),cursor:value.cursor};
}

export function appendReviewOperation(draft, operation) {
  const current = normalizeReviewDraft(draft,draft.base_mask_sha256);
  if (current.cursor >= MAX_OPERATIONS) throw new Error('当前结果已达到 200 步修订上限。');
  const operations = [...current.operations.slice(0,current.cursor),copy(operation)];
  return normalizeReviewDraft({...current,operations,cursor:operations.length},draft.base_mask_sha256);
}

export function selectedInstanceAt(labels, mask, point) {
  if (!(labels instanceof Uint32Array) || labels.length !== mask.shape[0]*mask.shape[1] || !Number.isFinite(point?.x) || !Number.isFinite(point?.y)) return 0;
  const x = Math.floor(point.x - mask.origin[0]), y = Math.floor(point.y - mask.origin[1]);
  return x < 0 || y < 0 || x >= mask.shape[1] || y >= mask.shape[0] ? 0 : labels[y*mask.shape[1]+x];
}

export function reviewSignature(draft) {
  return JSON.stringify([draft.base_mask_sha256,draft.cursor,draft.operations]);
}

export async function decodeReviewMask(mask) {
  if (mask?.encoding !== 'uint32-le-zlib-base64-v1' || !Array.isArray(mask.shape) || mask.shape.length !== 2 || mask.shape.some(value => !Number.isInteger(value) || value < 1) || mask.shape[0]*mask.shape[1] > MAX_PIXELS || mask.nbytes !== mask.shape[0]*mask.shape[1]*4) throw new Error('实例标签几何超出本地复核范围。');
  if (typeof mask.data !== 'string' || mask.data.length > 2796204) throw new Error('实例标签压缩内容超过 2 MiB 上限。');
  const binary = atob(mask.data), bytes = new Uint8Array(binary.length);
  for (let index=0;index<binary.length;index++) bytes[index]=binary.charCodeAt(index);
  const reader = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate')).getReader();
  const output = new Uint8Array(mask.nbytes);let offset=0;
  try {
    for (;;) {
      const {value,done}=await reader.read();if(done)break;
      if(offset+value.byteLength>output.byteLength)throw new Error('标签解压长度超出原图范围。');
      output.set(value,offset);offset+=value.byteLength;
    }
  } catch(error) {await reader.cancel().catch(()=>{});throw error;}
  if(offset!==output.byteLength)throw new Error('实例标签内容不完整。');
  const endian=new Uint8Array(new Uint32Array([1]).buffer)[0]===1;
  if(endian)return new Uint32Array(output.buffer);
  const view=new DataView(output.buffer),labels=new Uint32Array(mask.shape[0]*mask.shape[1]);
  for(let index=0;index<labels.length;index++)labels[index]=view.getUint32(index*4,true);
  return labels;
}

function context(selection) {
  const result=selection?.result,record=selection?.record;
  if(!deps || !result?.instance_mask || !record?.id || !deps.state.project)return null;
  const key=`${deps.state.project.id}:${record.id}:${result.instance_mask.sha256}`;
  let session=sessions.get(key);
  if(!session) {
    session={key,recordId:record.id,projectId:deps.state.project.id,open:false,selected:new Set(),mode:'select',seeds:[],polygon:[],zoom:1,error:'',pending:false,saving:false,paused:false,decodePending:false,result:null,labels:null,maskHash:null,allowDisconnected:false,controller:null,signature:null};
    sessions.set(key,session);
    while(sessions.size>2){const old=sessions.keys().next().value;if(old===key)break;sessions.get(old).controller?.abort();sessions.delete(old);}
  }
  try {
    const draft=normalizeReviewDraft(deps.state.project.settings.instance_reviews?.[record.id],result.instance_mask.sha256),signature=reviewSignature(draft);
    if(session.invalid)session.error='';session.invalid=false;
    if(session.signature!==signature) {session.signature=signature;session.draft=draft;session.result=draft.cursor===0?result:null;session.labels=null;session.maskHash=null;session.selected.clear();session.seeds=[];session.polygon=[];session.paused=false;session.error='';}
  } catch(error) {session.error=error.message;session.invalid=true;}
  return session;
}

function selectedContext() {
  const selection=deps.currentSelection();return {selection,session:context(selection)};
}

function currentMask(session,selection) {return (session.result || selection.result).instance_mask;}

export function reviewedInstanceResult(selection) {
  const session=context(selection);
  return session?.result && session.draft?.cursor>0 ? session.result : selection?.result;
}

function sourceFor(selection) {
  const params=selection.record.params || {},details=selection.result.method || {};
  const id=params.fiber_channel_id || params.fiber_channel;
  const channel=params.channels?.find(item=>item.id===id) || params.channels?.[0];
  return details.source_channel?.source || details.source || channel?.source || params.source || {asset_id:params.asset_id,series:params.series || 0,frame:params.frame ?? params.page ?? 0,channel:params.channel || 0};
}

function requestFor(selection,session,draft) {
  return {project_id:deps.state.project.id,revision:deps.state.project.revision,measurement_id:selection.record.id,
    base_mask_sha256:selection.result.instance_mask.sha256,operations:draft.operations,cursor:draft.cursor};
}

async function decodeCurrent(session,selection) {
  if(session.disposed)return;
  const mask=currentMask(session,selection);
  if(session.maskHash===mask.sha256 || session.decodePending)return;
  session.decodePending=true;const key=mask.sha256;
  try {const labels=await decodeReviewMask(mask);if(!session.disposed&&currentMask(session,selection).sha256===key){session.labels=labels;session.maskHash=key;}else labels.fill(0);}
  catch(error){if(!session.disposed)session.error=error.message;}
  finally{session.decodePending=false;if(!session.disposed)deps.emit();}
}

async function runReview(session,selection,draft,{commit=false}={}) {
  if(session.disposed || session.pending || selection.stale || !selection.persisted || session.invalid)return false;
  const sourceSignature=session.signature;
  const recipeUnchanged=()=>reviewSignature(normalizeReviewDraft(deps.state.project.settings.instance_reviews?.[selection.record.id],selection.result.instance_mask.sha256))===sourceSignature;
  session.pending=true;session.paused=false;session.error='';session.controller=new AbortController();deps.emit();
  try {
    await deps.flushSave();
    if(session.controller.signal.aborted)return false;
    const current=deps.currentSelection();
    if(current.record?.id!==selection.record.id || current.stale || deps.state.project.id!==session.projectId)throw new Error('原始来源或分析参数已经改变，请回到对应结果后复核。');
    if(!recipeUnchanged())throw new Error('保存期间复核记录已改变，已保留当前修订；请重新操作。');
    const body=requestFor(selection,session,draft),response=await deps.request('/api/instances/review',{method:'POST',body,signal:session.controller.signal});
    const result=response.result || response;
    if(session.controller.signal.aborted)return false;
    if(deps.state.project.id!==body.project_id || deps.state.project.revision!==body.revision || deps.currentSelection().record?.id!==body.measurement_id || deps.currentSelection().stale)throw new Error('计算期间项目或来源发生改变，未写入本次修订。');
    // A local undo can change the recipe before autosave advances its revision.
    // Such a change is authoritative even when the server revision is unchanged.
    if(!recipeUnchanged())throw new Error('计算期间复核记录已改变，已保留当前修订，未写入过期结果。');
    if(result.instance_review?.base_mask_sha256!==body.base_mask_sha256 || result.instance_review?.cursor!==draft.cursor || !result.instance_mask)throw new Error('服务返回的复核结果与请求不一致。');
    session.signature=reviewSignature(draft);session.draft=copy(draft);session.result=result;session.labels=null;session.maskHash=null;session.selected.clear();session.seeds=[];session.polygon=[];session.mode='select';
    const output=result.instance_review.operations.at(-1)?.output_ids || [];for(const id of output)session.selected.add(id);
    if(commit)deps.mutate(project=>{project.settings.instance_reviews ||= {};project.settings.instance_reviews[selection.record.id]=copy(draft);},'修订分割实例');
    await decodeCurrent(session,selection);return true;
  } catch(error){if(error.name==='AbortError')session.paused=true;else session.error=error.message;return false;}
  finally{session.pending=false;session.controller=null;deps.emit();}
}

function ensureLoaded(session,selection) {
  if(session.disposed || !session.open || session.invalid || selection.stale || !selection.persisted || session.pending || session.paused || session.error)return;
  if(!session.result)queueMicrotask(()=>runReview(session,selection,session.draft));
  else if(!session.labels && !session.decodePending)queueMicrotask(()=>decodeCurrent(session,selection));
}

const columns=[['id','实例 ID'],['area_px','面积 / px²'],['area_um2','面积 / μm²'],['mean_intensity','原始均值'],['integrated_intensity','原始积分'],['min_feret_px','最小 Feret / px'],['min_feret_um','最小 Feret / μm'],['nuclei_count','核数量'],['phenotype','分型']];
const display=value=>value==null?'—':typeof value==='number'?Number.isInteger(value)?String(value):Number(value.toPrecision(6)).toString():String(value);

export function renderInstanceReview(selection) {
  if(!deps || !selection?.result || selection.record?.kind!=='microscopy')return '';
  if(selection.result.instance_review_unavailable)return `<section class="analysis-card instance-review"><h3>实例复核</h3><p class="instance-error">${escape(selection.result.instance_review_unavailable)}</p></section>`;
  const method=selection.record.params?.method || selection.result.method?.name;
  if(!selection.result.instance_mask && ['nuclei','cellpose','fiber_cross_section','myotube'].includes(method)) {
    const submitting=Boolean(deps.state.analysisSubmission),active=isActiveAnalysisJob(deps.state.job);
    return `<section class="analysis-card instance-review"><h3>实例人工复核</h3><p>这份旧分析仅保存轮廓，没有无损实例标签。重新运行分割后可以拆分、合并或补画；旧结果仍保留。</p><button class="button small" data-action="run-analysis" ${submitting?'disabled aria-busy="true"':active?'disabled title="请先完成或取消当前分析任务"':''}>${submitting?'正在提交…':active?'当前任务未结束':'重新运行分割'}</button></section>`;
  }
  const session=context(selection);if(!session)return '';
  const disabled=selection.stale || !selection.persisted || session.invalid;
  const head=`<div class="card-heading"><div><h3>实例人工复核</h3><p>拆分、合并、删除与补画；原始分割保留，测量重新读取原始像素。</p></div><button class="button small" data-instance-action="toggle" ${disabled?'disabled':''}>${session.open?'收起复核':'打开复核工作台'}</button></div>`;
  if(!session.open)return `<section class="analysis-card instance-review">${head}${disabled?'<p class="instance-error">请先保存当前有效分析结果；过期结果不能直接修订。</p>':''}</section>`;
  ensureLoaded(session,selection);
  // A restored recipe is not a reconstructed result. Never label/export the
  // immutable automatic result as the user's saved manual review while replay
  // is pending or failed (including the first render before its microtask).
  if(!session.result)return `<section class="analysis-card instance-review" data-instance-review>${head}<p class="instance-instruction">${session.paused?'恢复已暂停。点击「重试载入」继续恢复已保存的修订。':`正在恢复已保存的 ${session.draft?.cursor ?? 0} 步修订。重新测量完成后才能编辑和导出复核结果。`}</p>${session.error?`<p class="instance-error" role="alert">${escape(session.error)} <button class="button small" data-instance-action="retry">重试载入</button></p>`:session.paused?'<button class="button small" data-instance-action="retry">重试载入</button>':''}${session.pending?'<button class="button small" data-instance-action="cancel">取消等待</button>':''}<div class="instance-downloads"><button class="button small" data-instance-action="csv" disabled>下载复核后 CSV</button><button class="button small" data-instance-action="mask" disabled>下载实例标签 TIFF</button></div></section>`;
  const result=session.result || selection.result,mask=result.instance_mask,review=result.instance_review;
  const source=sourceFor(selection),query=new URLSearchParams({max_size:'1600',series:String(source.series || 0),frame:String(source.frame || 0),channel:String(source.channel || 0)});
  const url=`/api/assets/${encodeURIComponent(source.asset_id)}/preview?${query}`,scale=Math.max(mask.shape[0],mask.shape[1])/750;
  const objects=result.objects || [],visible=objects.slice(0,1000);for(const item of objects)if(session.selected.has(item.id)&&!visible.includes(item))visible.push(item);
  const contours=visible.map(item=>{const points=item.contour || [],selected=session.selected.has(item.id);return points.length?`<path d="${points.map((point,index)=>`${index?'L':'M'}${point[0]} ${point[1]}`).join('')}Z" fill="${selected?'#ffc55b':'#24c29b'}" fill-opacity="${selected?'.23':'.07'}" stroke="${selected?'#ffc55b':'#2dd5aa'}" stroke-width="${scale*(selected?2.2:1.2)}" pointer-events="none"/>`:'';}).join('');
  const seeds=session.seeds.map((point,index)=>`<circle cx="${point[0]}" cy="${point[1]}" r="${scale*4}" fill="${index?'#f27686':'#ffc55b'}"/>`).join('');
  const draft=session.polygon.length?`<polyline points="${session.polygon.map(point=>point.join(',')).join(' ')}" fill="none" stroke="#ffc55b" stroke-width="${scale*1.5}"/>`:'';
  const button=(action,label,blocked=false)=>`<button class="button small ${session.mode===action?'active':''}" data-instance-action="${action}" ${session.pending||disabled||blocked?'disabled':''}>${label}</button>`;
  const displayed=objects.filter(item=>session.selected.size?session.selected.has(item.id):true).slice(0,50),present=columns.filter(([key])=>displayed.some(row=>row[key]!==undefined));
  const instructions={select:'点击实例以选择或取消选择；支持多选。点击空白区域清除选择。',split:'在同一实例内选择两个种子点，使用分水岭拆分并保留全部像素。',add:'沿边界添加顶点，点击“完成补画”提交新对象；已有对象保持不变。',rectangle:'拖动绘制矩形，释放后提交新对象；矩形不得与已有对象重叠。'};
  return `<section class="analysis-card instance-review" data-instance-review>${head}<div class="instance-summary"><span>自动分割 <b>${review?.original_count ?? selection.result.count ?? 0}</b></span><span>当前实例 <b>${result.count ?? 0}</b></span><span>前景面积 <b>${review?.original_foreground_area_px ?? selection.result.summary?.foreground_area_px ?? selection.result.summary?.total_area_px ?? 0} → ${review?.reviewed_foreground_area_px ?? result.summary?.foreground_area_px ?? result.summary?.total_area_px ?? 0}</b> px²</span><span>修订 ${session.draft.cursor} / ${session.draft.operations.length} 步</span></div>
    <div class="instance-toolbar">${button('select','选择')}${button('split','两点拆分')}${button('merge','合并所选',session.selected.size<2)}${button('delete','删除所选',!session.selected.size)}${button('add','多边形补画')}${button('rectangle','矩形补画')}${session.mode==='add'?button('finish-add','完成补画',session.polygon.length<3):''}${button('undo','撤销',!session.draft.cursor)}${button('redo','重做',session.draft.cursor>=session.draft.operations.length)}${button('reset','回到自动分割',!session.draft.cursor)}${button('zoom-out','−',session.zoom<=1)}<span>${Math.round(session.zoom*100)}%</span>${button('zoom-in','＋',session.zoom>=4)}</div>
    <label class="checkbox-label"><input type="checkbox" data-instance-field="allow-disconnected" ${session.allowDisconnected?'checked':''}>允许合并不相邻区域（不填补间隙，操作记录保留此选择）</label>
    <p class="instance-instruction">${session.pending?(session.saving?'正在保存复核结果…':'正在重放修订并重新测量原始像素…'):session.decodePending?'正在准备无损标签，完成后可点选…':instructions[session.mode]}${session.selected.size?` 已选择：${[...session.selected].join('、')}`:''}${session.pending?'<button class="button small" data-instance-action="cancel">取消等待</button>':''}</p>
    ${session.error?`<p class="instance-error" role="alert">${escape(session.error)} <button class="button small" data-instance-action="retry">重试载入</button></p>`:''}
    <div class="instance-stage"><svg data-instance-canvas viewBox="${mask.origin[0]} ${mask.origin[1]} ${mask.shape[1]} ${mask.shape[0]}" style="width:${session.zoom*100}%;aspect-ratio:${mask.shape[1]}/${mask.shape[0]}" aria-label="实例复核原图，坐标为原始像素"><image href="${escape(url)}" x="0" y="0" width="${mask.source_shape[1]}" height="${mask.source_shape[0]}" preserveAspectRatio="none"/>${contours}${seeds}${draft}<g class="instance-live-geometry"></g></svg></div>
    ${objects.length>1000?'<p class="microcopy">为保持交互流畅，显示前 1000 个及已选对象轮廓；点选仍依据全部无损标签。</p>':''}
    <div class="instance-downloads">${button('csv','下载复核后 CSV')}${button('mask','下载实例标签 TIFF')}<span class="microcopy">标签为 uint32 ROI，TIFF 元数据保留原图坐标偏移；原图不被改写。</span></div>
    ${displayed.length?`<div class="data-table-wrap"><table class="data-table"><thead><tr>${present.map(([,label])=>`<th>${label}</th>`).join('')}</tr></thead><tbody>${displayed.map(row=>`<tr>${present.map(([key])=>`<td>${escape(display(row[key]))}</td>`).join('')}</tr>`).join('')}</tbody></table></div><p class="microcopy">${session.selected.size?'所选对象':'前 50 个对象'}；CSV 导出完整返回明细，完整计数始终显示。</p>`:'<p class="microcopy">当前没有实例；可补画对象，或撤销删除。</p>'}</section>`;
}

async function edit(session,selection,operation) {
  try {const draft=appendReviewOperation(session.draft,{id:operationId(),...operation});await runReview(session,selection,draft,{commit:true});}
  catch(error){session.error=error.message;deps.emit();}
}

function csvFor(result) {
  const rows=result.objects || [],keys=[...new Set(rows.flatMap(row=>Object.keys(row)))].filter(key=>!['contour','bbox'].includes(key));
  const cell=value=>{let text=value==null?'':typeof value==='object'?JSON.stringify(value):String(value);if(typeof value==='string'&&(/^\s*[=+@-]/.test(text)||/^[\t\r]/.test(text)))text=`'${text}`;return `"${text.replaceAll('"','""')}"`;};
  return '\ufeff'+[keys.map(cell).join(','),...rows.map(row=>keys.map(key=>cell(row[key])).join(','))].join('\r\n');
}

async function action(name) {
  const {selection,session}=selectedContext();if(!session)return;
  if(name==='toggle'){session.open=!session.open;deps.emit();return;}
  if(name==='cancel'){session.paused=true;session.controller?.abort();deps.emit();return;}
  if(session.pending || selection.stale || !selection.persisted || session.invalid)return;
  if(name==='retry'){session.error='';session.paused=false;deps.emit();return;}
  if(!session.result)return;
  if(['select','split','add','rectangle'].includes(name)){session.mode=name;session.seeds=[];session.polygon=[];session.error='';deps.emit();return;}
  if(name==='zoom-in'||name==='zoom-out'){session.zoom=Math.min(4,Math.max(1,session.zoom+(name==='zoom-in'?.5:-.5)));deps.emit();return;}
  if(name==='undo'||name==='redo'||name==='reset'){
    const cursor=name==='reset'?0:Math.max(0,Math.min(session.draft.operations.length,session.draft.cursor+(name==='undo'?-1:1)));
    await runReview(session,selection,{...session.draft,cursor},{commit:true});return;
  }
  if(name==='merge'||name==='delete'){
    if(session.selected.size<(name==='merge'?2:1))return;
    await edit(session,selection,{type:name,ids:[...session.selected],...(name==='merge'?{allow_disconnected:session.allowDisconnected}:{})});return;
  }
  if(name==='finish-add'){
    if(session.polygon.length<3)return;await edit(session,selection,{type:'add',region:{type:'polygon',points:copy(session.polygon)}});return;
  }
  if(name==='csv'||name==='mask'){
    session.pending=true;session.saving=true;session.error='';session.controller=new AbortController();deps.emit();
    try {
      if(name==='csv')await deps.saveBlob(new Blob([csvFor(session.result || selection.result)],{type:'text/csv;charset=utf-8'}),`instances_reviewed_${selection.record.id}.csv`,{signal:session.controller.signal});
      else {
        await deps.flushSave();const body=requestFor(selection,session,session.draft);
        const response=await fetch('/api/instances/export',{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:session.controller.signal});
        if(!response.ok){let payload;try{payload=await response.json();}catch{}throw new Error(payload?.detail || '实例标签导出失败');}
        await deps.saveBlob(await response.blob(),`instances_reviewed_${selection.record.id}.tif`,{signal:session.controller.signal});
      }
    }catch(error){if(error.name!=='AbortError')session.error=error.message;else deps.notify('已取消保存','info');}finally{session.pending=false;session.saving=false;session.controller=null;deps.emit();}
  }
}

function pointInSvg(svg,event) {
  const matrix=svg.getScreenCTM();if(!matrix)return null;
  const point=new DOMPoint(event.clientX,event.clientY).matrixTransform(matrix.inverse());
  return {x:Number(point.x.toFixed(4)),y:Number(point.y.toFixed(4))};
}

let rectangle=null;
function pointerDown(event) {
  const svg=event.target.closest?.('[data-instance-canvas]');if(!svg || event.button!==0)return;
  const {selection,session}=selectedContext();if(!session || session.pending || !session.labels || selection.stale || !selection.persisted)return;
  const point=pointInSvg(svg,event);if(!point)return;
  const mask=currentMask(session,selection),inside=point.x>=mask.origin[0]&&point.y>=mask.origin[1]&&point.x<mask.origin[0]+mask.shape[1]&&point.y<mask.origin[1]+mask.shape[0];if(!inside)return;
  event.preventDefault();session.error='';
  if(session.mode==='rectangle') {deps.state.interactionLock=true;rectangle={svg,start:point,end:point,pointerId:event.pointerId,session,selection};svg.setPointerCapture(event.pointerId);return;}
  const id=selectedInstanceAt(session.labels,mask,point);
  if(session.mode==='select') {if(id){if(session.selected.has(id))session.selected.delete(id);else if(session.selected.size<32)session.selected.add(id);else session.error='一次最多选择 32 个实例。';}else session.selected.clear();deps.emit();return;}
  if(session.mode==='add'){if(session.polygon.length>=512){session.error='多边形最多 512 个顶点。';}else session.polygon.push([point.x,point.y]);deps.emit();return;}
  if(session.mode==='split') {
    if(!id){session.error='拆分种子需位于实例内部。';deps.emit();return;}
    if(!session.seeds.length){session.selected=new Set([id]);session.seeds=[[point.x,point.y]];deps.emit();return;}
    if(!session.selected.has(id)){session.error='第二个种子必须位于同一实例内。';deps.emit();return;}
    const seeds=[session.seeds[0],[point.x,point.y]];void edit(session,selection,{type:'split',instance_id:id,seeds});
  }
}

function moveRectangle(event) {
  if(!rectangle || event.pointerId!==rectangle.pointerId)return;
  const point=pointInSvg(rectangle.svg,event);if(!point)return;
  const mask=currentMask(rectangle.session,rectangle.selection);
  rectangle.end={x:Math.max(mask.origin[0],Math.min(mask.origin[0]+mask.shape[1],point.x)),y:Math.max(mask.origin[1],Math.min(mask.origin[1]+mask.shape[0],point.y))};
  const {start,end}=rectangle,group=rectangle.svg.querySelector('.instance-live-geometry'),scale=Math.max(...mask.shape)/750;
  if(group)group.innerHTML=`<rect x="${Math.min(start.x,end.x)}" y="${Math.min(start.y,end.y)}" width="${Math.abs(end.x-start.x)}" height="${Math.abs(end.y-start.y)}" fill="#ffc55b" fill-opacity=".15" stroke="#ffc55b" stroke-width="${scale*1.5}"/>`;
}

function finishRectangle(event,cancelled=false) {
  if(!rectangle || event.pointerId!==rectangle.pointerId)return;
  moveRectangle(event);const finished=rectangle;rectangle=null;deps.state.interactionLock=false;
  if(cancelled){deps.emit();return;}
  const {start,end,session,selection}=finished,width=Math.abs(end.x-start.x),height=Math.abs(end.y-start.y);
  if(width<1||height<1){session.error='补画矩形至少需要覆盖一个原始像素。';deps.emit();return;}
  void edit(session,selection,{type:'add',region:{type:'rectangle',x:Math.min(start.x,end.x),y:Math.min(start.y,end.y),width,height}});
}

export function bindInstanceReview(dependencies) {
  deps=dependencies;
  if(bound || typeof document==='undefined')return;bound=true;
  document.addEventListener('click',event=>{const element=event.target.closest('[data-instance-action]');if(element && !element.disabled)void action(element.dataset.instanceAction);});
  document.addEventListener('change',event=>{if(event.target.matches('[data-instance-field="allow-disconnected"]')){const {session}=selectedContext();if(session)session.allowDisconnected=event.target.checked;}});
  document.addEventListener('pointerdown',pointerDown);
  document.addEventListener('pointermove',moveRectangle);
  document.addEventListener('pointerup',event=>finishRectangle(event));
  document.addEventListener('pointercancel',event=>finishRectangle(event,true));
  document.addEventListener('keydown',event=>{if(event.key!=='Escape')return;const {session}=selectedContext();if(session?.open){session.mode='select';session.seeds=[];session.polygon=[];if(rectangle)deps.state.interactionLock=false;rectangle=null;deps.emit();}});
}
