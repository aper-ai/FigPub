import {annotationBounds,panelLabelPosition} from './layout-engine.js';

// Snap distances are measured on screen, independently of the figure's units
// and zoom. Image edges and label baselines are separate alignment features:
// a large label must never pull an image edge away from another image edge.
const SNAP_DISTANCE_PX = 7;
const EPSILON = 1e-8;

export function imageFrameBounds(panel) {
  const angle=(panel.rotation || 0)*Math.PI/180,width=panel.width_mm,height=panel.height_mm;
  const rotatedWidth=Math.abs(width*Math.cos(angle))+Math.abs(height*Math.sin(angle));
  const rotatedHeight=Math.abs(height*Math.cos(angle))+Math.abs(width*Math.sin(angle));
  const left=panel.x_mm+(width-rotatedWidth)/2,top=panel.y_mm+(height-rotatedHeight)/2;
  return {left,top,right:left+rotatedWidth,bottom:top+rotatedHeight};
}

function frame(item) {return item.asset_id?imageFrameBounds(item):annotationBounds(item);}
function axes(bounds) {return {x:[bounds.left,(bounds.left+bounds.right)/2,bounds.right],y:[bounds.top,(bounds.top+bounds.bottom)/2,bounds.bottom]};}
function addFrames(features,bounds,kind='frame') {
  const values=axes(bounds);
  for(const axis of ['x','y'])for(const position of values[axis])features[axis].push({position,kind});
}
function addLabel(features,panel) {
  if(!panel.asset_id||!panel.label||panel.label_mode==='none')return;
  const label=panelLabelPosition(panel);
  features.x.push({position:label.x_mm,kind:'label'});
  features.y.push({position:label.y_mm,kind:'label'});
}
function movingFeatures(objects) {
  const features={x:[],y:[]},bounds=objects.map(frame);
  if(bounds.length)addFrames(features,{left:Math.min(...bounds.map(box=>box.left)),right:Math.max(...bounds.map(box=>box.right)),top:Math.min(...bounds.map(box=>box.top)),bottom:Math.max(...bounds.map(box=>box.bottom))});
  for(const item of objects)addLabel(features,item);
  return features;
}
function targetFeatures(project,excluded) {
  const features={x:[],y:[]},figure=project.figure;
  addFrames(features,{left:0,top:0,right:figure.width_mm,bottom:figure.height_mm},'canvas');
  const margin=project.settings?.composition?.layout?.margin_mm;
  if(Number.isFinite(margin)&&margin>0&&margin<Math.min(figure.width_mm,figure.height_mm)/2) {
    for(const position of [margin,figure.width_mm-margin])features.x.push({position,kind:'canvas'});
    for(const position of [margin,figure.height_mm-margin])features.y.push({position,kind:'canvas'});
  }
  for(const item of [...(project.panels || []),...(project.annotations || [])])if(!excluded.has(item.id)) {
    addFrames(features,frame(item));addLabel(features,item);
  }
  return features;
}
function nearest(moving,targets,delta,tolerance) {
  let best=null;
  for(const source of moving)for(const target of targets) {
    if(target.kind!=='canvas'&&source.kind!==target.kind)continue;
    const adjustment=target.position-source.position-delta,distance=Math.abs(adjustment);
    if(distance>tolerance+EPSILON)continue;
    if(!best||distance<best.distance-EPSILON||Math.abs(distance-best.distance)<EPSILON&&target.kind==='label'&&best.kind!=='label')best={adjustment,distance,position:target.position,kind:target.kind};
  }
  return best;
}

/** Return one translation for the whole selection; internal spacing is intact. */
export function snapTranslation(project,objects,dx_mm,dy_mm,{enabled=true,pixelsPerMm=1,thresholdPx=SNAP_DISTANCE_PX}={}) {
  if(!enabled||!objects.length)return {dx_mm,dy_mm,guides:[]};
  const scale=Number.isFinite(pixelsPerMm)&&pixelsPerMm>0?pixelsPerMm:1;
  const tolerance=thresholdPx/scale,excluded=new Set(objects.map(item=>item.id));
  const sources=movingFeatures(objects),targets=targetFeatures(project,excluded),guides=[];
  const x=nearest(sources.x,targets.x,dx_mm,tolerance),y=nearest(sources.y,targets.y,dy_mm,tolerance);
  if(x){dx_mm+=x.adjustment;guides.push({axis:'x',position:x.position,kind:x.kind});}
  if(y){dy_mm+=y.adjustment;guides.push({axis:'y',position:y.position,kind:y.kind});}
  return {dx_mm,dy_mm,guides};
}

export function svgPixelsPerMm(svg) {
  const matrix=svg?.getScreenCTM?.();
  const scale=matrix?Math.hypot(matrix.a,matrix.b):Number(svg?.dataset?.previewScale);
  return Number.isFinite(scale)&&scale>0?scale:1;
}

/** Editor-only overlays; never persisted in projects or included in exports. */
export function snapGuideMarkup(guides,figure) {
  return guides.filter(guide=>Number.isFinite(guide.position)).map(guide=>{
    const coordinates=guide.axis==='x'?`x1="${guide.position}" x2="${guide.position}" y1="0" y2="${figure.height_mm}"`:`x1="0" x2="${figure.width_mm}" y1="${guide.position}" y2="${guide.position}"`;
    return `<g data-snap-axis="${guide.axis}" data-snap-kind="${guide.kind}" pointer-events="none" aria-hidden="true"><line ${coordinates} stroke="#ffffff" stroke-width="3.5" vector-effect="non-scaling-stroke"/><line ${coordinates} stroke="#087fce" stroke-width="1.5" stroke-dasharray="5 3" vector-effect="non-scaling-stroke"/></g>`;
  }).join('');
}
