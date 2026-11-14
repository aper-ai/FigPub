import {state, selectedAsset, selectedEdit, editableSource, mutate, emit, notify,currentRawSource} from './state.js';
import {localizationPixels} from './api.js';
import {clone, uid, clamp} from './domain.js';

/** Localization operates on a bounded preview; numerical analysis never reads it. */
function cropLocalizationImage(image, crop) {
  const sourceWidth = image.source_width || image.width, sourceHeight = image.source_height || image.height;
  const scaleX = image.width / sourceWidth, scaleY = image.height / sourceHeight;
  const left = clamp(Math.floor(crop.x * scaleX), 0, image.width - 1), top = clamp(Math.floor(crop.y * scaleY), 0, image.height - 1);
  const right = clamp(Math.ceil((crop.x + crop.width) * scaleX), left + 1, image.width), bottom = clamp(Math.ceil((crop.y + crop.height) * scaleY), top + 1, image.height);
  if(left===0&&top===0&&right===image.width&&bottom===image.height)return {image,offsetX:0,offsetY:0};
  const width = right - left, height = bottom - top, data = new Float32Array(width * height);
  for (let y = 0; y < height; y++) data.set(image.data.subarray((y + top) * image.width + left, (y + top) * image.width + right), y * width);
  return {image: {width, height, source_width: width / scaleX, source_height: height / scaleY, data}, offsetX: left / scaleX, offsetY: top / scaleY};
}

export async function detectGel() {
  const asset = selectedAsset(); if (!asset || asset.kind === 'table' || state.gelBusy) return;
  const source=currentRawSource(),assetId=source.asset_id,workspaceAssetId=asset.id,sequence=state.mutationSequence,crop=clone(selectedEdit().crop),token=uid('gel-task');
  state.gelBusy = true; state.gelTaskToken=token;state.gelSuggestions = null;state.gelAbort=new AbortController();emit();
  let worker;
  try {
    const pixels = await localizationPixels(assetId,state.gelAbort.signal,source);
    if (!state.gelBusy || state.gelTaskToken!==token) return;
    const localization = cropLocalizationImage(pixels, crop), jobId = uid('localize');
    worker = new Worker('/gel-worker.js'); state.gelWorker = worker;
    const result = await new Promise((resolve, reject) => {
      worker.onmessage = ({data}) => {if(data.id!==jobId)return;if(data.error)reject(new Error(data.error));else resolve(data.result);};
      worker.onerror = error => reject(new Error(error.message || '定位引擎运行失败'));
      state.gelReject = reject;
      worker.postMessage({id:jobId, image:localization.image, lane_count:state.gelLaneCount ? Number(state.gelLaneCount) : undefined, mode:state.gelMode || 'protein'}, [localization.image.data.buffer]);
    });
    if (sequence !== state.mutationSequence || state.selectedAssetId !== workspaceAssetId || state.gelTaskToken!==token) {notify('识别时素材或设置已更改，未覆盖当前编辑', 'info'); return;}
    result.lane_centers = (result.lane_centers || []).map(x => x + localization.offsetX);
    result.bands = (result.bands || []).map(band => ({...band, x: band.x == null ? undefined : band.x + localization.offsetX, y:band.y + localization.offsetY,
      ...(band.bbox?{bbox:{...band.bbox,x:band.bbox.x+localization.offsetX,y:band.bbox.y+localization.offsetY}}:{})}));
    result.marker_candidates = (result.marker_candidates || []).map(candidate => ({...candidate,points:candidate.points.map(point => ({...point,y:point.y + localization.offsetY}))}));
    mutate(project => {
      const edit = editableSource(project), previous = edit.gel || {}, oldCenters = previous.lane_centers || [];
      const names = result.lane_centers.map(x => {
        let nearest = -1, distance = Infinity; oldCenters.forEach((old,index) => {if(Math.abs(old-x) < distance) {distance = Math.abs(old-x); nearest = index;}});
        return nearest >= 0 && distance < crop.width / Math.max(4,result.lane_centers.length * 2) ? previous.lane_names?.[nearest] || '' : '';
      });
      edit.gel = {...previous, lane_centers:result.lane_centers, lane_names:names, bands:result.bands, polarity:result.polarity,inferred_lanes:result.inferred_lanes || [],marker_points:previous.marker_points || [], marker_unit:previous.marker_unit || (state.gelMode === 'dna' ? 'bp' : 'kDa'), hidden_lanes:previous.hidden_lanes || [], localization:{algorithm:result.algorithm,source_asset_id:assetId,source,requires_review:true}};
    }, '识别泳道与条带');
    state.gelSuggestions = result;
    notify(`已定位 ${result.lane_centers.length} 条泳道、${result.bands.length} 个候选条带，请对照原图核对`, 'info');
    return result;
  } catch (error) {if (error.message !== 'cancelled'&&error.name!=='AbortError') notify(`定位失败：${error.message}`, 'error');}
  finally {worker?.terminate();if(state.gelTaskToken===token){state.gelWorker=null;state.gelReject=null;state.gelAbort=null;state.gelBusy=false;}emit();}
}

export function cancelGel() {state.gelAbort?.abort();state.gelWorker?.terminate();state.gelReject?.(new Error('cancelled'));state.gelReject=null;state.gelAbort=null;state.gelBusy=false;state.gelWorker=null;state.gelTaskToken=null;notify('已取消泳道识别', 'info');emit();}

export function evenlySpaceLanes() {
  const asset = selectedAsset(); if (!asset || asset.kind === 'table') return;
  const count = Math.round(Number(state.gelLaneCount)); if (!count || count < 1 || count > 100) {notify('请输入 1–100 之间的泳道数量', 'info'); return;}
  mutate(project => {
    const edit = editableSource(project), crop = edit.crop, previous = edit.gel.lane_names || [];
    edit.gel.lane_centers = Array.from({length:count},(_,index) => crop.x + crop.width / count * (index+.5));
    edit.gel.lane_names = Array.from({length:count},(_,index) => previous[index] || ''); edit.gel.hidden_lanes = [];
  }, '手动设置泳道');
}

export async function estimateMarkerAt(targetY) {
  const points = selectedEdit().gel?.marker_points || [];
  if (points.length < 2) {notify('至少添加两个已知 Marker 校准点', 'info'); return;}
  const worker = new Worker('/gel-worker.js'), id = uid('calibrate');
  try {
    const result = await new Promise((resolve,reject) => {worker.onmessage = ({data}) => data.error ? reject(new Error(data.error)) : resolve(data.result); worker.onerror = error => reject(error); worker.postMessage({id,action:'calibrate',points,target_y:targetY});});
    state.markerEstimate = {...result,y:targetY,unit:selectedEdit().gel.marker_unit}; emit();
    notify(`${result.extrapolated ? '外推估计 ≈ ' : '插值估计 '}${Number(result.size.toPrecision(4))} ${selectedEdit().gel.marker_unit}`, 'info');
  } catch(error) {notify(error.message,'error');}
  finally {worker.terminate();}
}
