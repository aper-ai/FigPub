import {state, emit, selectedAsset, selectedPanels, selectPanel, commitGesture,captureRecipe,restoreRecipe,setAnalysisGeometry,notify} from './state.js';
import {clone, round, normalizeRoi,clamp,assetFor} from './domain.js';
import {panelContentMarkup} from './composer-view.js';
import {estimateMarkerAt} from './gel.js';
import {regionSvg} from './source-overlays.js';
import {selectionBounds} from './composition.js';
import {panelBounds} from './layout-engine.js';

let gesture = null, animationFrame = null, pendingEvent = null;

function svgPoint(svg, event) {
  const point = new DOMPoint(event.clientX, event.clientY), matrix = svg.getScreenCTM();
  return matrix ? point.matrixTransform(matrix.inverse()) : {x:0,y:0};
}
function showLiveRoi(svg, roi, sourceWidth) {
  const scale = sourceWidth / 700, group = svg.querySelector('.live-roi-overlay'); if (!group) return;
  group.innerHTML = regionSvg(roi,{color:state.analysisRegionTarget==='background_roi'?'#c68b2c':'#087f73',scale,lineWidth:Math.max(1,scale*1.6),label:`${roi.width} × ${roi.height} px`});
}

export function finishPolygon() {
  const points=state.sourcePolygon;if(points.length<3){notify('多边形至少需要三个顶点','info');return;}
  const xs=points.map(point=>point[0]),ys=points.map(point=>point[1]),left=Math.min(...xs),top=Math.min(...ys),width=Math.max(...xs)-left,height=Math.max(...ys)-top;
  if(width<1||height<1){notify('多边形区域不能退化为一条线','info');return;}
  state.sourcePolygon=[];setAnalysisGeometry(state.analysisRegionTarget,{type:'polygon',x:left,y:top,width,height,points:clone(points)});state.sourceTool='select';emit();
}

function beginGesture(event) {
  if (![0,1].includes(event.button) || state.modal || state.busy) return;
  const sourceStage=event.target.closest('.source-stage');
  const sourceSvg = event.target.closest('[data-source-canvas]') || ((state.spacePressed||event.button===1)&&sourceStage?sourceStage.querySelector('[data-source-canvas]'):null);
  if (sourceSvg) {
    const asset = selectedAsset(); if (!asset) return;
    if(state.spacePressed||event.button===1){event.preventDefault();state.interactionLock=true;gesture={kind:'pan',svg:sourceSvg,startClient:{x:event.clientX,y:event.clientY},originPan:{...state.sourcePan},pointerId:event.pointerId};sourceSvg.classList.add('grabbing');try{sourceSvg.setPointerCapture(event.pointerId);}catch{}return;}
    const dimensions={width_px:Number(sourceSvg.dataset.sourceWidth)||asset.width_px,height_px:Number(sourceSvg.dataset.sourceHeight)||asset.height_px},point=svgPoint(sourceSvg,event);
    if (point.x < 0 || point.y < 0 || point.x > dimensions.width_px || point.y > dimensions.height_px) return;
    if(!(state.tab==='analysis'&&state.analysisKind==='microscopy'))state.sourcePointerIdentity=JSON.parse(sourceSvg.dataset.sourceIdentity);
    if (state.sourceTool === 'marker') {state.modal = {type:'marker', y:Math.round(point.y)}; emit(); return;}
    if (state.sourceTool === 'estimate') {estimateMarkerAt(point.y); return;}
    if(state.sourceTool==='polygon'&&state.tab==='analysis'&&state.analysisKind==='microscopy'){
      event.preventDefault();const vertex=[Math.round(clamp(point.x,0,dimensions.width_px-1)),Math.round(clamp(point.y,0,dimensions.height_px-1))],previous=state.sourcePolygon.at(-1);
      if(!previous||Math.hypot(vertex[0]-previous[0],vertex[1]-previous[1])>1)state.sourcePolygon.push(vertex);emit();return;
    }
    if (!['roi','ellipse','analysis-line'].includes(state.sourceTool)) {
      const roi = event.target.closest('[data-roi-id]'); state.selectedRoiId = roi?.dataset.roiId || null; emit(); return;
    }
    event.preventDefault(); state.interactionLock = true;
    gesture = {kind:state.sourceTool==='analysis-line'?'analysis-line':'roi',shape:state.sourceTool==='ellipse'?'ellipse':'rectangle',svg:sourceSvg,start:point,asset:dimensions,pointerId:event.pointerId};
    try {sourceSvg.setPointerCapture(event.pointerId);} catch {}
    return;
  }
  const figureSvg = event.target.closest('#figure-svg'); if (!figureSvg||event.button!==0) return;
  // Canvas gestures prevent the browser's normal focus transfer. Commit the
  // focused inspector against its original selection before choosing another
  // object, then release that input so deferred renders can show the new one.
  const pendingInput=document.activeElement;
  if(pendingInput?.matches?.('[data-pending-edit]')) {
    pendingInput.dispatchEvent(new Event('change',{bubbles:true}));
    pendingInput.blur();
  }
  const panelElement=event.target.closest('[data-panel-id]'), annotationElement=event.target.closest('[data-annotation-id]');
  const selectionHandle=event.target.dataset.selectionHandle;
  if(!panelElement && !annotationElement && !selectionHandle) {
    event.preventDefault();state.interactionLock=true;
    const rectangle=document.createElementNS('http://www.w3.org/2000/svg','rect');
    for(const [name,value] of Object.entries({fill:'#07877918',stroke:'#078779','stroke-width':'.25','stroke-dasharray':'1 .7','pointer-events':'none'}))rectangle.setAttribute(name,value);
    figureSvg.append(rectangle);
    gesture={kind:'marquee',svg:figureSvg,start:svgPoint(figureSvg,event),rectangle,pointerId:event.pointerId,additive:event.shiftKey||event.ctrlKey||event.metaKey,previous:[...(state.selectedPanelIds || [])]};
    try{figureSvg.setPointerCapture(event.pointerId);}catch{}return;
  }
  event.preventDefault();
  const panelId=panelElement?.dataset.panelId, annotationId=annotationElement?.dataset.annotationId;
  const handle=selectionHandle || event.target.dataset.handle || null;
  if(panelId) {
    if(event.shiftKey||event.ctrlKey||event.metaKey){selectPanel(panelId,{toggle:true});return;}
    selectPanel(panelId,{preserve:true});
  } else if(annotationId) {state.selectedAnnotationId=annotationId;state.selectedPanelId=null;state.selectedPanelIds=[];state.compositionInspectorTab='selection';emit();}
  const svg=document.getElementById('figure-svg');state.interactionLock=true;
  if(panelId || selectionHandle) {
    const panels=selectedPanels(), origin=panels.length===1?clone(panels[0]):selectionBounds(panels);
    if(!panels.length){state.interactionLock=false;return;}
    gesture={kind:'panels',panelId,handle,svg,start:svgPoint(svg,event),startClient:{x:event.clientX,y:event.clientY},before:captureRecipe(),origin,pointerId:event.pointerId,ids:new Set(panels.map(panel=>panel.id)),
      items:panels.map(item=>({item,origin:clone(item),element:svg.querySelector(`[data-panel-id="${item.id}"]`)})),outline:svg.querySelector('[data-selection-outline]')};
  } else {
    const item=state.project.annotations.find(entry=>entry.id===annotationId);
    gesture={kind:'annotation',handle,svg,startClient:{x:event.clientX,y:event.clientY},element:svg.querySelector(`[data-annotation-id="${annotationId}"]`),item,start:svgPoint(svg,event),before:captureRecipe(),origin:clone(item),pointerId:event.pointerId};
  }
  try{svg.setPointerCapture(event.pointerId);}catch{}
}

function snappedPosition(item, x, y, origin, excluded=new Set([item.id])) {
  const figure = state.project.figure, guides = [], tolerance = 1.1;
  if (!state.snap) return {x,y,guides};
  const targetsX = [0,figure.width_mm/2,figure.width_mm], targetsY = [0,figure.height_mm/2,figure.height_mm];
  for (const panel of state.project.panels) if (!excluded.has(panel.id)) {
    const bounds=panelBounds(panel);
    targetsX.push(bounds.left,bounds.left+bounds.width_mm/2,bounds.right);
    targetsY.push(bounds.top,bounds.top+bounds.height_mm/2,bounds.bottom);
  }
  let minX=tolerance,minY=tolerance;
  for(const offset of [0,origin.width_mm/2,origin.width_mm]) for(const target of targetsX) if(Math.abs(x+offset-target)<minX) {minX=Math.abs(x+offset-target); guides[0]={axis:'x',position:target}; guides[2]=target-offset;}
  for(const offset of [0,origin.height_mm/2,origin.height_mm]) for(const target of targetsY) if(Math.abs(y+offset-target)<minY) {minY=Math.abs(y+offset-target); guides[1]={axis:'y',position:target}; guides[3]=target-offset;}
  return {x:guides[2] ?? x,y:guides[3] ?? y,guides:guides.slice(0,2).filter(Boolean)};
}

function updateGesture(event) {
  if (!gesture) return;
  if(gesture.kind==='pan'){state.sourcePan={x:gesture.originPan.x+event.clientX-gesture.startClient.x,y:gesture.originPan.y+event.clientY-gesture.startClient.y};document.querySelectorAll('[data-source-canvas]').forEach(svg=>{svg.style.transform=`translate(${state.sourcePan.x}px,${state.sourcePan.y}px) scale(${state.sourceZoom})`;});return;}
  const point=svgPoint(gesture.svg,event);
  if(gesture.kind==='roi') {const roi=normalizeRoi(gesture.start,point,gesture.asset);if(gesture.shape==='ellipse')roi.type='ellipse';gesture.roi=roi;showLiveRoi(gesture.svg,roi,gesture.asset.width_px);return;}
  if(gesture.kind==='analysis-line') {
    const width=gesture.asset.width_px,height=gesture.asset.height_px,line={x1:round(clamp(gesture.start.x,0,width-1)),y1:round(clamp(gesture.start.y,0,height-1)),x2:round(clamp(point.x,0,width-1)),y2:round(clamp(point.y,0,height-1))};gesture.line=line;
    const group=gesture.svg.querySelector('.live-roi-overlay');group.innerHTML=`<line x1="${line.x1}" y1="${line.y1}" x2="${line.x2}" y2="${line.y2}" stroke="#ffcb62" stroke-width="${Math.max(width/400,Number(state.analysisParams.line_width_px)||1)}"/>`;return;
  }
  if(gesture.kind==='marquee') {
    const left=Math.min(gesture.start.x,point.x),top=Math.min(gesture.start.y,point.y),width=Math.abs(point.x-gesture.start.x),height=Math.abs(point.y-gesture.start.y);
    gesture.box={left,top,right:left+width,bottom:top+height,width,height};
    for(const [name,value] of Object.entries({x:left,y:top,width,height}))gesture.rectangle.setAttribute(name,value);
    return;
  }
  if(!gesture.moved && Math.hypot(event.clientX-gesture.startClient.x,event.clientY-gesture.startClient.y)<3)return;
  gesture.moved=true;
  const {origin,item,element,handle}=gesture, deltaX=point.x-gesture.start.x,deltaY=point.y-gesture.start.y;
  if(gesture.kind==='panels') {
    if(handle) {
      const sign=handle==='nw'?-1:1,keepRatio=gesture.items.length>1 || state.lockAspect!==false && !event.shiftKey;
      let width=Math.max(2,origin.width_mm+sign*deltaX),height=Math.max(2,origin.height_mm+sign*deltaY);
      if(keepRatio) {
        const horizontal=sign*deltaX/origin.width_mm,vertical=sign*deltaY/origin.height_mm,change=Math.abs(horizontal)>=Math.abs(vertical)?horizontal:vertical;
        const minimum=Math.max(...gesture.items.map(entry=>2/Math.min(entry.origin.width_mm,entry.origin.height_mm)));
        const scale=Math.max(minimum,1+change);width=origin.width_mm*scale;height=origin.height_mm*scale;
      }
      const scaleX=width/origin.width_mm,scaleY=height/origin.height_mm;
      const x=handle==='nw'?origin.x_mm+origin.width_mm-width:origin.x_mm,y=handle==='nw'?origin.y_mm+origin.height_mm-height:origin.y_mm;
      for(const entry of gesture.items) {
        Object.assign(entry.item,{x_mm:round(x+(entry.origin.x_mm-origin.x_mm)*scaleX),y_mm:round(y+(entry.origin.y_mm-origin.y_mm)*scaleY),width_mm:round(entry.origin.width_mm*scaleX),height_mm:round(entry.origin.height_mm*scaleY)});
        entry.element?.setAttribute('transform',`translate(${entry.item.x_mm} ${entry.item.y_mm})`);
        if(entry.element)entry.element.innerHTML=panelContentMarkup(entry.item,assetFor(state.project,entry.item.asset_id),{selected:true,handles:gesture.items.length===1});
      }
      const bounds=selectionBounds(gesture.items.map(entry=>entry.item));updateSelectionOutline(bounds.left,bounds.top,bounds.width_mm,bounds.height_mm);
    } else {
      const snapped=snappedPosition({},origin.x_mm+deltaX,origin.y_mm+deltaY,origin,gesture.ids),dx=snapped.x-origin.x_mm,dy=snapped.y-origin.y_mm;
      for(const entry of gesture.items) {entry.item.x_mm=round(entry.origin.x_mm+dx);entry.item.y_mm=round(entry.origin.y_mm+dy);entry.element?.setAttribute('transform',`translate(${entry.item.x_mm} ${entry.item.y_mm})`);}
      updateSelectionOutline(snapped.x,snapped.y,origin.width_mm,origin.height_mm);showGuides(snapped.guides);
    }
  } else {
    const snapped=snappedPosition(item,origin.x_mm+deltaX,origin.y_mm+deltaY,origin);
    item.x_mm=round(snapped.x);item.y_mm=round(snapped.y);element.setAttribute('transform',`translate(${item.x_mm} ${item.y_mm})`);showGuides(snapped.guides);
  }
}

function updateSelectionOutline(x,y,width,height) {
  const outline=gesture.outline;if(!outline)return;
  outline.setAttribute('transform',`translate(${x} ${y})`);
  const box=outline.querySelector('[data-selection-box]'),handle=outline.querySelector('[data-selection-handle="se"]');
  box?.setAttribute('width',width);box?.setAttribute('height',height);handle?.setAttribute('x',width-1.2);handle?.setAttribute('y',height-1.2);
}
function showGuides(guides) {
  gesture.svg.querySelector('#snap-guides').innerHTML=guides.map(guide=>guide.axis==='x'?`<line x1="${guide.position}" x2="${guide.position}" y1="0" y2="${state.project.figure.height_mm}" stroke="#7ca998" stroke-width=".2" stroke-dasharray="1 1"/>`:`<line x1="0" x2="${state.project.figure.width_mm}" y1="${guide.position}" y2="${guide.position}" stroke="#7ca998" stroke-width=".2" stroke-dasharray="1 1"/>`).join('');
}

function moveGesture(event) {
  if(!gesture || event.pointerId!==gesture.pointerId)return;
  event.preventDefault();pendingEvent=event;
  if(!animationFrame) animationFrame=requestAnimationFrame(()=>{animationFrame=null;if(pendingEvent)updateGesture(pendingEvent);pendingEvent=null;});
}

function finishGesture(event, cancelled=false) {
  if(!gesture || event.pointerId!==gesture.pointerId)return;
  if(animationFrame){cancelAnimationFrame(animationFrame);animationFrame=null;}
  if(!cancelled)updateGesture(event);
  const finished=gesture;gesture=null;pendingEvent=null;state.interactionLock=false;
  if(finished.kind==='marquee') {
    finished.rectangle.remove();
    if(!cancelled) {
      const ids=new Set(finished.additive?finished.previous:[]),box=finished.box;
      if(box && box.width>1 && box.height>1)for(const panel of state.project.panels) {
        const bounds=panelBounds(panel);
        if(bounds.right>=box.left && bounds.left<=box.right && bounds.bottom>=box.top && bounds.top<=box.bottom) {
          if(panel.group_id)state.project.panels.filter(item=>item.group_id===panel.group_id).forEach(item=>ids.add(item.id));else ids.add(panel.id);
        }
      }
      state.selectedPanelIds=[...ids];state.selectedPanelId=state.selectedPanelIds.at(-1) || null;state.selectedAnnotationId=null;state.compositionInspectorTab=ids.size?'selection':'layout';
      const selected=selectedPanels().find(panel=>panel.id===state.selectedPanelId);if(selected)state.selectedAssetId=selected.asset_id;
    }
    emit();
  }
  else if(finished.kind==='pan'){if(cancelled)state.sourcePan=finished.originPan;emit();}
  else if(finished.kind==='roi') {if(!cancelled&&finished.roi&&finished.roi.width>2&&finished.roi.height>2){if(state.tab==='analysis'&&state.analysisKind==='microscopy')setAnalysisGeometry(state.analysisRegionTarget,finished.roi);else state.sourceRoi=finished.roi;}emit();}
  else if(finished.kind==='analysis-line') {if(!cancelled&&finished.line&&Math.hypot(finished.line.x2-finished.line.x1,finished.line.y2-finished.line.y1)>=1)setAnalysisGeometry('line',finished.line);emit();}
  else if(cancelled){restoreRecipe(finished.before);emit();}
  // Keep a multiselection intact while dragging, then resolve a plain click to
  // its object. selectPanel still expands a real group to all of its members.
  else if(finished.kind==='panels'&&!finished.moved&&!finished.handle&&finished.panelId){selectPanel(finished.panelId);}
  else {commitGesture(finished.before,finished.handle?'缩放面板':'移动对象');emit();}
}

export function bindGestures() {
  document.addEventListener('pointerdown',beginGesture);
  document.addEventListener('pointermove',moveGesture,{passive:false});
  document.addEventListener('pointerup',event=>finishGesture(event));
  document.addEventListener('pointercancel',event=>finishGesture(event,true));
  window.addEventListener('blur',()=>{if(gesture)finishGesture({pointerId:gesture.pointerId},true);});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&gesture)finishGesture({pointerId:gesture.pointerId},true);});
  document.addEventListener('dblclick',event=>{if(state.sourceTool==='polygon'&&event.target.closest('[data-source-canvas]')){event.preventDefault();finishPolygon();}});
}
