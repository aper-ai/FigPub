import {state,selectedPanels} from './state.js';
import {selectionBounds} from './composition.js';
import {annotationBounds,textLines as splitTextLines} from './layout-engine.js';
import {escapeHtml as e, assetFor, fullCrop, previewUrl, retainedSegments, sourceXInPanel, figureFont, scaleBarGeometry} from './domain.js';
import {icon} from './icons.js';
import {channelLayout} from './channel-domain.js';
import {previewForChannels} from './channels.js';
import {formatLayoutNumber} from './numeric-input.js';
import {composerPixelsPerMm,composerPreviewSize} from './composer-viewport.js';

function captionWidth(text,fontSizeMm,family=figureFont(text)) {
  if(typeof document==='undefined')return String(text).length*fontSizeMm*.6;
  const context=document.createElement('canvas').getContext('2d');context.font=`${fontSizeMm*96/25.4}px "${family}"`;
  return context.measureText(text).width*25.4/96;
}
function splitPanelMarkup(panel,asset) {
  const width=panel.width_mm,height=panel.height_mm,layout=channelLayout(panel.channel_view,Math.max(asset.width_px,panel.crop.x+panel.crop.width),Math.max(asset.height_px,panel.crop.y+panel.crop.height),panel.crop),scaleX=width/layout.width_px,scaleY=height/layout.height_px;
  const tiles=layout.tiles.map(tile=>{
    const tileWidth=tile.width_px*scaleX,tileHeight=tile.height_px*scaleY,x=panel.flip_h?width-(tile.x_px+tile.width_px)*scaleX:tile.x_px*scaleX,y=panel.flip_v?height-(tile.y_px+tile.height_px)*scaleY:tile.y_px*scaleY;
    const child={...panel,id:`${panel.id}-${tile.id}`,channel_view:tile.view,crop:layout.crop,gel:{},width_mm:tileWidth,height_mm:tileHeight,rotation:0};
    let text=String(tile.mode==='merge'?'Merge':tile.name).replaceAll('\n',' ');const family=figureFont(text),padding=1.2,size=7*.352778;while(Array.from(text).length>1&&captionWidth(text,size,family)>Math.max(0,tileWidth-2*padding))text=Array.from(text).slice(0,-2).join('')+'…';const labelWidth=Math.min(tileWidth,captionWidth(text,size,family)+2*padding),labelHeight=size+2*padding;
    const clipId=`caption-${child.id}`;
    return `<g class="split-panel-tile" transform="translate(${x} ${y})">${panelImageMarkup(child,asset)}${panel.show_channel_labels===false?'':`<defs><clipPath id="${clipId}"><rect width="${tileWidth}" height="${tileHeight}"/></clipPath></defs><g clip-path="url(#${clipId})"><rect width="${labelWidth}" height="${labelHeight}" fill="#202723"/><text x="${padding}" y="${padding+size}" fill="#ffffff" font-size="${size}" font-family="${family}">${e(text)}</text></g>`}</g>`;
  }).join('');
  return `<g transform="rotate(${panel.rotation || 0} ${width/2} ${height/2})">${tiles}${gelAnnotationMarkup(panel,asset)}</g>`;
}

function gelAnnotationMarkup(panel,asset) {
  const crop=panel.crop || fullCrop(asset),width=panel.width_mm,height=panel.height_mm,gel=panel.gel || {},hidden=new Set(gel.hidden_lanes || []);
  const lanes=(gel.lane_centers || []).map((center,index)=>{
    if(hidden.has(index))return '';const relative=sourceXInPanel(panel,asset,center);if(relative===null || !gel.lane_names?.[index])return '';
    return `<text x="${relative*width}" y="${height+3}" text-anchor="middle" font-size="${(gel.font_size_pt || 7)*.352778}" fill="#333333" font-family="${figureFont(gel.lane_names[index])}">${e(gel.lane_names[index])}</text>`;
  }).join('');
  const markers=(gel.marker_points || []).map(point=>{
    let relative=(point.y-crop.y)/crop.height;if(panel.flip_v)relative=1-relative;if(relative<0||relative>1)return '';const y=relative*height;
    return `<line x1="${width}" x2="${width+1.2}" y1="${y}" y2="${y}" stroke="#333333" stroke-width=".176389"/><text x="${width+1.8}" y="${y+.8}" font-size="2.469446" fill="#333333">${e(point.size)}</text>`;
  }).join('')+(gel.marker_points?.length?`<text x="${width+1.8}" y="-1.8" font-size="2.469446" fill="#333333">${e(gel.marker_unit || 'kDa')}</text>`:'');
  return lanes+markers;
}

export function panelImageMarkup(panel, asset) {
  if (!asset) return `<rect width="${panel.width_mm}" height="${panel.height_mm}" fill="#f6eae7"/><text x="3" y="8" font-size="3" fill="#9d493f">源素材缺失</text>`;
  if(panel.channel_view?.mode==='split')return splitPanelMarkup(panel,asset);
  const crop=panel.crop || fullCrop(asset),width=panel.width_mm,height=panel.height_mm;
  const segments=retainedSegments(panel,asset),url=panel.channel_view?'':e(previewUrl(asset,panel.display));
  const content=segments.map((segment,index)=>{
    const start=segment.offset*width,pieceWidth=segment.fraction*width,scaleX=pieceWidth/segment.crop.width,scaleY=height/segment.crop.height,id=`clip-${panel.id}-${index}`;
    if(panel.channel_view) {
      const preview=previewForChannels(panel.channel_view,segment.crop,composerPreviewSize(pieceWidth,height));
      return preview.url?`<image href="${e(preview.url)}" x="${start}" y="0" width="${pieceWidth}" height="${height}" preserveAspectRatio="none"/>`:`<rect x="${start}" width="${pieceWidth}" height="${height}" fill="#e9ede9"/><text x="${start+pieceWidth/2}" y="${height/2}" text-anchor="middle" font-size="2.8" fill="#66776a">${preview.status==='error'?'预览不可用':'生成预览…'}</text>`;
    }
    return `<defs><clipPath id="${id}"><rect x="${start}" y="0" width="${pieceWidth}" height="${height}"/></clipPath></defs><g clip-path="url(#${id})"><image href="${url}" x="${start-segment.crop.x*scaleX}" y="${-segment.crop.y*scaleY}" width="${(asset.width_px || crop.width)*scaleX}" height="${(asset.height_px || crop.height)*scaleY}" preserveAspectRatio="none"/></g>`;
  }).join('');
  const gaps=segments.slice(0,-1).map((segment,index)=>{const x=(segment.offset+segment.fraction+segments[index+1].offset)/2*width;return `<line x1="${x}" x2="${x}" y1="0" y2="${height}" stroke="#6b7280" stroke-width=".159" stroke-dasharray=".8 .7"/>`;}).join('');
  const annotations=gelAnnotationMarkup(panel,asset);
  const bar=panel.scale_bar,barGeometry=bar?scaleBarGeometry(panel,asset,bar):null;let scaleBar='';
  if(barGeometry?.valid){const length=barGeometry.length_mm,color=e(bar.color || '#ffffff'),label=`${bar.length_um} µm`,margin=Math.min(2,width*.03,height*.07);scaleBar=`<g class="panel-scale-bar"><line x1="${width-length-margin}" x2="${width-margin}" y1="${height-margin}" y2="${height-margin}" stroke="${color}" stroke-width=".529167"/><text x="${width-length/2-margin}" y="${height-margin-1.5}" font-size="2.293057" text-anchor="middle" fill="${color}" font-family="${figureFont(label)}">${e(label)}</text></g>`;}
  return `<g transform="rotate(${panel.rotation || 0} ${width/2} ${height/2})"><g transform="translate(${width/2} ${height/2}) scale(${panel.flip_h?-1:1} ${panel.flip_v?-1:1}) translate(${-width/2} ${-height/2})">${content}${gaps}</g>${annotations}${scaleBar}</g>`;
}

function textLines(text,x,fontSizeMm) {
  return splitTextLines(text).map((line,index)=>`<tspan x="${x}" dy="${index?fontSizeMm*1.25:0}">${e(line)}</tspan>`).join('');
}

function annotationMarkup(annotation) {
  const width = annotation.width_mm ?? 20, height = annotation.height_mm ?? 6, selected = annotation.id === state.selectedAnnotationId;
  let content = '';
  if (annotation.type === 'text') content = `<text x="0" y="${(annotation.font_size_pt || 8) * .35278}" font-size="${(annotation.font_size_pt || 8) * .35278}" fill="${e(annotation.color || '#222222')}" font-family="${figureFont(annotation.text)}" text-anchor="${e(annotation.anchor || 'start')}">${textLines(annotation.text || '文字',0,(annotation.font_size_pt || 8)*.35278)}</text>`;
  if (annotation.type === 'rect') content = `<rect width="${width}" height="${height}" fill="none" stroke="${e(annotation.color || '#222222')}" stroke-width=".35"/>`;
  if (annotation.type === 'line') content = `<line x1="0" y1="0" x2="${width}" y2="${height}" stroke="${e(annotation.color || '#222222')}" stroke-width=".35"/>`;
  const bounds=annotationBounds(annotation),left=bounds.left-annotation.x_mm-1,top=bounds.top-annotation.y_mm-1;
  return `<g class="figure-annotation" data-annotation-id="${annotation.id}" transform="translate(${annotation.x_mm} ${annotation.y_mm})"><rect x="${left}" y="${top}" width="${bounds.width_mm + 2}" height="${bounds.height_mm + 2}" fill="transparent"/>${content}${selected ? `<rect x="${left}" y="${top}" width="${bounds.width_mm + 2}" height="${bounds.height_mm + 2}" fill="none" stroke="#078779" stroke-width=".3" stroke-dasharray="1 .7"/>` : ''}</g>`;
}

export function panelContentMarkup(panel,asset,{selected=false,handles=false}={}) {
  return `        ${panelImageMarkup(panel, asset)}
        ${panel.label ? `<text class="panel-label" x="0" y="-1.8" font-size="${(panel.label_font_size_pt || 10)*.352778}" font-weight="700" fill="#1d2522" font-family="${figureFont(panel.label)}">${textLines(panel.label,0,(panel.label_font_size_pt || 10)*.352778)}</text>` : ''}
        ${panel.title ? `<text x="${panel.width_mm/2}" y="-1.8" text-anchor="middle" font-size="${(panel.title_font_size_pt || 8)*.352778}" fill="#222222" font-family="${figureFont(panel.title)}">${textLines(panel.title,panel.width_mm/2,(panel.title_font_size_pt || 8)*.352778)}</text>` : ''}
        <rect class="panel-hit" width="${panel.width_mm}" height="${panel.height_mm}" fill="transparent" stroke="${selected ? '#078779' : 'transparent'}" stroke-width="${selected ? '.35' : '0'}"/>
        ${selected && handles ? `<rect data-handle="se" x="${panel.width_mm - 1}" y="${panel.height_mm - 1}" width="2" height="2" fill="white" stroke="#078779" stroke-width=".3"/><rect data-handle="nw" x="-1" y="-1" width="2" height="2" fill="white" stroke="#078779" stroke-width=".3"/>` : ''}
`;
}

export function renderComposer() {
  const project = state.project, figure = project.figure, chosen=selectedPanels(), selectedIds=new Set(chosen.map(panel=>panel.id)), bounds=chosen.length>1?selectionBounds(chosen):null;
  const pixelsPerMm = composerPixelsPerMm();
  const svgWidth = figure.width_mm * pixelsPerMm, svgHeight = figure.height_mm * pixelsPerMm;
  return `<section class="composer-workspace">
    <div class="stage-toolbar"><div class="stage-title">${icon('layers')}<strong>Figure 画布</strong><span class="soft-tag">${formatLayoutNumber(figure.width_mm)} × ${formatLayoutNumber(figure.height_mm)} mm</span></div><div class="toolbar-actions"><button class="icon-button ${state.snap ? 'active' : ''}" data-action="toggle-snap" title="对齐吸附">${icon('grid')}</button><span class="toolbar-divider"></span><button class="icon-button" data-action="zoom-out" title="缩小">${icon('minus')}</button><span class="zoom-value">${Math.round(state.figureZoom * 100)}%</span><button class="icon-button" data-action="zoom-in" title="放大">${icon('plus')}</button><button class="icon-button" data-action="zoom-fit" title="适合窗口">${icon('fit')}</button></div></div>
    <div class="composition-toolbar"><div class="segmented subtle"><button class="${state.figureTool === 'select' ? 'selected' : ''}" data-action="figure-tool" data-tool="select">${icon('cursor')}选择</button><button data-action="add-annotation" data-type="text">${icon('text')}文字</button><button data-action="add-annotation" data-type="rect">${icon('rect')}区域框</button><button data-action="add-annotation" data-type="line">${icon('line')}线条</button></div><div class="composition-import-actions"><button class="button small" data-action="import">${icon('upload')}批量导入</button><button class="button small" data-action="compose-all-assets" title="将素材库中尚未加入的图像加入并排列">加入已有素材</button><button class="button small" data-action="auto-layout">${icon('grid')}自动排版</button></div></div>
    <div class="artboard-viewport" data-drop-zone="canvas"><div class="artboard-frame"><div class="canvas-ruler-label">${formatLayoutNumber(figure.width_mm)} mm</div><svg id="figure-svg" class="figure-svg" data-preview-scale="${pixelsPerMm}" font-family="DejaVu Sans" width="${svgWidth}" height="${svgHeight}" viewBox="0 0 ${figure.width_mm} ${figure.height_mm}" style="background:${e(figure.background || '#ffffff')}" aria-label="科研图稿画布">
      <rect data-canvas-background="true" width="${figure.width_mm}" height="${figure.height_mm}" fill="${e(figure.background || '#ffffff')}"/>
      ${project.panels.map(panel=>`<g class="figure-panel ${selectedIds.has(panel.id)?'is-selected':''}" data-panel-id="${panel.id}" transform="translate(${panel.x_mm} ${panel.y_mm})">${panelContentMarkup(panel,assetFor(project,panel.asset_id),{selected:selectedIds.has(panel.id),handles:chosen.length===1})}</g>`).join('')}
      ${project.annotations.map(annotationMarkup).join('')}
      ${bounds?`<g data-selection-outline transform="translate(${bounds.left} ${bounds.top})"><rect data-selection-box width="${bounds.width_mm}" height="${bounds.height_mm}" fill="none" stroke="#078779" stroke-width=".3" stroke-dasharray="1.2 .8" pointer-events="none"/><rect data-selection-handle="nw" x="-1.2" y="-1.2" width="2.4" height="2.4" fill="white" stroke="#078779" stroke-width=".3"/><rect data-selection-handle="se" x="${bounds.width_mm-1.2}" y="${bounds.height_mm-1.2}" width="2.4" height="2.4" fill="white" stroke="#078779" stroke-width=".3"/></g>`:''}
      <g id="snap-guides" pointer-events="none"></g>
    </svg>${!project.panels.length && !project.annotations.length ? `<div class="empty-artboard"><span>${icon('plus')}</span><strong>向画布添加素材</strong><p>批量拖入文件即可自动排列，也可从左侧添加素材</p></div>` : ''}</div></div>
    <div class="source-caption"><span>${project.panels.length} 个面板 <b>·</b> ${project.annotations.length} 个标注 <b>·</b> ${project.settings.font_family}</span><span>Shift 点选 / 空白框选 · 拖动整体移动 · 方向键微调</span></div>
  </section>`;
}
