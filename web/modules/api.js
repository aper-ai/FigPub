/** Local HTTP boundary. Source bytes never leave the local service. */
export class ApiError extends Error {
  constructor(message, status, detail) { super(message); this.name = 'ApiError'; this.status = status; this.detail = detail; }
}

function errorMessage(detail, status) {
  if (Array.isArray(detail)) {
    const fields = detail.map(item => {
      if (!item || typeof item !== 'object') return String(item);
      const field = (item.loc || []).filter(part => !['body', 'query'].includes(part)).join('.');
      return `${field ? `${field}：` : ''}${item.msg || '输入内容无效'}`;
    });
    return `请检查输入：${fields.join('；')}`;
  }
  if (typeof detail === 'string' && detail.trim() && !/^\s*</.test(detail)) return detail.slice(0, 2000);
  if (status === 413) return '文件超过允许的大小，请减少本次导入的文件数量或大小。';
  if (status === 409) return '项目已被另一项操作更新。请先备份当前项目，再重新打开工作区。';
  if (status >= 500) return '本机计算服务未能完成操作，请查看启动日志中的具体原因后重试。';
  return `操作未完成（${status}），请检查输入后重试。`;
}

async function fetchLocal(path, options) {
  try { return await fetch(path, options); }
  catch (error) {
    if (error.name === 'AbortError') throw error;
    const failure = new ApiError('无法连接本机计算服务。请确认客户端仍在运行，然后重试；当前未保存的编辑仍保留在窗口中。', 0);
    failure.cause = error;
    throw failure;
  }
}

async function readPayload(response) {
  if (!(response.headers.get('content-type') || '').includes('json')) return response.text();
  if (response.status === 204) return null;
  try { return await response.json(); }
  catch {
    throw new ApiError('本机服务返回了不完整的数据，请重试；若仍失败，请重启客户端并查看启动日志。', response.status);
  }
}

export async function request(path, {method = 'GET', body, signal} = {}) {
  const options = {method, signal, credentials: 'same-origin'};
  if (body !== undefined) {
    if (body instanceof FormData) options.body = body;
    else { options.body = JSON.stringify(body); options.headers = {'Content-Type': 'application/json'}; }
  }
  const response = await fetchLocal(path, options);
  const payload = await readPayload(response);
  if (!response.ok) {
    const detail = payload?.detail ?? payload?.error ?? payload?.message ?? payload;
    throw new ApiError(errorMessage(detail, response.status), response.status, detail);
  }
  return payload;
}

export async function importAssets(files) {
  const form = new FormData();
  for (const file of files) form.append('files', file);
  return request('/api/assets', {method: 'POST', body: form});
}
export async function localizationPixels(assetId,signal,source=null) {
  const query=new URLSearchParams({max_size:1024});if(source)for(const key of ['series','frame','channel'])query.set(key,String(source[key] || 0));
  const response=await fetchLocal(`/api/assets/${encodeURIComponent(assetId)}/pixels.bin?${query}`,{signal,credentials:'same-origin'});
  if(!response.ok){const payload=await readPayload(response),detail=payload?.detail??payload?.message??payload;throw new ApiError(errorMessage(detail,response.status),response.status,detail);}
  const buffer=await response.arrayBuffer(),read=name=>Number(response.headers.get(name));
  const width=read('X-Image-Width'),height=read('X-Image-Height');
  if(!width||!height||buffer.byteLength!==width*height*4)throw new Error('图像定位数据尺寸不匹配');
  return {width,height,source_width:read('X-Source-Width'),source_height:read('X-Source-Height'),bit_depth:read('X-Bit-Depth'),data:new Float32Array(buffer)};
}

export async function openProject(file) {
  const form = new FormData(); form.append('file', file);
  return request('/api/project/open', {method: 'POST', body: form});
}

const DESKTOP_DOWNLOAD_TIMEOUT = 10 * 60 * 1000;
const DOWNLOAD_EVENT = 'figure-workbench:download';

function desktopDownloadHost() {
  const location=globalThis.window?.location;
  return location?.protocol==='http:' && location.hostname==='127.0.0.1'
    && new URLSearchParams(location.search).get('desktop')==='1'
    && typeof globalThis.window?.chrome?.webview?.postMessage==='function';
}

function beginDownload(url, filename, {signal,release=()=>{}}={}) {
  if(signal?.aborted){release();return Promise.reject(new DOMException('已取消保存','AbortError'));}
  const anchor=document.createElement('a');anchor.href=url;anchor.download=filename;
  const expectedUrl=anchor.href,native=desktopDownloadHost();
  if(!native){
    try{document.body.append(anchor);anchor.click();}
    finally{anchor.remove();setTimeout(release,30000);}
    return Promise.resolve({filename,delivery:'browser'});
  }
  return new Promise((resolve,reject)=>{
    let settled=false,timer;
    const cleanup=()=>{clearTimeout(timer);window.removeEventListener(DOWNLOAD_EVENT,receive);signal?.removeEventListener('abort',cancel);anchor.remove();release();};
    const finish=(error)=>{if(settled)return;settled=true;cleanup();if(error)reject(error);else resolve({filename,delivery:'saved'});};
    const cancel=()=>{
      try{window.chrome.webview.postMessage({channel:'figure-workbench:download-cancel',url:expectedUrl});}
      catch{finish(new Error('无法向客户端确认取消保存，请检查保存位置。'));}
    };
    const receive=event=>{
      const result=event.detail;
      // Blob URLs are one-use; original-download URLs include a fresh request
      // ID. A delayed event for another operation can never settle this one.
      if(!result || result.url!==expectedUrl)return;
      if(result.status==='completed')finish();
      else if(result.status==='canceled')finish(new DOMException('已取消保存','AbortError'));
      else if(result.status==='error')finish(new Error(result.message || '文件未能保存，请检查保存位置和可用空间后重试。'));
    };
    window.addEventListener(DOWNLOAD_EVENT,receive);
    signal?.addEventListener('abort',cancel,{once:true});
    timer=setTimeout(()=>{
      cancel();finish(new Error('客户端尚未确认保存完成。请检查目标文件或关闭保存对话框后重试。'));
    },DESKTOP_DOWNLOAD_TIMEOUT);
    try{document.body.append(anchor);anchor.click();anchor.remove();}
    catch(error){finish(error);}
  });
}

export async function saveBlob(blob, filename, options={}) {
  const url=URL.createObjectURL(blob);
  return beginDownload(url,filename,{...options,release:()=>URL.revokeObjectURL(url)});
}

export async function downloadOriginal(assetId, filename, options={}) {
  const requestId=crypto.randomUUID();
  return beginDownload(`/api/assets/${encodeURIComponent(assetId)}/original?download=${requestId}`,filename,options);
}

export function downloadMessage(result) {
  return `${result.delivery==='saved'?'已保存文件':'已发起下载'}：${result.filename}`;
}

export async function exportFile({project, format, dpi = 300, outlined_text = false}, signal) {
  const response = await fetchLocal('/api/export', {method: 'POST', signal, headers: {'Content-Type': 'application/json'}, body: JSON.stringify({project, format, dpi, outlined_text})});
  if (!response.ok) {
    const payload = await readPayload(response), detail = payload?.detail ?? payload?.error ?? payload?.message ?? payload;
    throw new ApiError(errorMessage(detail, response.status), response.status, detail);
  }
  const disposition = response.headers.get('content-disposition') || '';
  const extended = disposition.match(/filename\*=UTF-8''([^;]+)/i), plain = disposition.match(/filename="?([^";]+)"?/i);
  const filename = extended ? decodeURIComponent(extended[1]) : plain ? plain[1] : `${project.title || 'figure'}.${format === 'project' ? 'figwork' : format === 'sources' ? 'zip' : format}`;
  const blob = await response.blob(); const saved=await saveBlob(blob, filename, {signal});return {...saved, bytes: blob.size};
}
