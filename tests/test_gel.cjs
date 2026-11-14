/* Numerical/behavior regressions for the new, DOM-free localization core. */
const test = require('node:test');
const assert = require('node:assert/strict');
const engine = require('../web/gel-engine.js');

function syntheticGel(bright = false, scale = 1) {
  const width = 640, height = 360, centers = [70, 170, 270, 370, 470, 570];
  const data = new Float32Array(width * height);
  for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
    let value = .85;
    centers.forEach((center, index) => {
      const bands = index === 0 ? [60, 100, 150, 210, 280] : [105 + 3 * index, 255];
      bands.forEach(band => {value -= .48 * Math.exp(-(((x-center)/22)**6) - ((y-band)/5)**2);});
    });
    data[y*width+x] = bright ? 1-value : value;
  }
  return {width, height, data, source_width:width*scale, source_height:height*scale};
}

test('dark lanes and measured band centers on synthetic image', () => {
  const image = syntheticGel(), before = image.data.slice();
  const result = engine.analyze(image, {lane_count:6, mode:'protein'});
  assert.equal(result.lane_centers.length,6);
  [70,170,270,370,470,570].forEach((center,i) => assert.ok(Math.abs(result.lane_centers[i]-center)<2));
  assert.equal(result.bands.filter(b=>b.lane_index===0).length,5);
  assert.equal(result.bands.length,15);
  assert.equal(result.polarity,'dark');
  assert.equal(result.requires_review,true);
  assert.deepEqual(image.data,before);
});
test('bright gel and localization-to-original scale', () => {
  const result = engine.analyze(syntheticGel(true,4), {lane_count:6});
  assert.equal(result.polarity,'bright');
  assert.ok(Math.abs(result.lane_centers[2]-270*4)<8);
  assert.ok(Math.abs(result.bands.find(b=>b.lane_index===0).y-60*4)<8);
});
test('blank image does not fabricate requested lanes or markers', () => {
  const result=engine.analyze({width:64,height:64,data:new Float32Array(4096).fill(.5)},{lane_count:6});
  assert.deepEqual(result.lane_centers,[]);assert.deepEqual(result.marker_points,[]);
});
test('invalid data and excessive geometry fail explicitly', () => {
  assert.throws(()=>engine.analyze({width:9,height:9,data:[1]}));
  const data=new Float32Array(1000*1000);data[33333]=NaN;
  assert.throws(()=>engine.analyze({width:1000,height:1000,data}),/有限/);
  assert.throws(()=>engine.analyze({width:9000,height:3,data:[]}));
});
test('log-linear interpolation, exact first anchor, and labelled extrapolation', () => {
  const points=[{y:10,size:100},{y:30,size:50},{y:80,size:10}];
  assert.ok(Math.abs(engine.calibrate(points,10).size-100)<1e-10);
  assert.equal(engine.calibrate(points,10).extrapolated,false);
  assert.ok(Math.abs(engine.calibrate(points,20).size-Math.sqrt(5000))<1e-10);
  assert.equal(engine.calibrate(points,90).extrapolated,true);
  assert.throws(()=>engine.calibrate(points,NaN));
  assert.throws(()=>engine.calibrate([{y:1,size:10},{y:1,size:5}],2));
  assert.throws(()=>engine.calibrate([{y:1,size:10},{y:2,size:20}],2));
});
test('marker matching only returns measured positions with missing bands', () => {
  const sizes=[150,100,75,50,37,25,20,15,10];
  const positions=sizes.map(size=>500-80*Math.log(size));positions.splice(3,1);
  const result=engine.matchMarker(positions,sizes,360);
  assert.ok(result && result.points.length>=6);
  assert.ok(result.points.every(point=>positions.includes(point.y)));
  assert.equal(engine.matchMarker([10,20],sizes,360),null);
});
