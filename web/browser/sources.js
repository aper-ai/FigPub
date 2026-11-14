import {LIMITS, sha256} from './archive.js';

export function imageFromUrl(url) { return new Promise((resolve, reject) => { const image = new Image(); image.onload = () => resolve(image); image.onerror = () => reject(new Error('浏览器无法解码这份图像')); image.src = url; }); }
function sniff(bytes) {
  if (bytes[0] === 0x89 && new TextDecoder().decode(bytes.subarray(1, 4)) === 'PNG') return ['image/png', '.png'];
  if (bytes[0] === 0xff && bytes[1] === 0xd8) return ['image/jpeg', '.jpg'];
  if (new TextDecoder().decode(bytes.subarray(0, 4)) === 'RIFF' && new TextDecoder().decode(bytes.subarray(8, 12)) === 'WEBP') return ['image/webp', '.webp'];
  if (/^(?:\s|\uFEFF)*(?:<\?xml[^]*?\?>\s*)?(?:<!--[^]*?-->\s*)?<svg[\s>]/i.test(new TextDecoder().decode(bytes.subarray(0, 4096)))) return ['image/svg+xml', '.svg'];
  throw new Error('浏览器版接受 PNG、JPEG、WebP 与安全 SVG。TIFF、PDF 和多通道原图请使用本机工作台。');
}
function checkSize(width, height) {
  if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0 || width * height > LIMITS.pixels) throw new Error('图像尺寸无效或超过浏览器版 4000 万像素上限');
}
function checkExif(bytes) {
  let start = 0;
  if (new TextDecoder().decode(bytes.subarray(0, 4)) === 'Exif') start = 6;
  if (bytes.length < start + 8) return;
  const view = new DataView(bytes.buffer, bytes.byteOffset + start, bytes.byteLength - start), little = view.getUint16(0) === 0x4949;
  if (!(little || view.getUint16(0) === 0x4d4d) || view.getUint16(2, little) !== 42) return;
  const offset = view.getUint32(4, little); if (offset + 2 > view.byteLength) throw new Error('EXIF 目录损坏');
  const count = view.getUint16(offset, little); if (offset + 2 + count * 12 > view.byteLength) throw new Error('EXIF 目录损坏');
  for (let index = 0; index < count; index++) { const at = offset + 2 + index * 12; if (view.getUint16(at, little) === 0x112) { const orientation = view.getUint16(at + 8, little); if (orientation !== 1) throw new Error('此图含 EXIF 自动旋转信息。请先在图片编辑器中保存为方向已校正的 PNG，再导入以保持项目坐标一致。'); } }
}
/** Read dimensions before decoding, and refuse animated sources rather than silently keeping one frame. */
export function inspectRaster(bytes, mime) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength); let width, height, bitDepth = 8, channels = 3;
  const word = (start, count = 4) => new TextDecoder().decode(bytes.subarray(start, start + count));
  if (mime === 'image/png') {
    if (bytes.length < 33 || word(12) !== 'IHDR' || view.getUint32(8) !== 13) throw new Error('PNG 文件头无效');
    width = view.getUint32(16); height = view.getUint32(20); bitDepth = bytes[24]; channels = ({0: 1, 2: 3, 3: 1, 4: 2, 6: 4})[bytes[25]];
    if (!channels || ![1, 2, 4, 8, 16].includes(bitDepth)) throw new Error('PNG 颜色或位深格式无效');
    for (let at = 8; at + 12 <= bytes.length;) { const size = view.getUint32(at), type = word(at + 4); if (at + 12 + size > bytes.length) throw new Error('PNG 数据块损坏'); if (type === 'acTL') throw new Error('浏览器组图版不接受动画 PNG，请先导出所需静态帧'); if (type === 'eXIf') checkExif(bytes.subarray(at + 8, at + 8 + size)); at += size + 12; if (type === 'IEND') break; }
  } else if (mime === 'image/jpeg') {
    for (let at = 2; at + 4 <= bytes.length;) {
      if (bytes[at++] !== 0xff) throw new Error('JPEG 标记无效'); while (bytes[at] === 0xff) at++;
      const marker = bytes[at++]; if (marker === 0xda || marker === 0xd9) break; if (marker === 1 || marker >= 0xd0 && marker <= 0xd7) continue;
      if (at + 2 > bytes.length) throw new Error('JPEG 数据块损坏'); const size = view.getUint16(at); if (size < 2 || at + size > bytes.length) throw new Error('JPEG 数据块损坏');
      if ([0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf].includes(marker)) { if (size < 8) throw new Error('JPEG 尺寸无效'); bitDepth = bytes[at + 2]; height = view.getUint16(at + 3); width = view.getUint16(at + 5); channels = bytes[at + 7]; }
      if (marker === 0xe1 && word(at + 2) === 'Exif') checkExif(bytes.subarray(at + 2, at + size)); at += size;
    }
  } else if (mime === 'image/webp') {
    for (let at = 12; at + 8 <= bytes.length;) {
      const type = word(at), size = view.getUint32(at + 4, true), data = at + 8; if (data + size > bytes.length) throw new Error('WebP 数据块损坏');
      if (type === 'ANIM' || type === 'ANMF' || type === 'VP8X' && bytes[data] & 2) throw new Error('浏览器组图版不接受动画 WebP，请先导出所需静态帧');
      if (type === 'VP8X' && size >= 10) { width = 1 + bytes[data + 4] + (bytes[data + 5] << 8) + (bytes[data + 6] << 16); height = 1 + bytes[data + 7] + (bytes[data + 8] << 8) + (bytes[data + 9] << 16); channels = bytes[data] & 16 ? 4 : 3; }
      if (width == null && type === 'VP8 ' && size >= 10) { width = view.getUint16(data + 6, true) & 0x3fff; height = view.getUint16(data + 8, true) & 0x3fff; }
      if (width == null && type === 'VP8L' && size >= 5) { const bits = view.getUint32(data + 1, true); width = 1 + (bits & 0x3fff); height = 1 + ((bits >>> 14) & 0x3fff); channels = bits & 0x10000000 ? 4 : 3; }
      if (type === 'EXIF') checkExif(bytes.subarray(data, data + size)); at = data + size + size % 2;
    }
  }
  checkSize(width, height); return {width, height, bitDepth, channels};
}
function unsafeCss(css) {
  // Restrict CSS syntax that can obscure addresses; allow local references and embedded font bytes.
  if (/@(?!font-face\b)|\\|expression\s*\(|javascript\s*:|behavior\s*:|-moz-binding/i.test(css)) return true;
  const clean = css.replace(/\/\*[\s\S]*?\*\//g, '');
  for (const match of clean.matchAll(/url\(\s*([^)]*)\)/gi)) { const address = match[1].trim().replace(/^(['"])([\s\S]*)\1$/, '$2'); if (!/^#[A-Za-z_][\w:.-]*$/.test(address) && !/^data:font\/(?:ttf|otf|woff2?);base64,[A-Za-z0-9+/=]+$/i.test(address)) return true; }
  return /url\s*\(/i.test(clean.replace(/url\([^)]*\)/gi, ''));
}
export function inspectSvg(text, depth = 0) {
  if (depth > 3 || text.length > 48 * 1024 * 1024 || /<!DOCTYPE|<!ENTITY/i.test(text)) throw new Error('SVG 含外部实体、嵌套过深或体积过大');
  const document = new DOMParser().parseFromString(text, 'image/svg+xml'), root = document.documentElement;
  if (root.localName !== 'svg' || document.querySelector('parsererror')) throw new Error('SVG XML 格式无效');
  const forbidden = new Set(['script', 'foreignobject', 'iframe', 'object', 'embed', 'audio', 'video', 'animate', 'animatemotion', 'animatetransform', 'set', 'discard']);
  let embeddedRaster = false, hasText = false, count = 0, embeddedPixels = 0;
  for (const element of [root, ...root.querySelectorAll('*')]) {
    if (++count > 50000 || forbidden.has(element.localName.toLowerCase()) || element.localName.toLowerCase().startsWith('animate') || element.namespaceURI !== 'http://www.w3.org/2000/svg' && !element.closest('metadata')) throw new Error('SVG 含动态、外部或过多元素，无法安全导入');
    if (element.localName === 'text' || element.localName === 'tspan') hasText = true;
    if (element.localName === 'style') {
      const css = element.textContent;
      if (unsafeCss(css)) throw new Error('SVG 样式引用了外部资源');
    }
    for (const attribute of element.attributes) {
      const name = attribute.name.toLowerCase(), value = attribute.value.trim();
      if (name.startsWith('on') || name === 'xml:base') throw new Error('SVG 含脚本事件或外部地址');
      if (['href', 'xlink:href', 'src'].includes(name)) {
        if (value.startsWith('#')) continue;
        const embedded = element.localName === 'image' && value.match(/^data:(image\/(?:png|jpeg|webp|svg\+xml));base64,([A-Za-z0-9+/=\s]+)$/);
        if (embedded) { const binary = atob(embedded[2]), data = Uint8Array.from(binary, character => character.charCodeAt(0)); if (embedded[1] === 'image/svg+xml') { const nested = inspectSvg(new TextDecoder('utf-8', {fatal: true}).decode(data), depth + 1); embeddedRaster ||= nested.embeddedRaster; hasText ||= nested.hasText; embeddedPixels += nested.embeddedPixels; } else { const dimensions = inspectRaster(data, embedded[1]); embeddedPixels += dimensions.width * dimensions.height; embeddedRaster = true; } if (embeddedPixels > LIMITS.pixels) throw new Error('SVG 内嵌位图总像素超过浏览器版 4000 万像素上限'); continue; }
        throw new Error('SVG 只允许内部引用和内嵌 PNG / JPEG / WebP');
      }
      if (unsafeCss(value)) throw new Error('SVG 属性包含外部或活动内容');
    }
  }
  return {embeddedRaster, hasText, embeddedPixels};
}
export async function createSource(bytes, name, expectedId = null) {
  if (!bytes.length || bytes.length > LIMITS.fileBytes) throw new Error('单份原件大小须为 1 字节至 64 MiB');
  const [mime, extension] = sniff(bytes); let svg = {}, raster = null;
  const suffix = String(name || '').match(/\.[A-Za-z0-9]+$/)?.[0].toLowerCase(), allowed = {'image/png': ['.png'], 'image/jpeg': ['.jpg', '.jpeg'], 'image/webp': ['.webp'], 'image/svg+xml': ['.svg']};
  if (suffix && !allowed[mime].includes(suffix)) throw new Error(`文件扩展名与内容不一致，请使用 ${extension} 扩展名后导入`);
  if (mime === 'image/svg+xml') svg = inspectSvg(new TextDecoder('utf-8', {fatal: true}).decode(bytes));
  else raster = inspectRaster(bytes, mime);
  const id = await sha256(bytes); if (expectedId && id !== expectedId) throw new Error('原件 SHA-256 与项目不一致');
  const url = URL.createObjectURL(new Blob([bytes], {type: mime}));
  try {
    const image = await imageFromUrl(url), width = image.naturalWidth, height = image.naturalHeight;
    checkSize(width, height); if (raster && (width !== raster.width || height !== raster.height)) throw new Error('浏览器解码尺寸与原始尺寸不同，请先另存为方向已校正的 PNG');
    const originalName = String(name || 'image'), stem = suffix ? originalName.slice(0, -suffix.length) : originalName, finalExtension = suffix || extension, safeFilename = (stem || 'image').slice(0, 240 - finalExtension.length) + finalExtension;
    const asset = {id, sha256: id, name: safeFilename, kind: mime === 'image/svg+xml' ? 'svg' : 'image', width_px: width, height_px: height, bit_depth: raster?.bitDepth || 0, dtype: raster ? raster.bitDepth > 8 ? 'uint16' : 'uint8' : 'vector', channels: raster?.channels || 0, frames: 1, preview_url: url,
      metadata: {browser_svg_text: !!svg.hasText, browser_embedded_raster: !!svg.embeddedRaster, browser_display_only: true, browser_source_mime: mime}};
    image.src = ''; return {asset, bytes, mime, extension, url};
  } catch (error) { URL.revokeObjectURL(url); throw error; }
}
export function bytesDataUrl(bytes, mime) {
  let binary = ''; for (let offset = 0; offset < bytes.length; offset += 32768) binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768));
  return `data:${mime};base64,${btoa(binary)}`;
}
export function releaseSources(sources) { for (const source of sources.values()) URL.revokeObjectURL(source.url); }
export function sourceBudget(sources) { return [...sources.values()].reduce((sum, source) => sum + source.bytes.length, 0); }
