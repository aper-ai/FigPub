import {request} from './api.js';
import {state,emit,notify,mutate,selectedAsset,selectedEdit,editableSource,withBusy} from './state.js';
import {clone,defaultEdit,panelFromAsset,round} from './domain.js';
import {descriptorKey,sourceIdentityKey,channelFieldPatch,channelPaneViews,channelViewSignature,compositionView,compositionInitialSource,viewAtSourcePlane} from './channel-domain.js';
import {ChannelPreviewCache,fetchChannelPreview} from './channel-preview.js';

const descriptorRequests=new Map();
export const channelPreviews=new ChannelPreviewCache({onChange:emit,fetcher:fetchChannelPreview});

export function descriptorEntry(source) {
  const key=descriptorKey(source);
  return {data:state.channelDescriptors.get(key),loading:descriptorRequests.has(key),error:state.channelDescriptorErrors.get(key)};
}
export async function loadChannelDescriptor(source,{retry=false}={}) {
  const key=descriptorKey(source);
  if(!retry&&state.channelDescriptors.has(key))return state.channelDescriptors.get(key);
  if(descriptorRequests.has(key))return descriptorRequests.get(key);
  state.channelDescriptorErrors.delete(key);
  const promise=request(`/api/assets/${encodeURIComponent(source.asset_id)}/channels?series=${source.series || 0}&frame=${source.frame || 0}`).then(data=>{state.channelDescriptors.set(key,data);return data;}).catch(error=>{state.channelDescriptorErrors.set(key,error.message);throw error;}).finally(()=>{descriptorRequests.delete(key);emit();});
  descriptorRequests.set(key,promise);return promise;
}
export function requestVisibleDescriptor(source) {
  const entry=descriptorEntry(source);
  if(!entry.data&&!entry.loading&&!entry.error)loadChannelDescriptor(source).catch(()=>{});
  return descriptorEntry(source);
}
export function currentSourceIdentity() {
  return selectedEdit()?.channel_view?.channels?.[0]?.source || {asset_id:state.selectedAssetId,series:0,frame:0,channel:0};
}
export function currentChannelView() {
  return selectedEdit()?.channel_view || descriptorEntry(currentSourceIdentity()).data?.default_view || null;
}
export function channelSourceDimensions(asset=selectedAsset(),view=selectedEdit()?.channel_view) {
  const source=view?.channels?.[0]?.source;
  const descriptor=source?descriptorEntry(source).data:null;
  return {width_px:descriptor?.width_px || asset?.width_px || 1,height_px:descriptor?.height_px || asset?.height_px || 1};
}
export function previewForChannels(view,crop=null,maxSize=1600) {
  return channelPreviews.get({channel_view:view,crop,max_size:maxSize});
}
export async function enterChannels() {
  const asset=selectedAsset();if(state.tab==='analysis'&&state.analysisKind==='microscopy')state.sourceRoi=null;state.tab='channels';state.selectedAnnotationId=null;emit();
  if(asset?.kind!=='image')return;
  try {await loadChannelDescriptor(currentSourceIdentity());}catch(error){notify(`读取通道失败：${error.message}`,'error');}
}
function storeChannelView(view,label) {
  mutate(project=>{editableSource(project).channel_view=view;},label);
}
export function changeChannelField(id,field,value) {
  const view=currentChannelView();if(!view)return;
  try {storeChannelView(channelFieldPatch(view,id,field,value),'编辑通道显示');}
  catch(error){notify(error.message,'info');emit();}
}
export function changeChannelMode(mode,id=null) {
  const view=currentChannelView();if(!view||!['single','merge','split'].includes(mode))return;
  const next=clone(view);next.mode=mode;if(id)next.active_channel=id;
  storeChannelView(next,'切换通道显示');
}
export function changeSplitSetting(field,value) {
  const view=currentChannelView();if(!view)return;
  const next=clone(view);next.split ||= {columns:0,gap_px:8,include_merge:false};
  next.split[field]=field==='include_merge'?Boolean(value):Number(value);
  if(!Number.isInteger(next.split.columns)||next.split.columns<0||next.split.columns>32||!Number.isInteger(next.split.gap_px)||next.split.gap_px<0||next.split.gap_px>1024){notify('分屏列数应为 0–32 的整数，间隔为 0–1024 个原始像素','info');emit();return;}
  storeChannelView(next,'调整分屏排列');
}
export async function changeSourcePlane(field,value) {
  const asset=selectedAsset();if(asset?.kind!=='image')return;
  const projectId=state.project.id,previousView=clone(currentChannelView()),previousCrop=clone(selectedEdit().crop),identity={...currentSourceIdentity(),[field]:Number(value),channel:0};
  if(field==='series')identity.frame=0;
  await withBusy('正在读取原始图像平面…',async()=>{
    const descriptor=await loadChannelDescriptor(identity);
    if(state.project.id!==projectId||state.selectedAssetId!==asset.id)return;
    mutate(project=>{const edit=editableSource(project);edit.channel_view=viewAtSourcePlane(previousView,descriptor);edit.crop=field==='frame'&&previousCrop.x+previousCrop.width<=descriptor.width_px&&previousCrop.y+previousCrop.height<=descriptor.height_px?previousCrop:{x:0,y:0,width:descriptor.width_px,height:descriptor.height_px};edit.gel={lane_centers:[],lane_names:[],marker_points:[],hidden_lanes:[],marker_unit:'kDa'};},'选择原始图像平面');
    state.sourceRoi=null;state.sourcePointerIdentity=null;state.sourceZoom=1;state.sourcePan={x:0,y:0};emit();
    if(field==='series')notify('已切换系列，通道显示按该系列的原始元数据重新初始化','info');
  },{cancellable:false});
}
export async function resetChannelView() {
  const original=currentChannelView();if(!original)return;
  const signature=channelViewSignature(original),projectId=state.project.id,assetId=state.selectedAssetId;
  await withBusy('正在恢复各通道显示范围…',async()=>{
    await Promise.all(original.channels.map(channel=>loadChannelDescriptor(channel.source)));
    if(state.project.id!==projectId||state.selectedAssetId!==assetId||channelViewSignature(currentChannelView())!==signature)return;
    const next=clone(original);
    for(const channel of next.channels) {
      const descriptor=descriptorEntry(channel.source).data,defaults=descriptor.default_view.channels.find(item=>sourceIdentityKey(item.source)===sourceIdentityKey(channel.source));
      channel.black=defaults?.black ?? null;channel.white=defaults?.white ?? null;channel.gamma=1;channel.visible=defaults?.visible!==false;
    }
    storeChannelView(next,'复位全部通道强度');notify('已恢复各通道默认范围、Gamma 和可见性');
  },{cancellable:false});
}
export async function autoRangeChannels(id=null) {
  const view=currentChannelView();if(!view)return;
  const signature=channelViewSignature(view),projectId=state.project.id,assetId=state.selectedAssetId;
  await withBusy('正在计算原始通道百分位…',async signal=>{
    const payload={channel_view:view,percentiles:[1,99.5]};if(id)payload.channel_ids=[id];
    const output=await request('/api/channel-view/auto-range',{method:'POST',body:payload,signal});
    if(state.project.id!==projectId||state.selectedAssetId!==assetId||channelViewSignature(currentChannelView())!==signature){notify('当前通道已改变，未覆盖新的设置','info');return;}
    storeChannelView(output.channel_view,'按原始通道百分位初始化');
    state.channelRangeReport=output;notify('已应用各通道 1–99.5% 百分位范围；请核对弱信号与饱和区域','info');
  });
}
export function openChannelComposition() {
  const images=state.project.assets.filter(asset=>asset.kind==='image');
  if(images.length<1){notify('请先导入图像素材','info');return;}
  const selected=images.find(asset=>asset.id===state.selectedAssetId) || images[0];
  const first=compositionInitialSource(selectedAsset()?.kind==='image'?currentChannelView():null,selected.id);
  state.modal={type:'channel-composition',entries:[first],error:null};
  loadChannelDescriptor(state.modal.entries[0]).catch(()=>{});emit();
}
export function addCompositionSource() {
  const modal=state.modal;if(modal?.type!=='channel-composition')return;
  const used=new Set(modal.entries.map(item=>item.asset_id)),asset=state.project.assets.find(item=>item.kind==='image'&&!used.has(item.id)) || selectedAsset();
  if(!asset)return;const source={asset_id:asset.id,series:0,frame:0,channel:0};modal.entries.push(source);loadChannelDescriptor(source).catch(()=>{});emit();
}
export function editCompositionSource(index,field,value) {
  const entry=state.modal?.entries?.[index];if(!entry)return;
  entry[field]=['series','frame','channel'].includes(field)?Number(value):value;
  if(field==='asset_id'){entry.series=0;entry.frame=0;entry.channel=0;}
  if(field==='series'){entry.frame=0;entry.channel=0;}
  if(field==='frame')entry.channel=0;
  state.modal.error=null;loadChannelDescriptor(entry).catch(()=>{});emit();
}
export async function confirmChannelComposition() {
  if(state.modal?.type!=='channel-composition')return;
  const entries=clone(state.modal.entries),modal=state.modal;modal.error=null;
  await withBusy('正在核验原件并组合通道…',async signal=>{
    try {
    await Promise.all(entries.map(source=>loadChannelDescriptor(source)));
    const composition=compositionView(entries,state.channelDescriptors);
    const output=await request('/api/channel-view/describe',{method:'POST',body:{channel_view:composition.view,crop:null},signal});
    const asset=state.project.assets.find(item=>item.id===entries[0].asset_id);
    state.selectedAssetId=asset.id;state.selectedPanelId=null;state.sourceRoi=null;state.sourcePan={x:0,y:0};state.sourceZoom=1;
    mutate(project=>{project.settings.asset_edits[asset.id]={...defaultEdit(asset),crop:{x:0,y:0,width:composition.width_px,height:composition.height_px},channel_view:output.channel_view};},'组合原始通道');
    state.modal=null;state.tab='channels';emit();notify(`已组合 ${entries.length} 个原始通道，所有原件引用随项目保存`);
    }catch(error){if(error.name!=='AbortError'&&state.modal===modal){modal.error=error.message || '组合验证未通过，请核对来源';emit();}throw error;}
  });
}
export function addChannelPanels() {
  const asset=selectedAsset(),view=currentChannelView();if(!asset||!view)return;
  const edit=clone(selectedEdit());if(state.sourceRoi&&state.tab!=='analysis')edit.crop=clone(state.sourceRoi);
  const panes=channelPaneViews(view);if(!panes.length){notify('请先显示至少一个通道','info');return;}
  mutate(project=>{
    const columns=Math.max(1,Math.min(panes.length,Math.ceil(Math.sqrt(panes.length)))),rows=Math.ceil(panes.length/columns),margin=8,gap=7;
    const cellWidth=(project.figure.width_mm-margin*2-gap*(columns-1))/columns,cellHeight=(project.figure.height_mm-margin*2-gap*(rows-1))/rows;
    panes.forEach((pane,index)=>{
      const panel=panelFromAsset(project,asset,{...clone(edit),channel_view:pane.view}),ratio=edit.crop.height/edit.crop.width;
      panel.width_mm=round(Math.min(cellWidth,cellHeight/ratio));panel.height_mm=round(panel.width_mm*ratio);
      panel.x_mm=round(margin+index%columns*(cellWidth+gap));panel.y_mm=round(margin+Math.floor(index/columns)*(cellHeight+gap));panel.title=pane.name;
      project.panels.push(panel);state.selectedPanelId=panel.id;
    });
  },'添加可编辑通道面板');
  state.tab='compose';state.sourceRoi=null;emit();notify(`已加入 ${panes.length} 个独立通道面板`);
}
