/** Portable .figwork archives. Original bytes and project JSON are SHA-256 verified. */
export const LIMITS = Object.freeze({fileBytes: 64 * 1024 * 1024, totalBytes: 192 * 1024 * 1024, metadataBytes: 8 * 1024 * 1024, assets: 256, panels: 500, pixels: 40_000_000});
const encoder = new TextEncoder(), decoder = new TextDecoder('utf-8', {fatal: true});
const crcTable = Uint32Array.from({length: 256}, (_, value) => {
  for (let bit = 0; bit < 8; bit++) value = value & 1 ? 0xedb88320 ^ (value >>> 1) : value >>> 1;
  return value >>> 0;
});
export function crc32(bytes) { let crc = 0xffffffff; for (const byte of bytes) crc = crcTable[(crc ^ byte) & 255] ^ (crc >>> 8); return (crc ^ 0xffffffff) >>> 0; }
export async function sha256(bytes) {
  if (!globalThis.crypto?.subtle) throw new Error('此浏览器没有安全哈希功能。请使用新版 Chrome / Edge，或通过 HTTPS 打开。');
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(byte => byte.toString(16).padStart(2, '0')).join('');
}
const safeName = name => typeof name === 'string' && name.length < 400 && !name.startsWith('/') && !name.includes('\\') && !name.split('/').some(part => part === '..' || part === '.');
function header(size) { const bytes = new Uint8Array(size); return {bytes, view: new DataView(bytes.buffer)}; }
export function zipStore(entries) {
  if (entries.length > 1024) throw new Error('项目文件数量超过上限');
  const parts = [], directory = []; let offset = 0, expanded = 0;
  for (const {name, bytes} of entries) {
    if (!safeName(name)) throw new Error('项目文件路径无效');
    expanded += bytes.length; if (expanded > LIMITS.totalBytes) throw new Error('浏览器项目累计数据超过 192 MiB');
    const filename = encoder.encode(name), checksum = crc32(bytes), local = header(30), central = header(46);
    local.view.setUint32(0, 0x04034b50, true); local.view.setUint16(4, 20, true); local.view.setUint16(6, 0x800, true);
    local.view.setUint32(14, checksum, true); local.view.setUint32(18, bytes.length, true); local.view.setUint32(22, bytes.length, true); local.view.setUint16(26, filename.length, true);
    central.view.setUint32(0, 0x02014b50, true); central.view.setUint16(4, 20, true); central.view.setUint16(6, 20, true); central.view.setUint16(8, 0x800, true);
    central.view.setUint32(16, checksum, true); central.view.setUint32(20, bytes.length, true); central.view.setUint32(24, bytes.length, true); central.view.setUint16(28, filename.length, true); central.view.setUint32(42, offset, true);
    parts.push(local.bytes, filename, bytes); directory.push(central.bytes, filename); offset += 30 + filename.length + bytes.length;
  }
  const length = directory.reduce((sum, bytes) => sum + bytes.length, 0), end = header(22);
  end.view.setUint32(0, 0x06054b50, true); end.view.setUint16(8, entries.length, true); end.view.setUint16(10, entries.length, true); end.view.setUint32(12, length, true); end.view.setUint32(16, offset, true);
  return new Blob([...parts, ...directory, end.bytes], {type: 'application/zip'});
}
async function inflateLimited(bytes, expected) {
  if (typeof DecompressionStream === 'undefined') throw new Error('浏览器不支持读取压缩项目，请使用新版 Chrome / Edge。');
  const reader = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate-raw')).getReader();
  const chunks = []; let length = 0;
  try { while (true) { const {value, done} = await reader.read(); if (done) break; length += value.length; if (length > expected) throw new Error('项目解压大小与清单不一致'); chunks.push(value); } }
  catch (error) { await reader.cancel().catch(() => {}); throw error; }
  if (length !== expected) throw new Error('项目解压大小与清单不一致');
  const result = new Uint8Array(length); let offset = 0; for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.length; } return result;
}
export async function unzipVerified(bytes) {
  if (bytes.byteLength > LIMITS.totalBytes) throw new Error('浏览器项目压缩包超过 192 MiB');
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength); let end = -1;
  for (let index = bytes.length - 22; index >= Math.max(0, bytes.length - 65557); index--) if (view.getUint32(index, true) === 0x06054b50 && index + 22 + view.getUint16(index + 20, true) === bytes.length) { end = index; break; }
  if (end < 0 || view.getUint16(end + 4, true) || view.getUint16(end + 6, true)) throw new Error('不是受支持的单卷 .figwork 项目');
  const count = view.getUint16(end + 10, true), directorySize = view.getUint32(end + 12, true), directoryOffset = view.getUint32(end + 16, true);
  if (count > 1024 || count !== view.getUint16(end + 8, true) || directoryOffset + directorySize !== end) throw new Error('项目目录无效');
  const records = [], names = new Set(); let position = directoryOffset, total = 0;
  for (let index = 0; index < count; index++) {
    if (position + 46 > end || view.getUint32(position, true) !== 0x02014b50) throw new Error('项目目录损坏');
    const flags = view.getUint16(position + 8, true), method = view.getUint16(position + 10, true), compressed = view.getUint32(position + 20, true), size = view.getUint32(position + 24, true), nameLength = view.getUint16(position + 28, true), extraLength = view.getUint16(position + 30, true), commentLength = view.getUint16(position + 32, true), localOffset = view.getUint32(position + 42, true);
    const name = decoder.decode(bytes.subarray(position + 46, position + 46 + nameLength));
    if (!safeName(name) || names.has(name) || flags & 1 || ![0, 8].includes(method) || size > LIMITS.fileBytes || (total += size) > LIMITS.totalBytes) throw new Error('项目含不安全路径、重复文件或超过浏览器资源上限');
    if (localOffset + 30 > directoryOffset || view.getUint32(localOffset, true) !== 0x04034b50) throw new Error('项目文件头损坏');
    const localNameLength = view.getUint16(localOffset + 26, true), localExtraLength = view.getUint16(localOffset + 28, true), start = localOffset + 30 + localNameLength + localExtraLength;
    if (start + compressed > directoryOffset || decoder.decode(bytes.subarray(localOffset + 30, localOffset + 30 + localNameLength)) !== name || view.getUint16(localOffset + 8, true) !== method) throw new Error('项目文件头与目录不一致');
    records.push({name, method, compressed, size, start, crc: view.getUint32(position + 16, true)}); names.add(name); position += 46 + nameLength + extraLength + commentLength;
  }
  if (position !== end) throw new Error('项目目录长度不一致');
  const files = new Map();
  for (const record of records) {
    const encoded = bytes.subarray(record.start, record.start + record.compressed), data = record.method === 0 ? encoded.slice() : await inflateLimited(encoded, record.size);
    if (data.length !== record.size || crc32(data) !== record.crc) throw new Error(`项目文件校验失败：${record.name}`);
    if (!record.name.endsWith('/')) files.set(record.name, data);
  }
  return files;
}
export async function encodeProject(project, sources) {
  const clean = structuredClone(project); for (const asset of clean.assets) asset.preview_url = '';
  const payload = encoder.encode(JSON.stringify(clean)), records = [], entries = [{name: 'project.json', bytes: payload}];
  if (payload.length > LIMITS.metadataBytes) throw new Error('项目元数据超过 8 MiB');
  for (const asset of clean.assets) {
    const source = sources.get(asset.id); if (!source || await sha256(source.bytes) !== asset.id) throw new Error('原件缺失或哈希校验失败，未导出项目');
    const extension = asset.name.match(/\.[A-Za-z0-9]+$/)?.[0].toLowerCase() || source.extension, name = `originals/${asset.id}${extension}`;
    entries.push({name, bytes: source.bytes}); records.push({id: asset.id, path: name, name: asset.name, sha256: asset.id, size_bytes: source.bytes.length});
  }
  entries.push({name: 'manifest.json', bytes: encoder.encode(JSON.stringify({format: 'figwork', version: 1, project_sha256: await sha256(payload), assets: records}))});
  return zipStore(entries);
}
export async function decodeProject(bytes) {
  const files = await unzipVerified(bytes), raw = files.get('project.json'), rawManifest = files.get('manifest.json');
  if (!raw || !rawManifest || raw.length > LIMITS.metadataBytes || rawManifest.length > 2 * 1024 * 1024) throw new Error('项目元数据缺失或超限');
  const project = JSON.parse(decoder.decode(raw)), manifest = JSON.parse(decoder.decode(rawManifest));
  if (manifest?.format !== 'figwork' || manifest.version !== 1 || !Array.isArray(manifest.assets) || manifest.assets.length > LIMITS.assets || await sha256(raw) !== manifest.project_sha256 || !Array.isArray(project?.assets)) throw new Error('项目格式或项目 SHA-256 校验失败');
  const expected = new Map(project.assets.map(asset => [asset.id, asset])), seen = new Set(), originals = [], declared = new Set(['project.json', 'manifest.json']);
  if (expected.size !== project.assets.length) throw new Error('项目包含重复素材身份');
  for (const record of manifest.assets) {
    const data = files.get(record?.path), asset = expected.get(record?.id);
    if (!data || !asset || seen.has(record.id) || !record.path.startsWith('originals/') || record.sha256 !== record.id || asset.sha256 !== record.id || record.size_bytes !== data.length || await sha256(data) !== record.id) throw new Error('项目原件大小、身份或 SHA-256 校验失败');
    const suffix = name => String(name).match(/\.[A-Za-z0-9]+$/)?.[0].toLowerCase();
    if (suffix(record.name) !== suffix(record.path) || suffix(asset.name) !== suffix(record.path)) throw new Error('原件扩展名与清单不一致');
    originals.push({asset, bytes: data, name: record.name}); seen.add(record.id); declared.add(record.path);
  }
  if (seen.size !== expected.size || [...files.keys()].some(name => !declared.has(name))) throw new Error('项目原件缺失或包含未声明文件');
  return {project, originals};
}
