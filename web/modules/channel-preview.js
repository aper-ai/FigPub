/** Bounded, deduplicated PNG previews. Abandoned requests never repaint a new recipe. */
function abortableDelay(milliseconds,signal) {
  return new Promise((resolve,reject)=>{
    if(signal?.aborted){reject(new DOMException('Aborted','AbortError'));return;}
    const abort=()=>{clearTimeout(timer);reject(new DOMException('Aborted','AbortError'));},timer=setTimeout(()=>{signal?.removeEventListener('abort',abort);resolve();},milliseconds);
    signal?.addEventListener('abort',abort,{once:true});
  });
}
export async function fetchChannelPreview(payload,signal,{fetcher=fetch,delay=abortableDelay,maxAttempts=4}={}) {
  for(let attempt=0;attempt<maxAttempts;attempt++) {
    if(signal?.aborted)throw new DOMException('Aborted','AbortError');
    const response=await fetcher('/api/channel-view/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),signal});
    if(response.ok)return response.blob();
    let message=await response.text();try{message=JSON.parse(message).detail || message;}catch{}
    if(response.status===429&&attempt<maxAttempts-1){const seconds=Number(response.headers.get('Retry-After'))||1;await delay(Math.max(500,Math.min(3000,seconds*1000+attempt*250)),signal);continue;}
    throw new Error(typeof message==='string'?message:JSON.stringify(message));
  }
  throw new Error('预览未能完成，请重试');
}
export class ChannelPreviewCache {
  constructor({fetcher,onChange=()=>{},createUrl=blob=>URL.createObjectURL(blob),revokeUrl=url=>URL.revokeObjectURL(url),maxEntries=64,maxBytes=128*1024*1024,maxConcurrent=2}={}) {
    this.fetcher=fetcher;this.onChange=onChange;this.createUrl=createUrl;this.revokeUrl=revokeUrl;
    this.maxEntries=maxEntries;this.maxBytes=maxBytes;this.maxConcurrent=maxConcurrent;this.entries=new Map();this.active=0;this.frameKeys=new Set();this.bytes=0;
  }
  beginFrame(){this.frameKeys=new Set();}
  get(payload) {
    const key=JSON.stringify(payload);this.frameKeys.add(key);
    let entry=this.entries.get(key);
    if(entry){this.entries.delete(key);this.entries.set(key,entry);return entry;}
    this.makeRoom(1,0);
    if(this.entries.size>=this.maxEntries)return {key,status:'error',url:null,error:'当前可见预览超过缓存上限，请减少分屏通道或分批查看。'};
    entry={key,payload,status:'queued',url:null,error:null,bytes:0,controller:null};this.entries.set(key,entry);
    this.trim();this.pump();return entry;
  }
  endFrame() {
    for(const [key,entry] of this.entries)if(!this.frameKeys.has(key)&&['queued','loading'].includes(entry.status))this.remove(key);
    this.trim();
  }
  remove(key) {
    const entry=this.entries.get(key);if(!entry)return;
    this.entries.delete(key);entry.controller?.abort();if(entry.url)this.revokeUrl(entry.url);this.bytes-=entry.bytes;
  }
  makeRoom(extraEntries=0,extraBytes=0) {
    for(const key of this.entries.keys()) {
      if(this.entries.size+extraEntries<=this.maxEntries&&this.bytes+extraBytes<=this.maxBytes)break;
      if(!this.frameKeys.has(key))this.remove(key);
    }
  }
  trim(){this.makeRoom();}
  clear(){for(const key of this.entries.keys())this.remove(key);this.frameKeys.clear();}
  retry(){for(const [key,entry] of this.entries)if(entry.status==='error')this.remove(key);}
  pump() {
    while(this.active<this.maxConcurrent) {
      const entry=[...this.entries.values()].find(item=>item.status==='queued');if(!entry)return;
      entry.status='loading';entry.controller=new AbortController();this.active++;
      Promise.resolve().then(()=>this.fetcher(entry.payload,entry.controller.signal)).then(blob=>{
        if(this.entries.get(entry.key)!==entry)return;
        // Charge a conservative decoded RGBA surface as well as the compressed PNG.
        const size=Number(entry.payload.max_size)||1600,bytes=blob.size+size*size*4;
        this.makeRoom(0,bytes);
        if(this.bytes+bytes>this.maxBytes)throw new Error('预览内存已达到上限。请缩小分屏数量后重试；原始数据未受影响。');
        entry.url=this.createUrl(blob);entry.bytes=bytes;this.bytes+=bytes;entry.status='ready';
      }).catch(error=>{
        if(this.entries.get(entry.key)!==entry)return;
        if(error.name==='AbortError')this.remove(entry.key);else {entry.status='error';entry.error=error.message || '预览生成失败';}
      }).finally(()=>{this.active--;this.trim();this.pump();if(this.entries.get(entry.key)===entry)this.onChange();});
    }
  }
}
