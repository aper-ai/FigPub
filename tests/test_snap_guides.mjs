import test from 'node:test';
import assert from 'node:assert/strict';
import {snapTranslation,imageFrameBounds,snapGuideMarkup,svgPixelsPerMm} from '../web/modules/snap-guides.js';

const panel=(id,x,y,extra={})=>({id,asset_id:'synthetic',x_mm:x,y_mm:y,width_mm:20,height_mm:10,label:'',...extra});
const project=panels=>({figure:{width_mm:500,height_mm:300},panels,annotations:[],settings:{}});
const near=(actual,expected)=>assert.ok(Math.abs(actual-expected)<1e-7,`${actual} != ${expected}`);

test('image tops align exactly even when labels have different font sizes',()=>{
  const moving=panel('moving',12,15,{label:'A',label_font_size_pt:8}),target=panel('target',80,60,{label:'B',label_font_size_pt:30});
  const snap=snapTranslation(project([moving,target]),[moving],67.2,44,{pixelsPerMm:4});
  near(moving.x_mm+snap.dx_mm,80);near(moving.y_mm+snap.dy_mm,60);
  assert.equal(snap.guides.length,2);
});

for(const scale of [.5,2,8])test(`the same 6 screen pixels snap and 8 screen pixels do not at ${scale} px/mm`,()=>{
  const moving=panel('moving',100,100,{width_mm:100,height_mm:80}),target=panel('target',300,200,{width_mm:100,height_mm:80});
  const data=project([moving,target]);
  const within=snapTranslation(data,[moving],200-6/scale,0,{pixelsPerMm:scale});
  near(within.dx_mm,200);assert.ok(within.guides.some(guide=>guide.axis==='x'));
  const outside=snapTranslation(data,[moving],200-8/scale,0,{pixelsPerMm:scale});
  near(outside.dx_mm,200-8/scale);assert.ok(!outside.guides.some(guide=>guide.axis==='x'));
});

test('detached grid labels snap to another label column and baseline without snapping text extents to image edges',()=>{
  const moving=panel('moving',20,30,{height_mm:12,label:'A',label_anchor:{x_ratio:-.25,y_ratio:-.4}}),target=panel('target',90,85,{label:'C',label_anchor:{x_ratio:-1,y_ratio:-.8}});
  const snap=snapTranslation(project([moving,target]),[moving],54,50.8,{pixelsPerMm:5});
  near(snap.dx_mm,55);near(snap.dy_mm,51.8);
  assert.ok(snap.guides.every(guide=>guide.kind==='label'));
  near(20+snap.dx_mm+20*(-.25),90+20*(-1));
  near(30+snap.dy_mm+12*(-.4),85+10*(-.8));
});

test('a whole selection snaps as one unit without pulling toward its own panels',()=>{
  const first=panel('first',20,30),second=panel('second',50,45),target=panel('target',100,90);
  const snap=snapTranslation(project([first,second,target]),[first,second],29.2,0,{pixelsPerMm:5});
  near(second.x_mm+second.width_mm+snap.dx_mm,100);
  near((second.x_mm+snap.dx_mm)-(first.x_mm+snap.dx_mm),30);
  const alone=snapTranslation(project([first,second]),[first,second],.3,.4,{pixelsPerMm:5});
  near(alone.dx_mm,.3);near(alone.dy_mm,.4);assert.deepEqual(alone.guides,[]);
});

test('canvas center, page edges and layout margins are usable targets',()=>{
  const item=panel('moving',20,30),data=project([item]);data.settings={composition:{layout:{margin_mm:6}}};
  near(snapTranslation(data,[item],-13,0,{pixelsPerMm:5}).dx_mm,-14);
  near(snapTranslation(data,[item],219,0,{pixelsPerMm:5}).dx_mm,220);
  near(snapTranslation(data,[item],459,0,{pixelsPerMm:5}).dx_mm,460);
});

test('turning snapping off returns the precise pointer movement and no guides',()=>{
  const item=panel('moving',20,30),target=panel('target',100,90);
  assert.deepEqual(snapTranslation(project([item,target]),[item],79.5,59.5,{enabled:false,pixelsPerMm:5}),{dx_mm:79.5,dy_mm:59.5,guides:[]});
});

test('rotated panels use their visible image edges and all calculations are read-only',()=>{
  const item=panel('rotated',20,30,{rotation:90}),target=panel('target',100,90),data=project([item,target]),before=structuredClone(data);
  const bounds=imageFrameBounds(item);near(bounds.left,25);near(bounds.right,35);near(bounds.top,25);near(bounds.bottom,45);
  const snap=snapTranslation(data,[item],64,0,{pixelsPerMm:5});near(snap.dx_mm,65);
  assert.deepEqual(data,before);
});

test('guides remain visible at different zoom levels and use only finite positions',()=>{
  const markup=snapGuideMarkup([{axis:'x',position:12,kind:'label'},{axis:'y',position:NaN,kind:'frame'}],{width_mm:180,height_mm:120});
  assert.match(markup,/data-snap-axis="x"/);assert.match(markup,/x1="12" x2="12"/);
  assert.match(markup,/stroke="#087fce"/);assert.match(markup,/vector-effect="non-scaling-stroke"/);
  assert.doesNotMatch(markup,/NaN|data-snap-axis="y"/);
  near(svgPixelsPerMm({getScreenCTM:()=>({a:0,b:4})}),4);
});
