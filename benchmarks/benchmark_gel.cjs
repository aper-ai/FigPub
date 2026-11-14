/* Measured localization performance; no upstream-app speed comparison. */
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const engine = require('../web/gel-engine.js');

function fixture(width, height, count) {
  const data = new Float32Array(width*height).fill(.86);
  const centers=Array.from({length:count},(_,index)=>(index+.5)*width/count);
  const bands=[.18,.34,.58,.8].map(fraction=>fraction*height);
  const xProfile=new Float32Array(width),yProfile=new Float32Array(height);
  for(let x=0;x<width;x++)xProfile[x]=centers.reduce((sum,center)=>sum+Math.exp(-(((x-center)/(width/count*.23))**6)),0);
  for(let y=0;y<height;y++)yProfile[y]=bands.reduce((sum,center)=>sum+Math.exp(-(((y-center)/(height*.01))**2)),0);
  let seed=2701;
  for(let index=0;index<data.length;index++){
    seed^=seed<<13;seed^=seed>>>17;seed^=seed<<5;
    data[index]-=.5*xProfile[index%width]*yProfile[Math.floor(index/width)]+((seed>>>0)/4294967296-.5)*.006;
  }
  return {width,height,data};
}
function benchmark(width,height,count){
  const image=fixture(width,height,count),samples=[];
  const cold=engine.analyze(image,{lane_count:count,mode:'protein'});
  let last;
  for(let index=0;index<7;index++){last=engine.analyze(image,{lane_count:count,mode:'protein'});samples.push(last.elapsed_ms);}
  const sorted=[...samples].sort((a,b)=>a-b);
  return {width,height,input_bytes:image.data.byteLength,expected_lanes:count,detected_lanes:last.lane_centers.length,detected_bands:last.bands.length,cold_ms:cold.elapsed_ms,samples_ms:samples,median_ms:sorted[3],max_ms:sorted.at(-1)};
}
const result={benchmark_version:1,synthetic_data:true,environment:{node:process.version,platform:os.platform(),cpu:os.cpus()[0]?.model},scope:'New projection/grid localization core only; excludes I/O, worker startup and marker catalogue matching.',cases:[benchmark(640,360,6),benchmark(1024,1024,12)],limitations:['Synthetic fixtures do not establish accuracy on real experimental images.','Not a benchmark of GelFigure or ScieFig; no industry-leading performance claim.']};
fs.writeFileSync(path.join(__dirname,'results_gel.json'),JSON.stringify(result,null,2));
console.log(JSON.stringify(result,null,2));
