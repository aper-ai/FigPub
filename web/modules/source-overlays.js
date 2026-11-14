import {state,analysisSelection,analysisGeometry} from './state.js';
import {escapeHtml as e} from './domain.js';
import {canonicalSource,wbScopedRois} from './analysis-domain.js';
import {sourceIdentityKey} from './channel-domain.js';
import {renderAdvancedOverlays} from './histology-fibers.js';

export function regionSvg(region,{color='#087f73',lineWidth=2,fillOpacity=.1,label='',scale=1}={}) {
  if(!region)return '';
  const attrs=`fill="${color}" fill-opacity="${fillOpacity}" stroke="${color}" stroke-width="${lineWidth}"`;
  let shape=region.type==='ellipse'?`<ellipse cx="${region.x+region.width/2}" cy="${region.y+region.height/2}" rx="${region.width/2}" ry="${region.height/2}" ${attrs}/>`:region.type==='polygon'?`<polygon points="${(region.points || []).map(point=>point.join(',')).join(' ')}" ${attrs}/>`:`<rect x="${region.x}" y="${region.y}" width="${region.width}" height="${region.height}" ${attrs}/>`;
  if(label)shape+=`<text x="${region.x+5*scale}" y="${Math.max(15*scale,region.y-6*scale)}" fill="${color}" font-size="${12*scale}" font-weight="600">${e(label)}</text>`;
  return shape;
}
export function renderSourceOverlays({asset,edit,width,height,analysis=false,sources=null,channelIds=null,first=true}) {
  const scale=Math.max(width,height)/750,lineWidth=scale*1.6,textSize=scale*12,crop=edit.crop,gel=edit.gel || {},showGel=state.tab==='gel';
  const allRois=state.project.settings.analysis_rois || [],keys=sources?.map(sourceIdentityKey),roiNumbers=new Map(allRois.map((item,index)=>[item.id,index+1]));
  const visibleRois=analysis&&state.analysisKind==='wb'?wbScopedRois(allRois,state.analysisParams.wb_draft_scope_id):allRois;
  const measurementRois=visibleRois.filter(roi=>keys?keys.includes(sourceIdentityKey(canonicalSource(roi.source,roi.asset_id))):roi.asset_id===asset.id);
  const current=state.sourceRoi,geometry=analysis&&state.analysisKind==='microscopy'?analysisGeometry():{};
  const selection=analysis&&state.analysisKind==='microscopy'?analysisSelection('microscopy'):null,result=selection&&!selection.stale?selection.result:null;
  const measuredIds=selection?.record?.params?.channels?.map(channel=>channel.id);
  const matchingObjects=(result?.objects || []).filter(object=>!channelIds?true:object.channel_id?channelIds.includes(object.channel_id):!measuredIds||measuredIds.some(id=>channelIds.includes(id)));
  const contourPath=state.showMicroOverlay!==false?matchingObjects.slice(0,500).map(object=>{const points=object.contour || [],stride=Math.max(1,Math.ceil(points.length/256));return points.length?points.filter((_,index)=>index%stride===0).map((point,index)=>`${index?'L':'M'}${point[0]} ${point[1]}`).join('')+'Z':'';}).join(''):'';
  const isCropped=crop&&(crop.x>0||crop.y>0||crop.width<width||crop.height<height),line=geometry.line;
  return `${isCropped?`<rect x="${crop.x}" y="${crop.y}" width="${crop.width}" height="${crop.height}" fill="none" stroke="#e8b24b" stroke-width="${lineWidth}" stroke-dasharray="${6*scale} ${4*scale}" pointer-events="none"/>`:''}
    ${showGel?(gel.lane_centers || []).map((center,index)=>`<g class="lane-overlay" data-lane-index="${index}"><line x1="${center}" x2="${center}" y1="${crop.y}" y2="${crop.y+crop.height}" stroke="#0c8a7b" stroke-opacity=".8" stroke-width="${lineWidth}" stroke-dasharray="${5*scale} ${4*scale}"/><rect x="${center-10*scale}" y="${Math.max(0,crop.y-21*scale)}" width="${20*scale}" height="${18*scale}" rx="${3*scale}" fill="#087f73"/><text x="${center}" y="${Math.max(0,crop.y-21*scale)+13*scale}" fill="white" text-anchor="middle" font-size="${textSize}">${index+1}</text></g>`).join(''):''}
    ${showGel?(gel.bands || []).map(band=>{const x=gel.lane_centers?.[band.lane_index];return x==null?'':`<line x1="${x-7*scale}" x2="${x+7*scale}" y1="${band.y}" y2="${band.y}" stroke="#0c8a7b" stroke-width="${lineWidth}" opacity=".7"/>`;}).join(''):''}
    ${showGel?(gel.marker_points || []).map(point=>{const x=gel.lane_centers?.[gel.marker_lane_index || 0] ?? crop.x+crop.width*.05;return `<g class="marker-overlay"><circle cx="${x}" cy="${point.y}" r="${4*scale}" fill="#d79430" stroke="white" stroke-width="${scale}"/><line x1="${x}" x2="${x+22*scale}" y1="${point.y}" y2="${point.y}" stroke="#d79430" stroke-width="${lineWidth}"/><text x="${x+26*scale}" y="${point.y+4*scale}" fill="#d79430" stroke="white" stroke-width="${.2*scale}" font-size="${textSize}">${e(point.size)} ${e(gel.marker_unit || 'kDa')}</text></g>`;}).join(''):''}
    ${measurementRois.map(item=>{const active=item.id===state.selectedRoiId,color=item.role==='control'?'#ba8128':'#078779';return `<g class="measurement-roi" data-roi-id="${item.id}">${regionSvg(item,{color,lineWidth:lineWidth*(active?1.8:1),fillOpacity:active?.18:.07})}<rect x="${item.x}" y="${Math.max(0,item.y-18*scale)}" width="${36*scale}" height="${18*scale}" rx="${2*scale}" fill="${color}"/><text x="${item.x+6*scale}" y="${Math.max(0,item.y-18*scale)+13*scale}" font-size="${textSize}" fill="white">R${roiNumbers.get(item.id)}</text></g>`;}).join('')}
    ${current&&!(analysis&&state.analysisKind==='microscopy'&&state.analysisParams.method==='line_profile')?`<g ${first?'id="current-source-roi"':''} class="current-source-roi">${regionSvg(current,{lineWidth:lineWidth*1.5,scale,label:`${current.width} × ${current.height} px`})}</g>`:''}
    ${geometry.background_roi&&state.analysisParams.method==='channel_intensity'&&state.analysisParams.background_method==='roi'?`<g class="background-region">${regionSvg(geometry.background_roi,{lineWidth,color:'#c68b2c',scale,label:'背景区域'})}</g>`:''}
    ${analysis&&state.analysisKind==='microscopy'&&state.analysisParams.method==='ihc_advanced'?renderAdvancedOverlays(geometry,scale):''}
    ${line&&state.analysisParams.method==='line_profile'?`<g class="analysis-line" pointer-events="none"><line x1="${line.x1}" y1="${line.y1}" x2="${line.x2}" y2="${line.y2}" stroke="#ffcb62" stroke-width="${Math.max(lineWidth,Number(state.analysisParams.line_width_px)||1)}"/><circle cx="${line.x1}" cy="${line.y1}" r="${4*scale}" fill="#ffcb62"/><text x="${line.x1+7*scale}" y="${line.y1-7*scale}" fill="#ffcb62" font-size="${textSize}">起点</text></g>`:''}
    ${contourPath?`<path class="analysis-contours" d="${contourPath}" fill="#26c39d" fill-opacity=".08" stroke="#1bc9a0" stroke-width="${lineWidth*.8}" pointer-events="none"/>`:''}
    ${state.sourcePolygon?.length?`<polyline class="polygon-draft" points="${state.sourcePolygon.map(point=>point.join(',')).join(' ')}" fill="none" stroke="#ffca61" stroke-width="${lineWidth}" stroke-dasharray="${5*scale} ${3*scale}"/>`:''}
    <g ${first?'id="live-roi-overlay"':''} class="live-roi-overlay"></g>`;
}
