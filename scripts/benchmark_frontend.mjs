/** Synthetic geometry payload benchmark; never presented as biological measurements. */
import {performance} from 'node:perf_hooks';
import {ensureProject} from '../web/modules/domain.js';
import {captureRecipe} from '../web/modules/state.js';

const objectCount=Number(process.argv[2] || 1000);
if(!Number.isSafeInteger(objectCount)||objectCount<1||objectCount>10000)throw new Error('Object count must be 1–10000.');
const objects=Array.from({length:objectCount},(_,index)=>({
  id:index,
  contour:Array.from({length:128},(_,point)=>[index*3+Math.cos(point/128*Math.PI*2),index*2+Math.sin(point/128*Math.PI*2)]),
}));
const project=ensureProject({
  id:'synthetic_benchmark',title:'Synthetic snapshot benchmark',revision:1,
  assets:[{id:'source',kind:'image',width_px:4096,height_px:4096}],
  figure:{width_mm:180,height_mm:120},
  panels:[{id:'panel',asset_id:'source',x_mm:10,y_mm:10,width_mm:80,height_mm:80}],
  measurements:[{id:'synthetic_payload',kind:'microscopy',result:{objects}}],
  annotations:[],settings:{},audit:[],
});
const samples=[];
for(let index=0;index<100;index++){const start=performance.now();captureRecipe(project);samples.push(performance.now()-start);}
samples.sort((left,right)=>left-right);
globalThis.gc?.();
const baselineStart=performance.now();const fullSnapshot=structuredClone(project);const fullCloneMs=performance.now()-baselineStart;
const recipe=captureRecipe(project);
console.log(JSON.stringify({
  benchmark:'frontend-recipe-snapshot',data:'synthetic geometry payload; no experimental measurements',
  node_version:process.version,object_count:objectCount,contour_points_per_object:128,
  recipe_samples:100,recipe_median_ms:samples[50],recipe_p95_ms:samples[95],
  full_project_clone_single_sample_ms:fullCloneMs,
  recipe_serialized_bytes:Buffer.byteLength(JSON.stringify(recipe)),
  full_project_serialized_bytes:Buffer.byteLength(JSON.stringify(fullSnapshot)),
  immutable_results_excluded:!Object.hasOwn(recipe,'measurements'),
  note:'Serialized byte counts are payload sizes, not heap memory measurements; timing is machine-dependent.',
},null,2));
