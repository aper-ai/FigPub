const DB_NAME = 'figure-workbench-browser-studio-v1';
let connection, pending = Promise.resolve(), saved = new Set();
function database() {
  if (!connection) connection = new Promise((resolve, reject) => {
    if (!globalThis.indexedDB) { reject(new Error('当前浏览器没有本地数据库')); return; }
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => { const db = request.result; db.createObjectStore('workspace'); db.createObjectStore('originals'); };
    request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); request.onblocked = () => reject(new Error('本地数据库被其他窗口占用'));
  });
  return connection;
}
const complete = transaction => new Promise((resolve, reject) => { transaction.oncomplete = resolve; transaction.onerror = () => reject(transaction.error); transaction.onabort = () => reject(transaction.error || new Error('本地保存已中止')); });
const result = request => new Promise((resolve, reject) => { request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); });
export async function loadWorkspace() {
  const db = await database(), transaction = db.transaction(['workspace', 'originals'], 'readonly');
  const project = await result(transaction.objectStore('workspace').get('active')); if (!project) return null;
  const originals = []; const sourceTransaction = db.transaction('originals', 'readonly'), store = sourceTransaction.objectStore('originals');
  const reads = project.assets.map(asset => result(store.get(asset.id)).then(source => { if (!source) throw new Error('本地原件缺失，请打开已下载的 .figwork 备份'); originals.push({asset, name: asset.name, bytes: new Uint8Array(source.bytes)}); saved.add(asset.id); }));
  await Promise.all(reads); return {project, originals};
}
export function saveWorkspace(project, sources) {
  const snapshot = structuredClone(project);
  pending = pending.catch(() => {}).then(async () => {
    const db = await database(), transaction = db.transaction(['workspace', 'originals'], 'readwrite'), originals = transaction.objectStore('originals'), added = [];
    for (const asset of snapshot.assets) if (!saved.has(asset.id)) { const source = sources.get(asset.id); if (!source) { transaction.abort(); throw new Error('原件缺失，未自动保存'); } originals.put({bytes: source.bytes, mime: source.mime, name: asset.name}, asset.id); added.push(asset.id); }
    transaction.objectStore('workspace').put(snapshot, 'active'); await complete(transaction); for (const id of added) saved.add(id);
  });
  return pending;
}
export async function clearWorkspace() {
  await pending.catch(() => {}); const db = await database(), transaction = db.transaction(['workspace', 'originals'], 'readwrite');
  transaction.objectStore('workspace').clear(); transaction.objectStore('originals').clear(); await complete(transaction); saved = new Set();
}
