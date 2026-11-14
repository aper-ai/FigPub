import {channelLayout} from './channel-domain.js';

/**
 * Physical composition geometry shared by the desktop and browser studios.
 * Source crops are never changed here. Geometry is mm; font sizes remain pt.
 * Planning functions are pure. Mutators validate a complete proposal before
 * assigning anything to the caller's project, including settings and labels.
 */
const POINT_MM = 25.4 / 72;
const EPSILON = 1e-7;
const MIN_EXTENT_MM = .01;
const MAX_ITEMS = 1000;
const DEFAULT_LAYOUT = Object.freeze({preset:'grid',columns:0,rows:0,margin_mm:6,gap_x_mm:3,gap_y_mm:3});
const DEFAULT_NUMBERING = Object.freeze({style:'upper',order:'visual',enabled:true});
const LABEL_MODES = new Set(['auto','manual','none']);
const roundMm = value => Math.round(value * 1e6) / 1e6;
const normalLabel = value => String(value ?? '').trim().toLocaleLowerCase('en');

function finite(value, name, {minimum=0,maximum=2000,integer=false,positive=false}={}) {
  if(typeof value!=='number'||!Number.isFinite(value)||(integer&&!Number.isInteger(value))||value<minimum||value>maximum||(positive&&value<=0)) {
    throw new Error(`${name}须为${integer?'整数':'有限数值'}，范围${positive?'大于 0，且不超过':`${minimum}–`}${maximum}`);
  }
  return value;
}

function figureDimensions(project) {
  return {
    width_mm:finite(project?.figure?.width_mm,'画布宽度',{positive:true}),
    height_mm:finite(project?.figure?.height_mm,'画布高度',{positive:true}),
  };
}

function layoutSettings(project, overrides={}) {
  const options={...DEFAULT_LAYOUT,...project?.settings?.composition?.layout,...overrides};
  if(!['grid','hero-left','hero-top'].includes(options.preset))throw new Error('请选择规则网格、左侧大图或顶部大图版式');
  for(const key of ['columns','rows'])finite(options[key],key==='columns'?'列数':'行数',{integer:true,maximum:MAX_ITEMS});
  for(const key of ['margin_mm','gap_x_mm','gap_y_mm'])finite(options[key],{margin_mm:'边距',gap_x_mm:'水平间距',gap_y_mm:'垂直间距'}[key]);
  return Object.fromEntries(Object.keys(DEFAULT_LAYOUT).map(key=>[key,options[key]]));
}

function numberingSettings(project, overrides={}) {
  const options={...DEFAULT_NUMBERING,...project?.settings?.composition?.numbering,...overrides};
  if(!['upper','lower','numeric'].includes(options.style))throw new Error('编号样式须为大写字母、小写字母或数字');
  if(options.order==='array')options.order='source';
  if(!['visual','source'].includes(options.order))throw new Error('编号顺序须为视觉阅读顺序或素材顺序');
  if(typeof options.enabled!=='boolean')throw new Error('自动编号状态须为布尔值');
  return {style:options.style,order:options.order,enabled:options.enabled};
}

function checkedPanels(project) {
  const panels=project?.panels || [];
  if(!Array.isArray(panels)||panels.length>MAX_ITEMS)throw new Error(`组图最多支持 ${MAX_ITEMS} 个面板`);
  const ids=new Set();
  for(const panel of panels) {
    if(!panel||typeof panel.id!=='string'||!panel.id||ids.has(panel.id))throw new Error('面板 ID 缺失或重复，请先修复项目');
    if(panel.label_mode!=null&&!LABEL_MODES.has(panel.label_mode))throw new Error('面板编号模式无效');
    ids.add(panel.id);
  }
  return panels;
}

function inferredMode(panel) {
  // Old files do not record whether a label was typed by the user. Preserve it.
  return panel.label_mode || (String(panel.label ?? '').trim()?'manual':'auto');
}

/** Zero-based spreadsheet-style letters: 0=A, 25=Z, 26=AA. */
export function alphabeticLabel(index, lower=false) {
  if(!Number.isSafeInteger(index)||index<0)throw new Error('编号序号须为非负安全整数');
  let value=index+1,label='';
  while(value>0){value--;label=String.fromCharCode(65+value%26)+label;value=Math.floor(value/26);}
  return lower?label.toLowerCase():label;
}

export function layoutOptions(project) {return layoutSettings(project);}

export function ensureCompositionSettings(project) {
  const layout=layoutSettings(project),numbering=numberingSettings(project),panels=checkedPanels(project);
  const composition={...project.settings?.composition,layout:{...project.settings?.composition?.layout,...layout},numbering:{...project.settings?.composition?.numbering,...numbering}};
  const modes=panels.map(inferredMode);
  project.settings ||= {};project.settings.composition=composition;
  panels.forEach((panel,index)=>{panel.label_mode=modes[index];if(panel.label_mode==='none')panel.label='';});
  return composition;
}

/** Reading rows use vertical overlap, so different-height panels can share a row. */
export function readingOrderPanels(panels) {
  const records=panels.map((panel,index)=>({panel,index,x:Number.isFinite(panel.x_mm)?panel.x_mm:0,y:Number.isFinite(panel.y_mm)?panel.y_mm:0,height:Number.isFinite(panel.height_mm)?Math.max(0,panel.height_mm):0}));
  records.sort((a,b)=>a.y-b.y||a.x-b.x||a.index-b.index);
  const rows=[];
  for(const record of records) {
    let row=rows.at(-1);
    const overlap=row?Math.min(row.bottom,record.y+record.height)-Math.max(row.top,record.y):-Infinity;
    const tolerance=row?Math.min(3,Math.max(.5,Math.min(row.height,record.height)*.12)):0;
    if(!row||(Math.abs(record.y-row.top)>tolerance&&overlap<Math.min(row.height,record.height)*.5-EPSILON)) {
      row={top:record.y,bottom:record.y+record.height,height:record.height,items:[]};rows.push(row);
    }
    row.items.push(record);
    // Use the common row intersection. A tall hero must not absorb later rows.
    row.top=Math.max(row.top,record.y);row.bottom=Math.min(row.bottom,record.y+record.height);row.height=Math.max(0,row.bottom-row.top);
  }
  return rows.flatMap(row=>row.items.sort((a,b)=>a.x-b.x||a.y-b.y||a.index-b.index).map(record=>record.panel));
}

export function reconcilePanelLabels(project, overrides={}) {
  const panels=checkedPanels(project),numbering=numberingSettings(project,overrides);
  const proposed=panels.map(panel=>({...panel,label:String(panel.label ?? ''),label_mode:overrides.force&&inferredMode(panel)!=='none'?'auto':inferredMode(panel)}));
  const order=numbering.order==='visual'?readingOrderPanels(proposed):proposed;
  const occupied=new Set(),manual=new Map();
  for(const panel of proposed)if(panel.label_mode==='manual'&&normalLabel(panel.label)) {
    const key=normalLabel(panel.label);occupied.add(key);
    if(!manual.has(key))manual.set(key,[]);manual.get(key).push(panel);
  }
  let index=0;
  for(const panel of order) {
    if(panel.label_mode==='none'||(panel.label_mode==='auto'&&!numbering.enabled)){panel.label='';continue;}
    if(panel.label_mode!=='auto')continue;
    let label;
    do {label=numbering.style==='numeric'?String(index+1):alphabeticLabel(index,numbering.style==='lower');index++;}while(occupied.has(normalLabel(label)));
    panel.label=label;occupied.add(normalLabel(label));
  }
  let changed=0;
  proposed.forEach((proposal,index)=>{const panel=panels[index];if(panel.label!==proposal.label||panel.label_mode!==proposal.label_mode)changed++;});
  // All validation and allocation above are complete before the first mutation.
  project.settings ||= {};project.settings.composition ||= {};
  project.settings.composition.numbering={...project.settings.composition.numbering,...numbering};
  proposed.forEach((proposal,index)=>{panels[index].label=proposal.label;panels[index].label_mode=proposal.label_mode;});
  return {changed,duplicates:[...manual.values()].filter(items=>items.length>1).map(items=>({label:items[0].label,panel_ids:items.map(panel=>panel.id)})),order:order.map(panel=>panel.id)};
}

function draftProject(project, figure=project.figure) {
  const draft={...project,figure:{...figure},panels:checkedPanels(project).map(panel=>({...panel})),annotations:(project.annotations || []).map(annotation=>({...annotation})),settings:{...project.settings,composition:{...project.settings?.composition,layout:{...project.settings?.composition?.layout,...layoutSettings(project)},numbering:{...project.settings?.composition?.numbering,...numberingSettings(project)}}}};
  reconcilePanelLabels(draft);
  return draft;
}

function fontSize(value, fallback) {return finite(value ?? fallback,'字号',{positive:true,maximum:1000})*POINT_MM;}

/** Match native export's splitlines(): CRLF is one break, one final break is not an extra line. */
export function textLines(value) {
  // eslint-disable-next-line no-control-regex -- These are Python splitlines() line-break characters.
  const lines=String(value ?? '').split(/\r\n|[\n\r\v\f\u001c-\u001e\u0085\u2028\u2029]/u);
  if(lines.length>1&&lines.at(-1)==='')lines.pop();
  return lines;
}

function textFootprint(text, fontMm) {
  // Conservative physical ink bounds; every renderer uses a 1.25 em line step.
  const lines=textLines(text),width=lines.reduce((longest,line)=>Math.max(longest,Array.from(line).reduce((sum,char)=>sum+(/\s/u.test(char)? .6 : 1.05),0)),0)*fontMm;
  return {width,top:-fontMm*1.15,bottom:fontMm*(.3+(lines.length-1)*1.25)};
}

/** Bounds are affine functions a*scale+b, with physical font extents in b. */
function envelope(panel, baseWidth, baseHeight, labelOverride) {
  const angle=finite(panel.rotation ?? 0,'旋转角度',{minimum:-36000,maximum:36000})*Math.PI/180;
  const rotatedWidth=Math.abs(baseWidth*Math.cos(angle))+Math.abs(baseHeight*Math.sin(angle));
  const rotatedHeight=Math.abs(baseWidth*Math.sin(angle))+Math.abs(baseHeight*Math.cos(angle));
  const left=[[-rotatedWidth/2,0]],right=[[rotatedWidth/2,0]],top=[[-rotatedHeight/2,0]],bottom=[[rotatedHeight/2,0]];
  const label=inferredMode(panel)==='none'?'':labelOverride ?? panel.label ?? (panel.label_mode==='auto'?'A':'');
  if(label) {
    const text=textFootprint(label,fontSize(panel.label_font_size_pt,10));
    left.push([-baseWidth/2,0]);right.push([-baseWidth/2,text.width]);
    top.push([-baseHeight/2,-1.8+text.top]);bottom.push([-baseHeight/2,-1.8+text.bottom]);
  }
  if(panel.title) {
    const text=textFootprint(panel.title,fontSize(panel.title_font_size_pt,8));
    left.push([0,-text.width/2]);right.push([0,text.width/2]);top.push([-baseHeight/2,-1.8+text.top]);bottom.push([-baseHeight/2,-1.8+text.bottom]);
  }
  const gel=panel.gel || {},laneNames=(gel.lane_names || []).filter(Boolean);
  if(laneNames.length) {
    const size=fontSize(gel.font_size_pt,7),texts=laneNames.map(name=>textFootprint(name,size)),half=Math.max(...texts.map(text=>text.width))/2;
    left.push([-baseWidth/2,-half]);right.push([baseWidth/2,half]);top.push([baseHeight/2,3-size*1.15]);bottom.push([baseHeight/2,3+Math.max(...texts.map(text=>text.bottom))]);
  }
  if(gel.marker_points?.length) {
    const size=7*POINT_MM,texts=gel.marker_points.map(point=>textFootprint(point.label ?? point.size ?? point.value ?? '',size)),unit=textFootprint(gel.marker_unit || 'kDa',size),width=Math.max(...texts.map(text=>text.width),unit.width);
    right.push([baseWidth/2,1.8+width]);top.push([-baseHeight/2,-1.8+unit.top]);bottom.push([-baseHeight/2,-1.8+unit.bottom],[baseHeight/2,.8+Math.max(...texts.map(text=>text.bottom))]);
  }
  return {left,right,top,bottom};
}

function rectangle(left, top, right, bottom) {
  return {x_mm:left,y_mm:top,width_mm:Math.max(0,right-left),height_mm:Math.max(0,bottom-top),left,top,right,bottom};
}
function evaluatedBounds(shape, scale=1) {
  return rectangle(Math.min(...shape.left.map(([a,b])=>a*scale+b)),Math.min(...shape.top.map(([a,b])=>a*scale+b)),Math.max(...shape.right.map(([a,b])=>a*scale+b)),Math.max(...shape.bottom.map(([a,b])=>a*scale+b)));
}
function shiftedBounds(bounds,x,y) {return rectangle(bounds.left+x,bounds.top+y,bounds.right+x,bounds.bottom+y);}

export function panelBounds(panel) {
  const width=finite(panel.width_mm,'面板宽度',{positive:true,maximum:4000}),height=finite(panel.height_mm,'面板高度',{positive:true,maximum:4000});
  const x=finite(panel.x_mm,'面板 X',{minimum:-2000,maximum:4000}),y=finite(panel.y_mm,'面板 Y',{minimum:-2000,maximum:4000});
  return shiftedBounds(evaluatedBounds(envelope(panel,width,height)),x+width/2,y+height/2);
}

export function annotationBounds(annotation) {
  const x=finite(annotation.x_mm,'标注 X',{minimum:-10000,maximum:10000}),y=finite(annotation.y_mm,'标注 Y',{minimum:-10000,maximum:10000});
  const width=finite(annotation.width_mm ?? 0,'标注宽度',{minimum:-10000,maximum:10000}),height=finite(annotation.height_mm ?? 0,'标注高度',{minimum:-10000,maximum:10000});
  if(annotation.type==='text') {
    const size=fontSize(annotation.font_size_pt,8),text=textFootprint(annotation.text,size);
    const offset=annotation.anchor==='middle'?text.width/2:annotation.anchor==='end'?text.width:0;
    return rectangle(x-offset,y+Math.min(0,size+text.top),x+Math.max(0,width,text.width-offset),y+Math.max(0,height,size+text.bottom));
  }
  return rectangle(Math.min(x,x+width),Math.min(y,y+height),Math.max(x,x+width),Math.max(y,y+height));
}

function unionBounds(bounds) {
  if(!bounds.length)return rectangle(0,0,0,0);
  return rectangle(Math.min(...bounds.map(item=>item.left)),Math.min(...bounds.map(item=>item.top)),Math.max(...bounds.map(item=>item.right)),Math.max(...bounds.map(item=>item.bottom)));
}

export function compositionBounds(project,{includeCanvas=false}={}) {
  const bounds=checkedPanels(project).map(panelBounds);
  if(!Array.isArray(project.annotations || [])||(project.annotations || []).length>5000)throw new Error('标注数量或数据格式无效');
  bounds.push(...(project.annotations || []).map(annotationBounds));
  if(includeCanvas){const figure=figureDimensions(project);bounds.push(rectangle(0,0,figure.width_mm,figure.height_mm));}
  return unionBounds(bounds);
}

function sourceRatio(panel,assets) {
  const asset=assets.get(panel.asset_id),crop=panel.crop;
  let width=crop?.width ?? asset?.width_px ?? panel.width_mm,height=crop?.height ?? asset?.height_px ?? panel.height_mm;
  if(panel.channel_view?.mode==='split') {
    const x=crop?.x ?? 0,y=crop?.y ?? 0;
    const layout=channelLayout(panel.channel_view,Math.max(asset?.width_px || 0,Math.ceil(x+width)),Math.max(asset?.height_px || 0,Math.ceil(y+height)),crop || {x:0,y:0,width,height});
    width=layout.width_px;height=layout.height_px;
  }
  if(!Number.isFinite(width)||!Number.isFinite(height)||width<=0||height<=0)throw new Error(`面板 ${panel.label || panel.id} 的原图宽高无效`);
  return width/height;
}

function fitInCell(descriptor,cell) {
  if(cell.width_mm<MIN_EXTENT_MM||cell.height_mm<MIN_EXTENT_MM)return null;
  const shape=descriptor.envelope,zero=evaluatedBounds(shape,0);
  if(zero.width_mm>cell.width_mm+EPSILON||zero.height_mm>cell.height_mm+EPSILON)return null;
  let scale=Infinity;
  for(const [high,low,extent] of [[shape.right,shape.left,cell.width_mm],[shape.bottom,shape.top,cell.height_mm]]) {
    for(const [a,b] of high)for(const [c,d] of low) {
      if(a-c>EPSILON)scale=Math.min(scale,(extent-(b-d))/(a-c));
    }
  }
  const width=descriptor.ratio*scale,height=scale;
  if(!Number.isFinite(scale)||width<MIN_EXTENT_MM||height<MIN_EXTENT_MM)return null;
  const bounds=evaluatedBounds(shape,scale);
  const centerX=cell.x_mm+(cell.width_mm-bounds.width_mm)/2-bounds.left,centerY=cell.y_mm+(cell.height_mm-bounds.height_mm)/2-bounds.top;
  return {id:descriptor.panel.id,x_mm:roundMm(centerX-width/2),y_mm:roundMm(centerY-height/2),width_mm:roundMm(width),height_mm:roundMm(height)};
}

function gridCandidate(descriptors,region,columns,rows,options) {
  if(columns*rows<descriptors.length)return null;
  const width=(region.width_mm-(columns-1)*options.gap_x_mm)/columns,height=(region.height_mm-(rows-1)*options.gap_y_mm)/rows;
  if(width<MIN_EXTENT_MM||height<MIN_EXTENT_MM)return null;
  const placements=[],cells=[];let score=0;
  for(let index=0;index<descriptors.length;index++) {
    const cell={x_mm:region.x_mm+(index%columns)*(width+options.gap_x_mm),y_mm:region.y_mm+Math.floor(index/columns)*(height+options.gap_y_mm),width_mm:width,height_mm:height};
    const placement=fitInCell(descriptors[index],cell);if(!placement)return null;
    score+=placement.width_mm*placement.height_mm;
    placements.push(placement);cells.push({...cell,panel_id:placement.id,column:index%columns,row:Math.floor(index/columns),column_span:1,row_span:1});
  }
  return {placements,cells,columns,rows,score,unused:columns*rows-descriptors.length};
}

function bestGrid(descriptors,region,options) {
  const count=descriptors.length;if(!count)return {placements:[],cells:[],columns:0,rows:0,score:0};
  if(options.columns||options.rows) {
    const columns=options.columns || Math.ceil(count/options.rows),rows=options.rows || Math.ceil(count/options.columns);
    if(columns*rows<count)throw new Error(`${columns} 列 × ${rows} 行只能容纳 ${columns*rows} 个面板；本次操作需要容纳 ${count} 个面板`);
    return gridCandidate(descriptors,region,columns,rows,options);
  }
  let best=null;
  for(let columns=1;columns<=count;columns++) {
    const rows=Math.ceil(count/columns),candidate=gridCandidate(descriptors,region,columns,rows,options);
    if(candidate&&(!best||candidate.score>best.score+EPSILON||(Math.abs(candidate.score-best.score)<=EPSILON&&(candidate.unused<best.unused||(candidate.unused===best.unused&&candidate.columns<best.columns)))))best=candidate;
  }
  return best;
}

export function planPanelLayout(project,overrides={}) {
  const options=layoutSettings(project,overrides),figure=figureDimensions(project),draft=draftProject(project);
  const region={x_mm:options.margin_mm,y_mm:options.margin_mm,width_mm:figure.width_mm-2*options.margin_mm,height_mm:figure.height_mm-2*options.margin_mm};
  if(region.width_mm<=0||region.height_mm<=0)throw new Error('边距占满了画布，请减小边距或增大画布');
  if(!draft.panels.length)return {options,placements:[],cells:[],columns:0,rows:0,preset:options.preset,warnings:[]};
  const assets=new Map((project.assets || []).map(asset=>[asset.id,asset]));
  const autoLength=Math.max(1,...draft.panels.filter(panel=>panel.label_mode==='auto').map(panel=>Array.from(panel.label).length));
  const descriptors=draft.panels.map(panel=>{
    const ratio=sourceRatio(panel,assets),label=panel.label_mode==='auto'&&draft.settings.composition.numbering.enabled?'A'.repeat(autoLength):panel.label;
    return {panel,ratio,envelope:envelope(panel,ratio,1,label)};
  });
  let result,warnings=[];
  if(options.preset==='grid'||descriptors.length===1)result=bestGrid(descriptors,region,options);
  else {
    const horizontal=options.preset==='hero-left',gap=horizontal?options.gap_x_mm:options.gap_y_mm;
    const usable=(horizontal?region.width_mm:region.height_mm)-gap,heroExtent=usable*.58;
    const hero={...region},rest={...region};
    if(horizontal){hero.width_mm=heroExtent;rest.x_mm+=heroExtent+gap;rest.width_mm=usable-heroExtent;}
    else {hero.height_mm=heroExtent;rest.y_mm+=heroExtent+gap;rest.height_mm=usable-heroExtent;}
    const main=fitInCell(descriptors[0],hero),others=bestGrid(descriptors.slice(1),rest,{...options,columns:0,rows:0});
    if(main&&others) {
      result={placements:[main,...others.placements],cells:[{...hero,panel_id:main.id,column:0,row:0,column_span:horizontal?1:others.columns,row_span:horizontal?others.rows:1},...others.cells.map(cell=>({...cell,column:cell.column+(horizontal?1:0),row:cell.row+(horizontal?0:1)}))],columns:others.columns+(horizontal?1:0),rows:others.rows+(horizontal?0:1)};
      if(options.columns||options.rows)warnings.push('大图组合模板自动分配行列；指定行列数仅用于规则网格。');
    }
  }
  if(!result)throw new Error('当前画布、边距、间距和字号不足以容纳全部面板；请增大画布、减小间距/字号或调整行列');
  const plan={options,placements:result.placements,cells:result.cells,columns:result.columns,rows:result.rows,preset:options.preset,warnings};
  // The final labels can be assigned in their final reading order without
  // escaping the conservative auto-label footprint used while fitting.
  const byId=new Map(plan.placements.map(item=>[item.id,item]));
  for(const panel of draft.panels)Object.assign(panel,byId.get(panel.id));
  reconcilePanelLabels(draft);
  for(const panel of draft.panels)if(!within(panelBounds(panel),figure))throw new Error('排版后的图像或编号超出画布；请调整画布与边距');
  return plan;
}

function within(bounds,figure) {return bounds.left>=-1e-5&&bounds.top>=-1e-5&&bounds.right<=figure.width_mm+1e-5&&bounds.bottom<=figure.height_mm+1e-5;}

export function applyPanelLayout(project,overrides={}) {
  const plan=planPanelLayout(project,overrides),draft=draftProject(project),byId=new Map(plan.placements.map(item=>[item.id,item]));
  for(const panel of draft.panels)Object.assign(panel,byId.get(panel.id));
  draft.settings.composition.layout={...draft.settings.composition.layout,...plan.options};reconcilePanelLabels(draft);
  project.settings ||= {};project.settings.composition={...draft.settings.composition};
  draft.panels.forEach((panel,index)=>Object.assign(project.panels[index],byId.get(panel.id),{label:panel.label,label_mode:panel.label_mode}));
  return plan;
}

function scaledGeometry(item,scale,dx=0,dy=0) {
  return {...item,x_mm:item.x_mm*scale+dx,y_mm:item.y_mm*scale+dy,width_mm:(item.width_mm ?? 0)*scale,height_mm:(item.height_mm ?? 0)*scale};
}

function scaledBounds(project,scale) {
  const bounds=[rectangle(0,0,project.figure.width_mm*scale,project.figure.height_mm*scale)];
  for(const panel of project.panels) {
    const width=panel.width_mm,height=panel.height_mm;
    bounds.push(shiftedBounds(evaluatedBounds(envelope(panel,width,height),scale),(panel.x_mm+width/2)*scale,(panel.y_mm+height/2)*scale));
  }
  for(const annotation of project.annotations)bounds.push(annotationBounds(scaledGeometry(annotation,scale)));
  return unionBounds(bounds);
}

function fitResizeDraft(draft,width,height) {
  const previous=figureDimensions(draft);compositionBounds(draft);
  const zero=scaledBounds(draft,0);
  if(zero.width_mm>width+EPSILON||zero.height_mm>height+EPSILON)throw new Error('保持当前字号后，文字宽度或高度超过新画布；请先缩小字号或缩短文字');
  let upper=Math.min(width/previous.width_mm,height/previous.height_mm),lower=0;
  const fits=scale=>{const box=scaledBounds(draft,scale);return box.width_mm<=width+EPSILON&&box.height_mm<=height+EPSILON;};
  if(!fits(upper)){for(let step=0;step<48;step++){const middle=(lower+upper)/2;if(fits(middle))lower=middle;else upper=middle;}upper=lower;}
  if(!Number.isFinite(upper)||upper<=0)throw new Error('新画布太小，无法保持内容与字号');
  const bounds=scaledBounds(draft,upper),dx=(width-bounds.width_mm)/2-bounds.left,dy=(height-bounds.height_mm)/2-bounds.top;
  const panels=draft.panels.map(panel=>scaledGeometry(panel,upper,dx,dy));
  if(panels.some(panel=>panel.width_mm<MIN_EXTENT_MM||panel.height_mm<MIN_EXTENT_MM))throw new Error('新画布会使面板小于 0.01 mm，请使用更大的画布');
  return {panels,annotations:draft.annotations.map(annotation=>scaledGeometry(annotation,upper,dx,dy)),scale:upper};
}

export function resizeFigure(project,widthMm,heightMm,{mode='fit'}={}) {
  const width=finite(widthMm,'画布宽度',{positive:true}),height=finite(heightMm,'画布高度',{positive:true});
  if(!['fit','reflow','canvas'].includes(mode))throw new Error('请选择整体适配、重新排版或仅修改画布');
  const draft=draftProject(project),warnings=[];let scale=1;
  const figure={...draft.figure,width_mm:width,height_mm:height};
  if(mode==='canvas') {
    if(!within(compositionBounds(draft),figure))throw new Error('仅修改画布会裁掉现有图像或标注，请选择整体适配或重新排版');
  } else {
    const fit=fitResizeDraft(draft,width,height);scale=fit.scale;
    draft.annotations=fit.annotations;
    if(mode==='fit')draft.panels=fit.panels;
    else {
      draft.figure=figure;
      const plan=planPanelLayout(draft),byId=new Map(plan.placements.map(item=>[item.id,item]));
      for(const panel of draft.panels)Object.assign(panel,byId.get(panel.id));
      if(draft.annotations.length)warnings.push('面板已重新排版，独立标注已整体适配；请核对标注与面板的对应关系。');
    }
  }
  draft.figure=figure;reconcilePanelLabels(draft);
  if(!within(compositionBounds(draft),figure))throw new Error('适配后的图像或标注超出画布，请检查字号与位置');
  project.figure={...figure};
  draft.panels.forEach((panel,index)=>Object.assign(project.panels[index],{x_mm:roundMm(panel.x_mm),y_mm:roundMm(panel.y_mm),width_mm:roundMm(panel.width_mm),height_mm:roundMm(panel.height_mm),label:panel.label,label_mode:panel.label_mode}));
  (project.annotations || []).forEach((annotation,index)=>{const next=draft.annotations[index];Object.assign(annotation,{x_mm:roundMm(next.x_mm),y_mm:roundMm(next.y_mm),width_mm:roundMm(next.width_mm),height_mm:roundMm(next.height_mm)});});
  project.settings ||= {};project.settings.composition={...draft.settings.composition};
  return {mode,width_mm:width,height_mm:height,scale,warnings};
}
