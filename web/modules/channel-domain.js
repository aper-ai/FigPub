/** Display recipes contain raw source identities; they never contain image pixels. */
export const channelPalette=['#2878ff','#25d77d','#ff4c65','#d65cff','#20d5df','#ffc84a','#ffffff'];
export function sourceIdentityKey(source) {
  return `${source.asset_id}:${source.series || 0}:${source.frame || 0}:${source.channel || 0}`;
}
export function descriptorKey(source) {
  return `${source.asset_id}:${source.series || 0}:${source.frame || 0}`;
}
export function channelViewSignature(view) {return JSON.stringify(view || null);}
export function singleChannelView(view,id) {
  return {...structuredClone(view),mode:'single',active_channel:id};
}
export function mergeChannelView(view) {return {...structuredClone(view),mode:'merge'};}
export function compositionInitialSource(view,assetId) {
  return structuredClone(view?.channels?.find(channel=>channel.id===view.active_channel)?.source || view?.channels?.[0]?.source || {asset_id:assetId,series:0,frame:0,channel:0});
}
/** Keep a consistent display across frames; a different series starts from its own metadata. */
export function viewAtSourcePlane(view,descriptor) {
  const next=structuredClone(descriptor.default_view),oldSource=view?.channels?.[0]?.source,newSource=next.channels[0]?.source;
  const compatible=oldSource&&newSource&&oldSource.asset_id===newSource.asset_id&&oldSource.series===newSource.series&&view.channels.every(channel=>channel.source.asset_id===oldSource.asset_id&&channel.source.series===oldSource.series&&channel.source.frame===oldSource.frame);
  if(!compatible)return next;
  const activeIndex=view.channels.find(channel=>channel.id===view.active_channel)?.source.channel;
  for(const channel of next.channels){const previous=view.channels.find(item=>item.source.channel===channel.source.channel);if(previous)for(const field of ['name','color','black','white','gamma','visible'])channel[field]=previous[field];}
  next.mode=view.mode;next.split=structuredClone(view.split);next.active_channel=next.channels.find(channel=>channel.source.channel===activeIndex)?.id || next.channels[0].id;
  return next;
}
export function channelPaneViews(view) {
  if(!view?.channels?.length)return [];
  if(view.mode==='single') {
    const channel=view.channels.find(item=>item.id===view.active_channel) || view.channels[0];
    return [{id:channel.id,name:channel.name,channel,view:singleChannelView(view,channel.id)}];
  }
  if(view.mode!=='split')return [{id:'merge',name:'合并',channel:null,view:mergeChannelView(view)}];
  const panes=view.channels.filter(channel=>channel.visible!==false).map(channel=>({id:channel.id,name:channel.name,channel,view:singleChannelView(view,channel.id)}));
  if(view.split?.include_merge&&panes.length)panes.push({id:'merge',name:'合并',channel:null,view:mergeChannelView(view)});
  return panes;
}
export function channelLayout(view,sourceWidth,sourceHeight,crop=null) {
  // Preview/export include every pixel touched by a fractional source rectangle.
  const left=Math.floor(crop?.x || 0),top=Math.floor(crop?.y || 0),region=crop?{x:left,y:top,width:Math.min(sourceWidth,Math.ceil(crop.x+crop.width))-left,height:Math.min(sourceHeight,Math.ceil(crop.y+crop.height))-top}:{x:0,y:0,width:sourceWidth,height:sourceHeight},panes=channelPaneViews(view),count=panes.length;
  const columns=view.mode==='split'?Math.max(1,Math.min(count || 1,Number(view.split?.columns)||Math.ceil(Math.sqrt(count || 1)))):1;
  const rows=Math.max(1,Math.ceil(count/columns)),gap=view.mode==='split'?Number(view.split?.gap_px ?? 8):0;
  return {width_px:columns*region.width+(columns-1)*gap,height_px:rows*region.height+(rows-1)*gap,source_width_px:sourceWidth,source_height_px:sourceHeight,crop:region,columns,rows,gap_px:gap,
    tiles:panes.map((pane,index)=>({id:pane.id,channel_id:pane.channel?.id || null,channel_ids:pane.channel?[pane.channel.id]:view.channels.filter(item=>item.visible!==false).map(item=>item.id),mode:pane.channel?'single':'merge',name:pane.name,x_px:index%columns*(region.width+gap),y_px:Math.floor(index/columns)*(region.height+gap),width_px:region.width,height_px:region.height,source:pane.channel?.source,view:pane.view}))};
}
/** Source-gallery geometry uses a common px/px scale, so the gap control is visible. */
export function previewGridGeometry({count,width_px,height_px,columns,gap_px,available_width_px,available_height_px}) {
  const columnCount=Math.max(1,Math.min(count,columns || Math.ceil(Math.sqrt(count)))),rows=Math.ceil(count/columnCount),captionHeight=28;
  const nativeWidth=columnCount*width_px+(columnCount-1)*gap_px,nativeHeight=rows*height_px+(rows-1)*gap_px;
  const scale=Math.max(145/width_px,Math.min(Math.max(1,available_width_px)/nativeWidth,Math.max(1,available_height_px-rows*captionHeight)/nativeHeight));
  return {columns:columnCount,rows,scale,tile_width_px:width_px*scale,tile_height_px:height_px*scale,gap_px:gap_px*scale,caption_height_px:captionHeight};
}
export function channelFieldPatch(view,id,field,value) {
  const result=structuredClone(view),channel=result.channels.find(item=>item.id===id);
  if(!channel)throw new Error('选中的通道已不存在');
  if(field==='name') {value=String(value).trim();if(!value||value.length>120)throw new Error('通道名称须为 1–120 个字符');}
  else if(field==='color') {if(!/^#[0-9a-f]{6}$/i.test(value))throw new Error('请选择有效的通道颜色');}
  else if(field==='visible')value=Boolean(value);
  else if(['black','white'].includes(field)) {value=value===''||value==null?null:Number(value);if(value!==null&&!Number.isFinite(value))throw new Error('黑白点必须是有限的原始强度值');}
  else if(field==='gamma') {value=Number(value);if(!Number.isFinite(value)||value<.1||value>5)throw new Error('Gamma 必须为 0.1–5 之间的有限数值');}
  else throw new Error('未知通道字段');
  channel[field]=value;
  if(channel.black!=null&&channel.white!=null&&channel.black>=channel.white)throw new Error('白点必须大于黑点');
  return result;
}
export function compositionView(sources,descriptors) {
  if(sources.length<2)throw new Error('请选择至少两个原始通道');
  const identities=new Set();let width=null,height=null;
  const channels=sources.map((entry,index)=>{
    const source=entry.source || entry,key=sourceIdentityKey(source),descriptor=descriptors.get(descriptorKey(source));
    if(identities.has(key))throw new Error(`来源 ${index+1} 与前面的原始平面重复（S${source.series || 0} / F${source.frame || 0} / C${source.channel || 0}），请选择另一个通道`);identities.add(key);
    if(!descriptor)throw new Error('请等待读取通道信息');
    if(width===null){width=descriptor.width_px;height=descriptor.height_px;}
    if(width!==descriptor.width_px||height!==descriptor.height_px)throw new Error(`来源 ${index+1} 为 ${descriptor.width_px} × ${descriptor.height_px} px，来源 1 为 ${width} × ${height} px；组合要求像素宽高完全相同`);
    const plane=descriptor.channels.find(item=>item.source.channel===source.channel);
    if(!plane)throw new Error('所选原始通道不可用');
    const original=descriptor.default_view.channels.find(item=>sourceIdentityKey(item.source)===key);
    return {id:`c${index}`,name:String(entry.name || plane.name || `通道 ${index+1}`),color:entry.color || channelPalette[index%channelPalette.length],visible:true,black:original?.black ?? null,white:original?.white ?? null,gamma:1,source:{asset_id:source.asset_id,series:source.series || 0,frame:source.frame || 0,channel:source.channel || 0}};
  });
  return {view:{version:1,mode:'merge',active_channel:channels[0].id,split:{columns:0,gap_px:8,include_merge:false},channels},width_px:width,height_px:height};
}
export function analysisContextKey(assetId,view=null) {
  const sources=view?.channels?.map(channel=>sourceIdentityKey(channel.source)).sort();
  return sources?.length?sources.join('|'):`${assetId}:0:0`;
}
export function analysisChannelChoices(asset,view=null,descriptor=null) {
  if(view?.channels?.length)return view.channels.map(channel=>({id:channel.id,name:channel.name,source:structuredClone(channel.source)}));
  if(descriptor?.channels?.length)return descriptor.channels.filter(channel=>channel.role!=='alpha').map(channel=>({id:channel.id,name:channel.name,source:structuredClone(channel.source)}));
  return Array.from({length:Math.max(1,asset?.channels || 1)},(_,channel)=>({id:`c${channel}`,name:`通道 ${channel+1}`,source:{asset_id:asset?.id,series:0,frame:0,channel}}));
}
