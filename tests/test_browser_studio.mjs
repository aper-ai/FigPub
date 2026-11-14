import test from 'node:test';
import assert from 'node:assert/strict';
import {deflateRawSync, deflateSync, inflateSync} from 'node:zlib';
import {readFile} from 'node:fs/promises';
import {crc32, zipStore, unzipVerified, encodeProject, decodeProject, sha256} from '../web/browser/archive.js';
import {newProject, panelForAsset, validateBrowserProject, figureIssues, recipeSnapshot, captureCommittedWorkspace, expandedSelection, selectedObjects, selectionUnits, selectionBounds, planUnitAlignment, planSelectionTransform, selectionGeometryChanged, assertUngroupedLayout} from '../web/browser/model.js';
import {inspectRaster} from '../web/browser/sources.js';
import {pngWithDensity, encodeRasterPdf} from '../web/browser/encoding.js';
import {figureMarkup, exportSvg, fontKeyForText, fontIssues} from '../web/browser/render.js';
import {applyPanelLayout} from '../web/modules/layout-engine.js';
import {fontCoverage} from '../scripts/build_browser_studio_fonts.mjs';

const utf8 = new TextEncoder();
const bytesOf = async blob => new Uint8Array(await blob.arrayBuffer());
function pngChunk(type, data) { const result = Buffer.alloc(data.length + 12); result.writeUInt32BE(data.length); result.write(type, 4); result.set(data, 8); result.writeUInt32BE(crc32(result.subarray(4, 8 + data.length)), 8 + data.length); return result; }
function png(width = 24, height = 16) { const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(width); ihdr.writeUInt32BE(height, 4); ihdr[8] = 8; ihdr[9] = 2; return new Uint8Array(Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), pngChunk('IHDR', ihdr), pngChunk('IDAT', deflateSync(Buffer.alloc((width * 3 + 1) * height, 0))), pngChunk('IEND', Buffer.alloc(0))])); }
async function fixture() {
  const bytes = png(), id = await sha256(bytes), project = newProject();
  const asset = {id, sha256: id, name: 'synthetic.png', kind: 'image', width_px: 24, height_px: 16, bit_depth: 8, dtype: 'uint8', channels: 3, frames: 1, metadata: {}, preview_url: 'blob:temporary'};
  project.assets.push(asset); project.panels.push(panelForAsset(asset)); applyPanelLayout(project); return {project, sources: new Map([[id, {asset, bytes, mime: 'image/png', extension: '.png', url: 'blob:temporary'}]])};
}
function deflatedZip(name, bytes) {
  const encodedName = Buffer.from(name), compressed = deflateRawSync(bytes), local = Buffer.alloc(30), central = Buffer.alloc(46), end = Buffer.alloc(22), checksum = crc32(bytes);
  local.writeUInt32LE(0x04034b50); local.writeUInt16LE(20, 4); local.writeUInt16LE(8, 8); local.writeUInt32LE(checksum, 14); local.writeUInt32LE(compressed.length, 18); local.writeUInt32LE(bytes.length, 22); local.writeUInt16LE(encodedName.length, 26);
  central.writeUInt32LE(0x02014b50); central.writeUInt16LE(20, 4); central.writeUInt16LE(20, 6); central.writeUInt16LE(8, 10); central.writeUInt32LE(checksum, 16); central.writeUInt32LE(compressed.length, 20); central.writeUInt32LE(bytes.length, 24); central.writeUInt16LE(encodedName.length, 28);
  end.writeUInt32LE(0x06054b50); end.writeUInt16LE(1, 8); end.writeUInt16LE(1, 10); end.writeUInt32LE(46 + encodedName.length, 12); end.writeUInt32LE(30 + encodedName.length + compressed.length, 16);
  return new Uint8Array(Buffer.concat([local, encodedName, compressed, central, encodedName, end]));
}

test('portable archive preserves every original byte, metadata, and source identity', async () => {
  const {project, sources} = await fixture(); project.settings.provenance = {workflow: 'native scientific results remain preserved'};
  const restored = await decodeProject(await bytesOf(await encodeProject(project, sources)));
  assert.equal(restored.project.assets[0].preview_url, ''); assert.equal(project.assets[0].preview_url, 'blob:temporary');
  assert.deepEqual(restored.originals[0].bytes, sources.values().next().value.bytes); assert.deepEqual(restored.project.settings.provenance, project.settings.provenance);
  validateBrowserProject(restored.project); assert.equal(restored.project.panels[0].label, 'A');
});
test('ZIP reads native deflate files as well as stored entries', async () => {
  const raw = utf8.encode('原始数据'.repeat(300)); assert.deepEqual((await unzipVerified(deflatedZip('project.json', raw))).get('project.json'), raw);
  assert.deepEqual((await unzipVerified(await bytesOf(zipStore([{name: 'safe.txt', bytes: raw}])))).get('safe.txt'), raw);
});
test('ZIP refuses traversal, duplicate names, damaged CRC, and mismatched file headers', async () => {
  assert.throws(() => zipStore([{name: '../secret', bytes: new Uint8Array()}]), /路径/);
  const duplicate = await bytesOf(zipStore([{name: 'a', bytes: utf8.encode('a')}, {name: 'a', bytes: utf8.encode('b')}])); await assert.rejects(unzipVerified(duplicate), /重复/);
  const corrupt = await bytesOf(zipStore([{name: 'a', bytes: utf8.encode('a')} ])); corrupt[31] ^= 1; await assert.rejects(unzipVerified(corrupt), /校验/);
  const mismatched = deflatedZip('a', utf8.encode('test')); mismatched[30] = 98; await assert.rejects(unzipVerified(mismatched), /文件头/);
});
test('ZIP checks decompressed length before accepting compressed content', async () => {
  const archive = deflatedZip('x', utf8.encode('long content '.repeat(1000))), view = new DataView(archive.buffer); let central;
  for (let i = 0; i < archive.length - 4; i++) if (view.getUint32(i, true) === 0x02014b50) { central = i; break; }
  view.setUint32(central + 24, 1, true); await assert.rejects(unzipVerified(archive), /解压大小/);
});
test('project SHA, original SHA, declared membership and export source availability are enforced', async () => {
  const {project, sources} = await fixture(), data = await bytesOf(await encodeProject(project, sources)), files = await unzipVerified(data);
  const entries = () => [...files].map(([name, bytes]) => ({name, bytes}));
  files.set('project.json', utf8.encode('{}')); await assert.rejects(decodeProject(await bytesOf(zipStore(entries()))), /SHA/);
  const original = await unzipVerified(data); original.set('extra.bin', utf8.encode('not declared')); await assert.rejects(decodeProject(await bytesOf(zipStore([...original].map(([name, bytes]) => ({name, bytes}))))), /未声明/);
  const tampered = new Map(sources); tampered.set(project.assets[0].id, {...sources.get(project.assets[0].id), bytes: png(25, 16)}); await assert.rejects(encodeProject(project, tampered), /哈希/);
  await assert.rejects(encodeProject(project, new Map()), /原件/);
});
test('browser schema refuses unsupported scientific presentation instead of dropping it', async () => {
  const {project} = await fixture(); project.measurements.push({kind: 'wb', values: [1, 2, 3]}); validateBrowserProject(project); assert.equal(project.measurements.length, 1);
  for (const changed of [{channel_view: {mode: 'merge'}}, {display: {gamma: 1.2}}, {gel: {lane_centers: [12]}}, {frame: 2}]) { const value = structuredClone(project); Object.assign(value.panels[0], changed); assert.throws(() => validateBrowserProject(value), /本机工作台/); }
});
test('browser schema checks crop, nonfinite geometry and scale bars before rendering', async () => {
  const {project} = await fixture(); project.panels[0].crop.x = 24; assert.throws(() => validateBrowserProject(project), /裁切/);
  project.panels[0].crop.x = 0; project.panels[0].rotation = Infinity; assert.throws(() => validateBrowserProject(project), /尺寸/);
  project.panels[0].rotation = 0; project.panels[0].scale_bar = {um_per_px: 1, length_um: 1000, color: '#ffffff'}; assert.throws(() => validateBrowserProject(project), /比例尺长度/);
});
test('original resolution and shared caption bounds are checked independently of output DPI', async () => {
  const {project} = await fixture(); const initial = figureIssues(project); assert(initial.some(issue => issue.code === 'low-ppi')); assert(!initial.some(issue => issue.severity === 'error'));
  project.panels[0].y_mm = 1; assert(figureIssues(project).some(issue => issue.code === 'out-of-canvas'));
  project.panels.push({...project.panels[0], id: 'second', label: 'a'}); assert(figureIssues(project).some(issue => issue.code === 'duplicate-label'));
});
test('raster header inspection preserves PNG bit depth and rejects oversized or animated sources', () => {
  const raw = png(); assert.deepEqual(inspectRaster(raw, 'image/png'), {width: 24, height: 16, bitDepth: 8, channels: 3});
  const sixteen = raw.slice(); sixteen[24] = 16; assert.equal(inspectRaster(sixteen, 'image/png').bitDepth, 16);
  const huge = raw.slice(); new DataView(huge.buffer).setUint32(16, 100000); new DataView(huge.buffer).setUint32(20, 100000); assert.throws(() => inspectRaster(huge, 'image/png'), /4000/);
  const animated = Buffer.concat([raw.subarray(0, 33), pngChunk('acTL', Buffer.alloc(8)), raw.subarray(33)]); assert.throws(() => inspectRaster(animated, 'image/png'), /动画 PNG/);
});
test('JPEG EXIF orientation is rejected explicitly to protect native coordinate round trips', () => {
  const exif = Buffer.alloc(32); exif.write('Exif'); exif.write('II', 6); exif.writeUInt16LE(42, 8); exif.writeUInt32LE(8, 10); exif.writeUInt16LE(1, 14); exif.writeUInt16LE(0x112, 16); exif.writeUInt16LE(3, 18); exif.writeUInt32LE(1, 20); exif.writeUInt16LE(6, 24);
  const app = Buffer.alloc(4); app.writeUInt16BE(0xffe1); app.writeUInt16BE(exif.length + 2, 2); const data = Buffer.concat([Buffer.from([255, 216]), app, exif, Buffer.from([255, 217])]);
  assert.throws(() => inspectRaster(data, 'image/jpeg'), /EXIF 自动旋转/);
});
test('PNG output has one validated pHYs chunk with the requested physical DPI', async () => {
  const once = await bytesOf(pngWithDensity(png(), 300)), twice = await bytesOf(pngWithDensity(once, 600)); let found = 0;
  for (let at = 8; at < twice.length;) { const view = new DataView(twice.buffer), length = view.getUint32(at), type = new TextDecoder().decode(twice.subarray(at + 4, at + 8)); if (type === 'pHYs') { found++; assert.equal(view.getUint32(at + 8), Math.round(600 / .0254)); assert.equal(twice[at + 16], 1); } at += length + 12; }
  assert.equal(found, 1); const bad = png(); bad[40] ^= 1; assert.throws(() => pngWithDensity(bad, 300), /校验/);
});
test('raster PDF streams are lossless RGB, page size and xref offsets are exact', async () => {
  const rgb = Uint8Array.from({length: 12 * 8 * 3}, (_, index) => index % 256), bytes = Buffer.from(await bytesOf(await encodeRasterPdf(rgb, 12, 8, 89, 60))), text = bytes.toString('latin1');
  assert(text.includes('/MediaBox [0 0 252.28346 170.07874]')); assert(text.includes('/Width 12 /Height 8')); assert(text.includes('/Filter /FlateDecode'));
  const start = text.indexOf('stream\n', text.indexOf('4 0 obj')) + 7, length = Number(text.slice(text.indexOf('4 0 obj'), start).match(/\/Length (\d+)/)[1]); assert.deepEqual(new Uint8Array(inflateSync(bytes.subarray(start, start + length))), rgb);
  const xref = Number(text.match(/startxref\n(\d+)/)[1]); assert.equal(text.slice(xref, xref + 4), 'xref'); const entries = text.slice(xref).split('\n').slice(3, 8); entries.forEach((entry, index) => assert.equal(text.slice(Number(entry.slice(0, 10)), Number(entry.slice(0, 10)) + 7), `${index + 1} 0 obj`));
  await assert.rejects(encodeRasterPdf(new Uint8Array(3), 12, 8, 89, 60), /像素数据/);
});
test('SVG embeds exact original bytes and escapes all user text', async () => {
  const {project, sources} = await fixture(); project.panels[0].title = '<script>&"'; const markup = figureMarkup(project, sources, {embedded: true});
  assert(markup.includes('&lt;script&gt;&amp;&quot;')); assert(!markup.includes('blob:temporary')); const encoded = markup.match(/data:image\/png;base64,([A-Za-z0-9+/=]+)/)[1]; assert.deepEqual(new Uint8Array(Buffer.from(encoded, 'base64')), sources.values().next().value.bytes); assert(markup.includes('width="180mm"')); assert(markup.includes('text-anchor="middle"'));
});
test('multiline SVG text uses shared line splitting, 1.25-em spacing and native title anchors', async () => {
  const {project, sources} = await fixture(); project.panels[0].title = 'first\r\nsecond\n'; project.panels[0].title_font_size_pt = 9;
  project.annotations.push({id: 'multiline', type: 'text', text: '中文\u2028two', x_mm: 70, y_mm: 60, width_mm: 0, height_mm: 0, font_size_pt: 8, color: '#222222', anchor: 'end'});
  const markup = figureMarkup(project, sources); assert(markup.includes('text-anchor="end"')); assert(markup.includes('text-anchor="middle"')); assert(markup.includes('dy="3.96875">second</tspan>')); assert(markup.includes('>中文</tspan>')); assert(markup.includes('>two</tspan>')); assert(!markup.includes('first\r'));
});
async function groupedFixture() {
  const {project, sources} = await fixture(), base = project.panels[0]; project.panels = Array.from({length: 6}, (_, index) => ({...structuredClone(base), id: `p${index}`, x_mm: [10, 25, 60, 75, 120, 155][index], y_mm: 20 + Math.floor(index / 2) * 10, width_mm: 10, height_mm: 8, label: '', label_mode: 'none', group_id: index < 2 ? 'g1' : index < 4 ? 'g2' : null})); return {project, sources};
}
test('selection expansion follows restored image groups and excludes deleted identifiers', async () => {
  const {project} = await groupedFixture(); const selection = new Set(['p0', 'deleted']);
  assert.deepEqual([...expandedSelection(project, selection)], ['p0', 'p1']); assert.equal(selectedObjects(project, selection).length, 2);
  const ungrouped = recipeSnapshot(project); delete ungrouped.panels[0].group_id; delete ungrouped.panels[1].group_id;
  assert.deepEqual([...expandedSelection(ungrouped, selection)], ['p0']); assert.deepEqual([...expandedSelection(project, selection)], ['p0', 'p1']); assert.deepEqual([...selection], ['p0', 'deleted']);
});
test('alignment and distribution move complete group units without changing their internal spacing', async () => {
  const {project} = await groupedFixture(), selection = new Set(['p0', 'p2', 'p4']);
  assert.equal(selectionUnits(project, selection).length, 3); const before = JSON.stringify(project);
  const left = new Map(planUnitAlignment(project, selection, 'align-left').map(item => [item.id, item]));
  assert.equal(left.get('p0').x_mm, 10); assert.equal(left.get('p2').x_mm, 10); assert.equal(left.get('p3').x_mm - left.get('p2').x_mm, 15); assert.equal(left.get('p4').x_mm, 10);
  const distribution = new Map(planUnitAlignment(project, selection, 'distribute-x').map(item => [item.id, item]));
  assert.equal(distribution.get('p3').x_mm - distribution.get('p2').x_mm, 15);
  const gapOne = distribution.get('p2').x_mm - distribution.get('p1').x_mm - 10, gapTwo = distribution.get('p4').x_mm - distribution.get('p3').x_mm - 10; assert.equal(gapOne, gapTwo);
  assert.equal(JSON.stringify(project), before); assert.throws(() => planUnitAlignment(project, new Set(['p0']), 'align-left'), /独立面板或编组/);
});
test('uniform group transforms preserve ratios, expand partial selections, and are atomic on invalid input', async () => {
  const {project} = await groupedFixture(), before = recipeSnapshot(project), selection = new Set(['p0']), original = JSON.stringify(project);
  const placements = planSelectionTransform(project, selection, {scale: 1.5, pivot_x_mm: 0, pivot_y_mm: 0});
  assert.equal(placements.length, 2); assert.equal(placements[0].width_mm, 15); assert.equal(placements[0].height_mm, 12); assert.equal(placements[1].x_mm - placements[0].x_mm, 22.5);
  assert.throws(() => planSelectionTransform(project, selection, {scale_x: 2, scale_y: 1}), /等比/);
  assert.throws(() => planSelectionTransform(project, selection, {dx_mm: 100000}), /参数/); assert.equal(JSON.stringify(project), original);
  const after = recipeSnapshot(project); for (const placement of placements) Object.assign(after.panels.find(item => item.id === placement.id), placement); assert(selectionGeometryChanged(before, after, selection)); assert(!selectionGeometryChanged(before, project, selection));
  assert.equal(after.panels[0].label_font_size_pt, project.panels[0].label_font_size_pt);
});
test('grouped automatic layouts and mixed annotation resizing are refused explicitly', async () => {
  const {project} = await groupedFixture(); assert.throws(() => assertUngroupedLayout(project), /先解组/);
  project.annotations.push({id: 'text', type: 'text', text: 'label', x_mm: 20, y_mm: 70, width_mm: 10, height_mm: 5, font_size_pt: 8, color: '#222222'});
  assert.throws(() => planSelectionTransform(project, new Set(['p0', 'text']), {scale: 2}), /仅支持图片/);
  project.annotations[0].group_id = 'g1'; assert.throws(() => validateBrowserProject(project), /仅支持图片面板编组/);
});
test('multi-image selections expose a physical bounding box and a uniform resize handle', async () => {
  const {project, sources} = await groupedFixture(), selection = expandedSelection(project, new Set(['p0']));
  assert.deepEqual(selectionBounds(project, selection), {x: 10, y: 20, width: 25, height: 8});
  assert(figureMarkup(project, sources, {editor: true, selection}).includes('data-resize="selection"'));
  assert(!figureMarkup(project, sources, {embedded: true, selection}).includes('data-resize'));
});
test('history shares large immutable results while isolating every editable recipe', async () => {
  const {project, sources} = await fixture(); project.measurements = [{large_result: new Float64Array(100000)}]; project.audit = [{action: 'analysis_completed'}]; project.vendor_extension = {preserved: true}; project.assets[0].metadata.large_record = new Uint8Array(100000);
  const snapshot = recipeSnapshot(project), committed = captureCommittedWorkspace(project, sources);
  assert.equal(snapshot.measurements, project.measurements); assert.equal(snapshot.audit, project.audit); assert.equal(snapshot.vendor_extension, project.vendor_extension); assert.equal(snapshot.assets[0], project.assets[0]);
  assert.notEqual(snapshot.assets, project.assets); assert.notEqual(snapshot.panels[0], project.panels[0]); assert.notEqual(snapshot.settings, project.settings);
  project.panels[0].x_mm += 20; project.figure.width_mm = 300; project.settings.composition.layout.columns = 6; project.assets.push({...project.assets[0], id: 'another'}); sources.clear();
  assert.notEqual(committed.project.panels[0].x_mm, project.panels[0].x_mm); assert.equal(committed.project.figure.width_mm, 180); assert.notEqual(committed.project.settings.composition.layout.columns, 6); assert.equal(committed.project.assets.length, 1); assert.equal(committed.sources.size, 1);
  assert.equal(committed.project.measurements, project.measurements);
});
test('standalone fonts embed actual Latin/CJK files and reject unsupported glyphs before export', async () => {
  const directory = new URL('../resources/fonts/', import.meta.url); const normal = await readFile(new URL('DejaVuSans.ttf', directory)), bold = await readFile(new URL('DejaVuSans-Bold.ttf', directory)), cjk = await readFile(new URL('NotoSansCJK-SC.woff2', directory)), collection = await readFile(new URL('NotoSansCJK-Regular.ttc', directory));
  globalThis.FW_STUDIO_FONTS = {normal: normal.toString('base64'), bold: bold.toString('base64'), cjk: cjk.toString('base64')}; globalThis.FW_STUDIO_FONT_COVERAGE = {normal: fontCoverage(normal), bold: fontCoverage(bold), cjk: fontCoverage(collection, 2)};
  try {
    assert.equal(fontKeyForText('A β µm', true), 'bold'); assert.equal(fontKeyForText('蛋白表达 Δ'), 'cjk'); assert.throws(() => fontKeyForText('\u{10ffff}'), /不能表示/);
    const {project, sources} = await fixture(); project.panels[0].title = '蛋白表达 Δ'; const svg = await (await exportSvg(project, sources)).text(); assert(svg.includes('data:font/woff2;base64,' + cjk.toString('base64'))); assert(svg.includes('蛋白表达 Δ')); assert(svg.includes('data:font/ttf;base64,'));
    project.panels[0].title = '\u{10ffff}'; assert.equal(fontIssues(project)[0].severity, 'error'); await assert.rejects(exportSvg(project, sources), /不能表示/);
  } finally { delete globalThis.FW_STUDIO_FONTS; delete globalThis.FW_STUDIO_FONT_COVERAGE; }
});
