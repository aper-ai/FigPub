const DB_NAME = 'figure-workbench-browser-studio-v1';
let connection, pending = Promise.resolve(), saved = new Set(), observedEpoch = null;
const EPOCH_KEY = 'workspace-epoch';
function readEpoch(value) {
  if(value===undefined)return 0;
  if(!Number.isSafeInteger(value)||value<0)throw new Error('本地保存记录损坏。请先保存项目文件，再在浏览器设置中清除此应用的站点数据。');
  return value;
}
function nextEpoch(value) {if(value>=Number.MAX_SAFE_INTEGER)throw new Error('本地保存记录无法更新。请先保存项目文件，再在浏览器设置中清除此应用的站点数据。');return value+1;}
function staleWorkspace() {
  saved.clear();
  return Object.assign(new Error('本地工作区已被其他窗口清理或替换。当前内容仍保留在此窗口；请先保存项目文件，再重新打开应用或新建工作区。'),{code:'workspace_generation_changed'});
}
function database() {
  if (!connection) connection = new Promise((resolve, reject) => {
    if (!globalThis.indexedDB) { reject(new Error('当前浏览器没有本地数据库')); return; }
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => { const db = request.result; db.createObjectStore('workspace'); db.createObjectStore('originals'); };
    request.onsuccess = () => { const db=request.result; db.onversionchange=()=>{db.close();connection=null;}; resolve(db); };
    request.onerror = () => reject(request.error); request.onblocked = () => reject(new Error('本地数据库被其他窗口占用'));
  }).catch(error=>{connection=null;throw error;});
  return connection;
}
const complete = transaction => new Promise((resolve, reject) => { transaction.oncomplete = resolve; transaction.onerror = () => reject(transaction.error); transaction.onabort = () => reject(transaction.error || new Error('本地保存已中止')); });
const result = request => new Promise((resolve, reject) => { request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); });
// Every operation joins the same queue. Clearing waits for older saves, and a
// subsequent save cannot overlap or accidentally run before the clear commits.
function serialize(operation) { pending=pending.catch(()=>{}).then(operation);return pending; }
async function commit(transaction,operation) {
  const done=complete(transaction);
  try{await operation();await done;}
  catch(error){try{transaction.abort();}catch{}await done.catch(()=>{});throw error;}
}
export function getWorkspaceStatus() {
  return serialize(async()=>{
    const db=await database(),transaction=db.transaction(['workspace','originals'],'readonly');
    const workspace=transaction.objectStore('workspace');
    const [project,count,rawEpoch]=await Promise.all([result(workspace.get('active')),result(transaction.objectStore('originals').count()),result(workspace.get(EPOCH_KEY))]);
    const epoch=readEpoch(rawEpoch);if(observedEpoch===null)observedEpoch=epoch;
    if(observedEpoch!==epoch)saved.clear();
    return {has_recovery:Boolean(project&&(['assets','panels','annotations'].some(key=>project[key]?.length)||project.revision>0)),stored_originals:count,changed_elsewhere:observedEpoch!==epoch};
  });
}
export function loadWorkspace() {
  return serialize(async()=>{
    // Keep the project, generation and every original in one read transaction.
    // A clear in another tab cannot split this recovery into mixed generations.
    const db=await database(),transaction=db.transaction(['workspace','originals'],'readonly'),workspace=transaction.objectStore('workspace');
    const [project,rawEpoch]=await Promise.all([result(workspace.get('active')),result(workspace.get(EPOCH_KEY))]);
    const epoch=readEpoch(rawEpoch);
    if(!project){observedEpoch=epoch;saved.clear();return null;}
    const originals=[],store=transaction.objectStore('originals');
    await Promise.all(project.assets.map(asset=>result(store.get(asset.id)).then(source=>{if(!source)throw new Error('本地原件缺失，请打开已下载的 .figwork 备份，或清除本地数据');originals.push({asset,name:asset.name,bytes:new Uint8Array(source.bytes)});})));
    observedEpoch=epoch;saved=new Set(project.assets.map(asset=>asset.id));return {project,originals};
  });
}
export function saveWorkspace(project, sources, {startNewGeneration=false}={}) {
  const snapshot = structuredClone(project);
  return serialize(async () => {
    const db=await database(),transaction=db.transaction(['workspace','originals'],'readwrite'),originals=transaction.objectStore('originals'),workspace=transaction.objectStore('workspace'),added=[];
    let committedEpoch;
    await commit(transaction,async()=>{
      const epoch=readEpoch(await result(workspace.get(EPOCH_KEY)));
      if(!startNewGeneration&&observedEpoch!==null&&observedEpoch!==epoch)throw staleWorkspace();
      committedEpoch=startNewGeneration?nextEpoch(epoch):epoch;
      const known=startNewGeneration?new Set():saved;
      for(const asset of snapshot.assets)if(!known.has(asset.id)){
        const source=sources.get(asset.id);if(!source)throw new Error('原件缺失，未自动保存');
        originals.put({bytes:source.bytes,mime:source.mime,name:asset.name},asset.id);added.push(asset.id);
      }
      if(startNewGeneration)workspace.put(committedEpoch,EPOCH_KEY);
      workspace.put(snapshot,'active');
    });
    observedEpoch=committedEpoch;if(startNewGeneration)saved.clear();for(const id of added)saved.add(id);
  });
}
export function clearWorkspace() {
  return serialize(async()=>{
    const db=await database(),transaction=db.transaction(['workspace','originals'],'readwrite'),workspace=transaction.objectStore('workspace');
    let clearedEpoch;
    await commit(transaction,async()=>{
      clearedEpoch=nextEpoch(readEpoch(await result(workspace.get(EPOCH_KEY))));
      workspace.clear();transaction.objectStore('originals').clear();workspace.put(clearedEpoch,EPOCH_KEY);
    });
    observedEpoch=clearedEpoch;saved.clear();
  });
}
