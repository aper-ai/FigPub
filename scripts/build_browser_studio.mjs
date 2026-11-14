#!/usr/bin/env node
/** Build one portable, network-independent HTML. Runtime requires no Node/Python/CDN. */
import {build} from 'esbuild';
import {readFile, writeFile, mkdir} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {fontCoverage} from './build_browser_studio_fonts.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const {version} = JSON.parse(await readFile(path.join(root, 'package.json'), 'utf8'));
if (!/^\d+\.\d+\.\d+$/.test(version)) throw new Error('Invalid application version');
const output = path.resolve(process.argv[2] || path.join(root, 'dist', `Figure_Workbench_${version}_Browser.html`));
const fonts = {normal: 'DejaVuSans.ttf', bold: 'DejaVuSans-Bold.ttf', cjk: 'NotoSansCJK-SC.woff2'};
const [template, css, result, fontEntries, notices, coverages] = await Promise.all([
  readFile(path.join(root, 'web/browser-studio.html'), 'utf8'),
  readFile(path.join(root, 'web/browser-studio.css'), 'utf8'),
  build({entryPoints: [path.join(root, 'web/browser-studio.js')], bundle: true, write: false, minify: true, format: 'esm', target: ['chrome120', 'edge120', 'firefox122', 'safari17'], legalComments: 'inline', charset: 'utf8', sourcemap: false, metafile: true}),
  Promise.all(Object.entries(fonts).map(async ([key, name]) => [key, (await readFile(path.join(root, 'resources/fonts', name))).toString('base64')])),
  Promise.all(['LICENSE', 'licenses/DejaVu-fonts.txt', 'licenses/Noto-CJK.txt'].map(name => readFile(path.join(root, name), 'utf8'))),
  Promise.all([['normal', 'DejaVuSans.ttf', 0], ['bold', 'DejaVuSans-Bold.ttf', 0], ['cjk', 'NotoSansCJK-Regular.ttc', 2]].map(async ([key, name, index]) => [key, fontCoverage(await readFile(path.join(root, 'resources/fonts', name)), index)])),
]);
if (result.metafile.outputs[Object.keys(result.metafile.outputs)[0]].imports.length) throw new Error('Unexpected external bundle dependency');
const script = result.outputFiles[0].text.replace(/<\/script/gi, '<\\/script');
const policy = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data: blob:; connect-src 'none'; base-uri 'none'; form-action 'none'">`;
const copyright = `<!-- Figure Workbench ${version}; bundled code and font license notices.\n${notices.join('\n\n').replaceAll('--', '—')}\n-->`;
// Function replacers preserve literal $&, $`, and $' in bundled JavaScript.
// A replacement string interprets those sequences and can inject HTML tags
// inside a JavaScript string, breaking the entire offline application.
const html = template.replace('<meta name="viewport"', () => `${policy}\n  <meta name="viewport"`)
  .replace('<link rel="stylesheet" href="browser-studio.css">', () => `<style>${css}</style>`)
  .replace('<script type="module" src="browser-studio.js"></script>', () => `${copyright}\n<script>globalThis.FW_STUDIO_FONTS=${JSON.stringify(Object.fromEntries(fontEntries))};globalThis.FW_STUDIO_FONT_COVERAGE=${JSON.stringify(Object.fromEntries(coverages))};</script>\n<script type="module">${script}</script>`);
await mkdir(path.dirname(output), {recursive: true}); await writeFile(output, html);
console.log(`Built ${output} (${(Buffer.byteLength(html) / 1048576).toFixed(2)} MiB); JS, styles, and Latin/CJK fonts embedded.`);
