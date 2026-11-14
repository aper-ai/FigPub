import {escapeXml as e, selectedObjects, selectionBounds} from './model.js';
import {bytesDataUrl, imageFromUrl} from './sources.js';
import {LIMITS} from './archive.js';
import {pngWithDensity, encodeRasterPdf} from './encoding.js';
import {textLines} from '../modules/layout-engine.js';

const MM_PER_PT = 25.4 / 72;
const isCjk = text => /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}\u3000-\u303f\uff00-\uffef]/u.test(text);
function covered(codepoint, ranges) { let left = 0, right = ranges.length - 1; while (left <= right) { const middle = (left + right) >>> 1, range = ranges[middle]; if (codepoint < range[0]) right = middle - 1; else if (codepoint > range[1]) left = middle + 1; else return true; } return false; }
export function fontKeyForText(text, bold = false) {
  const maps = globalThis.FW_STUDIO_FONT_COVERAGE, primary = bold ? 'bold' : 'normal'; if (!maps) return isCjk(text) ? 'cjk' : primary;
  const characters = Array.from(String(text || '')).filter(character => !/\s/u.test(character));
  if (characters.every(character => covered(character.codePointAt(0), maps[primary]))) return primary;
  if (characters.every(character => covered(character.codePointAt(0), maps.cjk))) return 'cjk';
  const missing = [...new Set(characters.filter(character => !covered(character.codePointAt(0), maps[primary]) && !covered(character.codePointAt(0), maps.cjk)))].slice(0, 12).join(' ');
  throw new Error(missing ? `内嵌字体不能表示以下字符：${missing}。请换用有字形支持的文本。` : '此文字混合了不同字体支持的字符，请拆分为不同文字对象后导出。');
}
// The editor remains usable for correcting unsupported characters; exports validate them strictly first.
const family = (text, bold = false) => { try { return fontKeyForText(text, bold) === 'cjk' ? 'FW Studio CJK' : 'FW Studio Sans'; } catch { return isCjk(text) ? 'FW Studio CJK' : 'FW Studio Sans'; } };
export function fontIssues(project) {
  const issues = []; for (const [text, bold] of [...project.panels.flatMap(panel => [[panel.label, true], [panel.title, false]]), ...project.annotations.filter(item => item.type === 'text' && !item.hidden).map(item => [item.text, !!item.bold])]) { try { fontKeyForText(text, bold); } catch (error) { issues.push({severity: 'error', code: 'font-missing', message: error.message}); } } return issues;
}
const fonts = new Map(), loadedFonts = new Map();
async function fontAsset(key) {
  if (fonts.has(key)) return fonts.get(key);
  const configurations = {normal: {file: 'DejaVuSans.ttf', family: 'FW Studio Sans', weight: '400', format: 'truetype', mime: 'font/ttf'}, bold: {file: 'DejaVuSans-Bold.ttf', family: 'FW Studio Sans', weight: '700', format: 'truetype', mime: 'font/ttf'}, cjk: {file: 'NotoSansCJK-SC.woff2', family: 'FW Studio CJK', weight: '400 900', format: 'woff2', mime: 'font/woff2'}};
  const configuration = configurations[key], embedded = globalThis.FW_STUDIO_FONTS?.[key]; let url;
  if (embedded) url = `data:${configuration.mime};base64,${embedded}`;
  else { const response = await fetch(`/fonts/${configuration.file}`); if (!response.ok) throw new Error('导出字体不可用。请使用完整单文件浏览器版，或本机工作台入口。'); url = bytesDataUrl(new Uint8Array(await response.arrayBuffer()), configuration.mime); }
  const result = {...configuration, url}; fonts.set(key, result); return result;
}
async function projectFonts(project) {
  const required = new Set(['normal', 'bold']); for (const panel of project.panels) { required.add(fontKeyForText(panel.label, true)); required.add(fontKeyForText(panel.title)); } for (const item of project.annotations.filter(item => item.type === 'text' && !item.hidden)) required.add(fontKeyForText(item.text, !!item.bold)); return Promise.all([...required].map(fontAsset));
}
function svgText(text, {x, y, size, bold = false, anchor = 'start', color = '#202923'}) {
  return `<text x="${x}" y="${y}" font-size="${size}" font-family="${family(text, bold)}" font-weight="${bold ? 700 : 400}" text-anchor="${e(anchor)}" fill="${e(color)}">${textLines(text).map((line, index) => `<tspan x="${x}" dy="${index ? size * 1.25 : 0}">${e(line)}</tspan>`).join('')}</text>`;
}
function fillMultiline(context, text, x, y, size) { textLines(text).forEach((line, index) => context.fillText(line, x, y + index * size * 1.25)); }
export async function prepareFonts(project) {
  const records = await projectFonts(project); await Promise.all(records.map(record => {
    if (!loadedFonts.has(record.file)) loadedFonts.set(record.file, (async () => { const face = new FontFace(record.family, `url("${record.url}")`, {weight: record.weight}); await face.load(); document.fonts.add(face); })()); return loadedFonts.get(record.file);
  })); return records;
}
function scaleBar(panel) {
  const bar = panel.scale_bar; if (!bar) return '';
  const length = bar.length_um / bar.um_per_px / panel.crop.width * panel.width_mm, margin = Math.min(2, panel.width_mm * .03, panel.height_mm * .07);
  if (!(length > 0 && length <= panel.width_mm * .9)) return '';
  return `<g class="scale-bar"><line x1="${panel.width_mm - length - margin}" x2="${panel.width_mm - margin}" y1="${panel.height_mm - margin}" y2="${panel.height_mm - margin}" stroke="${e(bar.color)}" stroke-width=".529167"/><text x="${panel.width_mm - length / 2 - margin}" y="${panel.height_mm - margin - 1.5}" text-anchor="middle" font-family="FW Studio Sans" font-size="2.293057" fill="${e(bar.color)}">${e(bar.length_um)} µm</text></g>`;
}
export function figureMarkup(project, sources, {embedded = false, editor = false, selection = new Set(), fontStyle = ''} = {}) {
  const panels = project.panels.map(panel => {
    const source = sources.get(panel.asset_id), crop = panel.crop, asset = project.assets.find(item => item.id === panel.asset_id), width = panel.width_mm, height = panel.height_mm;
    const url = source ? embedded ? bytesDataUrl(source.bytes, source.mime) : source.url : '', sx = width / crop.width, sy = height / crop.height, clip = `clip-${panel.id}`;
    const image = `<defs><clipPath id="${clip}"><rect width="${width}" height="${height}"/></clipPath></defs><g transform="rotate(${panel.rotation || 0} ${width / 2} ${height / 2})"><g transform="translate(${width / 2} ${height / 2}) scale(${panel.flip_h ? -1 : 1} ${panel.flip_v ? -1 : 1}) translate(${-width / 2} ${-height / 2})" clip-path="url(#${clip})"><image href="${e(url)}" x="${-crop.x * sx}" y="${-crop.y * sy}" width="${asset.width_px * sx}" height="${asset.height_px * sy}" preserveAspectRatio="none"/></g>${scaleBar(panel)}</g>`;
    const captions = svgText(panel.label, {x: 0, y: -1.8, size: (panel.label_font_size_pt || 10) * MM_PER_PT, bold: true}) + svgText(panel.title, {x: width / 2, y: -1.8, size: (panel.title_font_size_pt || 8) * MM_PER_PT, anchor: 'middle'});
    const selected = editor && selection.has(panel.id) ? `<rect class="selection-outline" x="-.4" y="-.4" width="${width + .8}" height="${height + .8}" fill="none" stroke="#087b60" stroke-width=".45" pointer-events="none"/>${selection.size === 1 ? `<rect data-resize="${e(panel.id)}" x="${width - 1.2}" y="${height - 1.2}" width="2.4" height="2.4" fill="white" stroke="#087b60" stroke-width=".35"/>` : ''}` : '';
    return `<g ${editor ? `data-object="${e(panel.id)}" tabindex="0" role="img" aria-label="面板 ${e(panel.label)} ${e(asset.name)}"` : ''} transform="translate(${panel.x_mm} ${panel.y_mm})">${image}${captions}${selected}</g>`;
  }).join('');
  const annotations = project.annotations.filter(item => !item.hidden).map(item => {
    const lineWidth = (item.line_width_pt ?? .8) * MM_PER_PT;
    const content = item.type === 'text' ? svgText(item.text, {x: 0, y: item.font_size_pt * MM_PER_PT, size: item.font_size_pt * MM_PER_PT, bold: !!item.bold, anchor: item.anchor || 'start', color: item.color}) : item.type === 'line' ? `<line x1="0" y1="0" x2="${item.width_mm}" y2="${item.height_mm}" stroke="${e(item.color)}" stroke-width="${lineWidth}"${item.dashed ? ' stroke-dasharray="1.4 .8"' : ''}/>` : `<rect width="${item.width_mm}" height="${item.height_mm}" fill="${e(item.fill || 'none')}" stroke="${e(item.color)}" stroke-width="${lineWidth}"/>`;
    const selected = editor && selection.has(item.id) ? `<rect x="-.5" y="-.5" width="${Math.max(4, item.width_mm) + 1}" height="${Math.max(4, item.height_mm) + 1}" fill="#087b6010" stroke="#087b60" stroke-width=".35"/>` : '';
    return `<g ${editor ? `data-object="${e(item.id)}"` : ''} transform="translate(${item.x_mm} ${item.y_mm})">${content}${selected}</g>`;
  }).join('');
  let selectionOverlay = '';
  if (editor && selection.size > 1) {
    const objects = selectedObjects(project, selection), bounds = selectionBounds(project, selection);
    selectionOverlay = `<g class="selection-bounds"><rect x="${bounds.x - .6}" y="${bounds.y - .6}" width="${bounds.width + 1.2}" height="${bounds.height + 1.2}" fill="none" stroke="#087b60" stroke-width=".35" stroke-dasharray="1.3 .8" pointer-events="none"/>${objects.every(item => item.asset_id) ? `<rect data-resize="selection" aria-label="等比缩放所选图片" x="${bounds.x + bounds.width - 1.2}" y="${bounds.y + bounds.height - 1.2}" width="2.8" height="2.8" fill="white" stroke="#087b60" stroke-width=".4"/>` : ''}</g>`;
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" ${editor ? 'id="studio-svg" role="img" aria-label="科研图稿画布"' : ''} width="${project.figure.width_mm}mm" height="${project.figure.height_mm}mm" viewBox="0 0 ${project.figure.width_mm} ${project.figure.height_mm}">${fontStyle ? `<defs><style>${fontStyle}</style></defs>` : ''}<rect width="100%" height="100%" fill="${e(project.figure.background)}"/>${panels}${annotations}${selectionOverlay}</svg>`;
}
export async function exportSvg(project, sources) {
  const fontRecords = await projectFonts(project), fontStyle = fontRecords.map(font => `@font-face{font-family:'${font.family}';font-weight:${font.weight};src:url('${font.url}') format('${font.format}')}`).join('');
  return new Blob(['<?xml version="1.0" encoding="UTF-8"?>\n', figureMarkup(project, sources, {embedded: true, fontStyle})], {type: 'image/svg+xml'});
}
export async function rasterCanvas(project, sources, dpi) {
  if (![150, 300, 600].includes(Number(dpi))) throw new Error('导出 DPI 须为 150、300 或 600');
  const width = Math.round(project.figure.width_mm / 25.4 * dpi), height = Math.round(project.figure.height_mm / 25.4 * dpi);
  if (width * height > LIMITS.pixels || width > 16384 || height > 16384) throw new Error('成图超过 4000 万像素或 16384 px 单边上限，请降低 DPI 或画布尺寸。');
  await prepareFonts(project); const canvas = document.createElement('canvas'); canvas.width = width; canvas.height = height;
  const context = canvas.getContext('2d', {alpha: false}); if (!context) throw new Error('浏览器画布不可用');
  context.fillStyle = project.figure.background; context.fillRect(0, 0, width, height); context.scale(width / project.figure.width_mm, height / project.figure.height_mm); context.imageSmoothingQuality = 'high';
  for (const panel of project.panels) {
    const source = sources.get(panel.asset_id); if (!source) throw new Error('原件缺失');
    const image = await imageFromUrl(source.url), crop = panel.crop, w = panel.width_mm, h = panel.height_mm;
    context.save(); context.translate(panel.x_mm + w / 2, panel.y_mm + h / 2); context.rotate((panel.rotation || 0) * Math.PI / 180);
    context.save(); context.scale(panel.flip_h ? -1 : 1, panel.flip_v ? -1 : 1); context.drawImage(image, crop.x, crop.y, crop.width, crop.height, -w / 2, -h / 2, w, h); context.restore(); image.src = '';
    if (panel.scale_bar) {
      const bar = panel.scale_bar, length = bar.length_um / bar.um_per_px / crop.width * w, margin = Math.min(2, w * .03, h * .07);
      if (length > 0 && length <= w * .9) { context.strokeStyle = bar.color; context.fillStyle = bar.color; context.lineWidth = .529167; context.beginPath(); context.moveTo(w / 2 - length - margin, h / 2 - margin); context.lineTo(w / 2 - margin, h / 2 - margin); context.stroke(); context.font = '2.293057px "FW Studio Sans"'; context.textAlign = 'center'; context.fillText(`${bar.length_um} µm`, w / 2 - length / 2 - margin, h / 2 - margin - 1.5); }
    }
    context.restore(); context.textAlign = 'left'; context.fillStyle = '#202923'; const labelSize = (panel.label_font_size_pt || 10) * MM_PER_PT, titleSize = (panel.title_font_size_pt || 8) * MM_PER_PT;
    context.font = `700 ${labelSize}px "${family(panel.label, true)}"`; fillMultiline(context, panel.label, panel.x_mm, panel.y_mm - 1.8, labelSize);
    context.font = `${titleSize}px "${family(panel.title)}"`; context.textAlign = 'center'; fillMultiline(context, panel.title, panel.x_mm + w / 2, panel.y_mm - 1.8, titleSize);
  }
  for (const item of project.annotations.filter(item => !item.hidden)) {
    context.fillStyle = item.color; context.strokeStyle = item.color; context.lineWidth = (item.line_width_pt ?? .8) * MM_PER_PT;
    if (item.type === 'text') { const size = item.font_size_pt * MM_PER_PT; context.font = `${item.bold ? '700 ' : ''}${size}px "${family(item.text, !!item.bold)}"`; context.textAlign = ({middle: 'center', end: 'right'})[item.anchor] || 'left'; fillMultiline(context, item.text, item.x_mm, item.y_mm + size, size); }
    else if (item.type === 'rect') { if (item.fill && item.fill !== 'none') { context.fillStyle = item.fill; context.fillRect(item.x_mm, item.y_mm, item.width_mm, item.height_mm); } context.strokeRect(item.x_mm, item.y_mm, item.width_mm, item.height_mm); }
    else { context.setLineDash(item.dashed ? [1.4, .8] : []); context.beginPath(); context.moveTo(item.x_mm, item.y_mm); context.lineTo(item.x_mm + item.width_mm, item.y_mm + item.height_mm); context.stroke(); context.setLineDash([]); }
  }
  return canvas;
}
export async function exportPng(project, sources, dpi) {
  const canvas = await rasterCanvas(project, sources, dpi), blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png')); canvas.width = 0; canvas.height = 0;
  if (!blob) throw new Error('PNG 编码失败');
  return pngWithDensity(new Uint8Array(await blob.arrayBuffer()), dpi);
}
export async function exportRasterPdf(project, sources, dpi) {
  const canvas = await rasterCanvas(project, sources, dpi), width = canvas.width, height = canvas.height, pixels = canvas.getContext('2d').getImageData(0, 0, width, height).data;
  const rgb = new Uint8Array(width * height * 3); for (let source = 0, target = 0; source < pixels.length; source += 4) { rgb[target++] = pixels[source]; rgb[target++] = pixels[source + 1]; rgb[target++] = pixels[source + 2]; }
  canvas.width = 0; canvas.height = 0;
  return encodeRasterPdf(rgb, width, height, project.figure.width_mm, project.figure.height_mm);
}
