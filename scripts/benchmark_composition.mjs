#!/usr/bin/env node
/** Reproducible geometry planning benchmark; it does not measure DOM or rendering. */
import {performance} from 'node:perf_hooks';
import {cpus,platform,arch,release} from 'node:os';
import {createHash} from 'node:crypto';
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {planPanelLayout} from '../web/modules/layout-engine.js';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const results=[];
for(const count of [6,144,1000]) {
  const width=count===6?180:1000,height=count===6?140:1000;
  const project={figure:{width_mm:width,height_mm:height},settings:{},annotations:[],panels:Array.from({length:count},(_,index)=>({id:`panel_${index}`,x_mm:0,y_mm:0,width_mm:40,height_mm:30,crop:{x:0,y:0,width:800+index%5*100,height:600+index%3*100},label:'',label_mode:'auto'}))};
  planPanelLayout(project);
  const samples=[];
  for(let run=0;run<5;run++){const started=performance.now();planPanelLayout(project);samples.push(Number((performance.now()-started).toFixed(3)));}
  const sorted=[...samples].sort((a,b)=>a-b);
  results.push({panels:count,figure_mm:[width,height],warmup_runs:1,measured_runs:5,samples_ms:samples,median_ms:sorted[2]});
}
const report={version:'0.4.0',measured_at:new Date().toISOString(),environment:{node:process.version,platform:platform(),arch:arch(),kernel:release(),cpu:cpus()[0]?.model},module_sha256:createHash('sha256').update(await readFile(path.join(root,'web/modules/layout-engine.js'))).digest('hex'),scope:'Pure JavaScript planPanelLayout only, synthetic image geometry; excludes image decoding, DOM rendering, import, network, exports and scientific analysis. No comparison against upstream software or competing tools.',results};
const output=path.resolve(process.argv[2] || path.join(root,'benchmarks/composition_0.4.json'));
await mkdir(path.dirname(output),{recursive:true});await writeFile(output,JSON.stringify(report,null,2)+'\n');
console.log(JSON.stringify(report,null,2));
