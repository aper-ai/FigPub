/** Binary output encoders, deliberately independent of the DOM/Canvas for validation. */
import {crc32, LIMITS} from './archive.js';

export function pngWithDensity(raw, dpi) {
  if (![150, 300, 600].includes(Number(dpi))) throw new Error('导出 DPI 须为 150、300 或 600');
  if (raw.length < 20 || ![137, 80, 78, 71, 13, 10, 26, 10].every((value, index) => raw[index] === value)) throw new Error('PNG 编码结果无效');
  const view = new DataView(raw.buffer, raw.byteOffset, raw.byteLength), chunks = [raw.subarray(0, 8)], physical = new Uint8Array(21), pv = new DataView(physical.buffer);
  pv.setUint32(0, 9); physical.set(new TextEncoder().encode('pHYs'), 4); pv.setUint32(8, Math.round(dpi / .0254)); pv.setUint32(12, Math.round(dpi / .0254)); physical[16] = 1; pv.setUint32(17, crc32(physical.subarray(4, 17)));
  let inserted = false, ended = false;
  for (let position = 8; position < raw.length;) {
    if (position + 12 > raw.length) throw new Error('PNG 数据块损坏'); const length = view.getUint32(position), type = new TextDecoder().decode(raw.subarray(position + 4, position + 8));
    if (position + length + 12 > raw.length || crc32(raw.subarray(position + 4, position + 8 + length)) !== view.getUint32(position + 8 + length)) throw new Error('PNG 数据块校验失败');
    if (type === 'IDAT' && !inserted) { chunks.push(physical); inserted = true; } if (type !== 'pHYs') chunks.push(raw.subarray(position, position + length + 12)); position += length + 12;
    if (type === 'IEND') { ended = true; if (position !== raw.length) throw new Error('PNG 文件结尾无效'); }
  }
  if (!inserted || !ended) throw new Error('PNG 图像数据缺失'); return new Blob(chunks, {type: 'image/png'});
}

/** Losslessly embed a single opaque RGB raster at an exact physical page size. */
export async function encodeRasterPdf(rgb, width, height, widthMm, heightMm) {
  if (!(rgb instanceof Uint8Array) || !Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1 || width * height > LIMITS.pixels || rgb.length !== width * height * 3 || !Number.isFinite(widthMm) || !Number.isFinite(heightMm) || widthMm <= 0 || heightMm <= 0 || widthMm > 1000 || heightMm > 1000) throw new Error('PDF 页面尺寸或像素数据无效');
  if (typeof CompressionStream === 'undefined') throw new Error('当前浏览器不支持无损 PDF 编码，请用新版 Chrome / Edge 或导出 PNG。');
  const compressed = new Uint8Array(await new Response(new Blob([rgb]).stream().pipeThrough(new CompressionStream('deflate'))).arrayBuffer());
  const encoder = new TextEncoder(), parts = [], offsets = [0]; let length = 0;
  const add = value => { const bytes = typeof value === 'string' ? encoder.encode(value) : value; parts.push(bytes); length += bytes.length; };
  const object = (id, content) => { offsets[id] = length; add(`${id} 0 obj\n${content}\nendobj\n`); };
  const pageWidth = widthMm * 72 / 25.4, pageHeight = heightMm * 72 / 25.4;
  add('%PDF-1.4\n% Figure Workbench Browser Studio - raster PDF\n'); object(1, '<< /Type /Catalog /Pages 2 0 R >>'); object(2, '<< /Type /Pages /Kids [3 0 R] /Count 1 >>');
  object(3, `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${pageWidth.toFixed(5)} ${pageHeight.toFixed(5)}] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>`);
  offsets[4] = length; add(`4 0 obj\n<< /Type /XObject /Subtype /Image /Width ${width} /Height ${height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length ${compressed.length} >>\nstream\n`); add(compressed); add('\nendstream\nendobj\n');
  const content = `q\n${pageWidth.toFixed(5)} 0 0 ${pageHeight.toFixed(5)} 0 0 cm\n/Im0 Do\nQ\n`; object(5, `<< /Length ${encoder.encode(content).length} >>\nstream\n${content}endstream`);
  const xref = length; add('xref\n0 6\n0000000000 65535 f \n'); for (let index = 1; index <= 5; index++) add(`${String(offsets[index]).padStart(10, '0')} 00000 n \n`); add(`trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`);
  return new Blob(parts, {type: 'application/pdf'});
}
