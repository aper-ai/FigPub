import {state,emit} from './state.js';

// Display geometry belongs to the mounted viewport, never to the saved recipe.
// Keep one measurement for SVG sizing and channel preview resolution selection.
let availableSize=null,observedViewport=null,observer=null,resizeFrame=null,previewFrame=null,resetScroll=false;

export function fitComposerScale(figure,available,zoom=1) {
  if(!available || !(available.width>0 && available.height>0 && figure?.width_mm>0 && figure?.height_mm>0))return 1;
  return Math.min(available.width/figure.width_mm,available.height/figure.height_mm)*zoom;
}

export function composerPixelsPerMm() {
  return fitComposerScale(state.project?.figure,availableSize,state.figureZoom);
}

export function composerPreviewSize(widthMm,heightMm,pixelsPerMm=composerPixelsPerMm(),devicePixelRatio=globalThis.window?.devicePixelRatio || 1) {
  const target=Math.max(widthMm,heightMm)*pixelsPerMm*Math.max(1,devicePixelRatio)*1.5;
  return [256,512,1024,1600].find(size=>size>=target) || 1600;
}

export function viewportContentSize(viewport) {
  const style=viewport.ownerDocument.defaultView.getComputedStyle(viewport);
  const pixels=name=>Number.parseFloat(style[name]) || 0;
  const width=viewport.clientWidth-pixels('paddingLeft')-pixels('paddingRight');
  const height=viewport.clientHeight-pixels('paddingTop')-pixels('paddingBottom');
  return width>0 && height>0?{width,height}:null;
}

export function requestComposerFit() {
  state.figureZoom=1;
  resetScroll=true;
}

function disconnect() {
  observer?.disconnect();observer=null;observedViewport=null;
  if(resizeFrame!==null)window.cancelAnimationFrame(resizeFrame);
  if(previewFrame!==null)window.cancelAnimationFrame(previewFrame);
  resizeFrame=previewFrame=null;
}

function scheduleResize() {
  if(resizeFrame!==null)return;
  resizeFrame=window.requestAnimationFrame(()=>{resizeFrame=null;syncComposerViewport();});
}

export function syncComposerViewport() {
  const viewport=globalThis.document?.querySelector?.('.artboard-viewport');
  const svg=viewport?.querySelector('#figure-svg');
  if(state.tab!=='compose' || !viewport || !svg) {disconnect();return;}
  if(observedViewport!==viewport) {
    observer?.disconnect();observedViewport=viewport;
    const Observer=viewport.ownerDocument.defaultView.ResizeObserver;
    observer=Observer?new Observer(scheduleResize):null;
    observer?.observe(viewport);
  }
  // A pointer gesture uses the captured SVG transform until it commits. Its
  // final render applies any pending viewport change without moving coordinates.
  if(state.interactionLock)return;
  const figure=state.project.figure;
  let scale;
  // Existing zoom scrollbars can disappear after fitting. Remeasure the real
  // client box so their former width/height does not leave an undersized canvas.
  for(let pass=0;pass<3;pass++) {
    const measured=viewportContentSize(viewport);
    if(!measured)return;
    availableSize=measured;
    scale=composerPixelsPerMm();
    const width=figure.width_mm*scale,height=figure.height_mm*scale;
    const changed=Math.abs(Number(svg.getAttribute('width'))-width)>.001 || Math.abs(Number(svg.getAttribute('height'))-height)>.001;
    if(!changed)break;
    svg.setAttribute('width',String(width));svg.setAttribute('height',String(height));
  }
  if(resetScroll) {viewport.scrollTop=0;viewport.scrollLeft=0;resetScroll=false;}
  // Only channel previews need another render when the mounted size differs
  // from the provisional markup. Ordinary canvas fitting updates the same SVG
  // directly, preserving focus and avoiding render/ResizeObserver feedback.
  if(Math.abs(Number(svg.dataset.previewScale)-scale)>.001 && state.project.panels.some(panel=>panel.channel_view) && previewFrame===null) {
    previewFrame=window.requestAnimationFrame(()=>{previewFrame=null;if(state.tab==='compose' && !state.interactionLock)emit();});
  }
}
