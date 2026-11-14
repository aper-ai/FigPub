/** Extract Unicode coverage from bundled, trusted TTF/TTC font files at build time. */
export function fontCoverage(buffer, collectionIndex = 0) {
  const font = buffer.toString('ascii', 0, 4) === 'ttcf' ? buffer.readUInt32BE(12 + collectionIndex * 4) : 0;
  const tables = buffer.readUInt16BE(font + 4); let cmap;
  for (let index = 0; index < tables; index++) { const at = font + 12 + index * 16; if (buffer.toString('ascii', at, at + 4) === 'cmap') cmap = buffer.readUInt32BE(at + 8); }
  if (cmap == null) throw new Error('Bundled font has no Unicode character map');
  const codepoints = new Set(), seen = new Set();
  for (let index = 0; index < buffer.readUInt16BE(cmap + 2); index++) {
    const at = cmap + 4 + index * 8, platform = buffer.readUInt16BE(at), encoding = buffer.readUInt16BE(at + 2), subtable = cmap + buffer.readUInt32BE(at + 4);
    if (!(platform === 0 || platform === 3 && [1, 10].includes(encoding)) || seen.has(subtable)) continue; seen.add(subtable);
    const format = buffer.readUInt16BE(subtable);
    if (format === 12) for (let group = 0; group < buffer.readUInt32BE(subtable + 12); group++) { const offset = subtable + 16 + group * 12, start = buffer.readUInt32BE(offset), end = buffer.readUInt32BE(offset + 4), glyph = buffer.readUInt32BE(offset + 8); for (let codepoint = start; codepoint <= end; codepoint++) if (glyph + codepoint - start) codepoints.add(codepoint); }
    else if (format === 4) {
      const count = buffer.readUInt16BE(subtable + 6) / 2, ends = subtable + 14, starts = ends + count * 2 + 2, deltas = starts + count * 2, offsets = deltas + count * 2;
      for (let segment = 0; segment < count; segment++) { const start = buffer.readUInt16BE(starts + segment * 2), end = buffer.readUInt16BE(ends + segment * 2), delta = buffer.readInt16BE(deltas + segment * 2), range = buffer.readUInt16BE(offsets + segment * 2);
        for (let codepoint = start; codepoint <= end && codepoint < 0xffff; codepoint++) { const raw = range ? buffer.readUInt16BE(offsets + segment * 2 + range + (codepoint - start) * 2) : codepoint, glyph = range && raw === 0 ? 0 : (raw + delta) & 0xffff; if (glyph) codepoints.add(codepoint); }
      }
    }
  }
  const ranges = []; for (const codepoint of [...codepoints].sort((a, b) => a - b)) { const previous = ranges.at(-1); if (previous && previous[1] + 1 === codepoint) previous[1] = codepoint; else ranges.push([codepoint, codepoint]); }
  if (!ranges.length) throw new Error('Bundled font has no supported Unicode cmap'); return ranges;
}
