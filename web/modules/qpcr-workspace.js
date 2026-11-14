import {clone} from './domain.js';
import {defaultAnalysisParams} from './analysis-domain.js';

// Source row numbers and sample names only identify wells within one worksheet.
// Keep the complete qPCR recipe with that immutable original, never globally.
export const qpcrParameterFields=Object.freeze(['reference_gene','reference_genes','control_group','test','group_mapping','excluded_source_rows','max_ct','technical_sd_limit']);
const record=value=>value&&typeof value==='object'&&!Array.isArray(value);
const sheetName=value=>value==null||value===''?null:String(value);
const sheetKey=value=>JSON.stringify(sheetName(value));
const own=(value,key)=>record(value)&&Object.hasOwn(value,key)?value[key]:null;

function recipe(values={}) {
  const defaults=defaultAnalysisParams(),result={};
  for(const field of qpcrParameterFields)result[field]=clone(values[field] ?? defaults[field]);
  return result;
}

/** Migrate unbound legacy settings once, to the previous current table only. */
export function initializeQpcrWorkspaces(project,currentAssetId=null) {
  if(!project?.settings||record(project.settings.qpcr_workspaces))return false;
  const workspaces=project.settings.qpcr_workspaces={};
  if(project.assets.some(asset=>asset.id===currentAssetId&&asset.kind==='table')) {
    const previous=project.settings.analysis_params || {},sheet=sheetName(previous.sheet);
    workspaces[currentAssetId]={sheet,worksheets:{[sheetKey(sheet)]:recipe(previous)}};
  }
  return true;
}

export function qpcrParameters(project,assetId) {
  const workspace=own(project?.settings?.qpcr_workspaces,assetId),sheet=sheetName(workspace?.sheet);
  return {...recipe(own(workspace?.worksheets,sheetKey(sheet)) || {}),sheet};
}

function workspaceForEdit(project,assetId) {
  if(!project.assets.some(asset=>asset.id===assetId&&asset.kind==='table'))throw new Error('请先选择需要编辑的 Ct 数据表');
  initializeQpcrWorkspaces(project);
  const workspaces=project.settings.qpcr_workspaces;
  if(!record(own(workspaces,assetId)))workspaces[assetId]={sheet:null,worksheets:{}};
  const workspace=workspaces[assetId];
  if(!record(workspace.worksheets))workspace.worksheets={};
  workspace.sheet=sheetName(workspace.sheet);
  return workspace;
}

export function editableQpcrParameters(project,assetId) {
  const workspace=workspaceForEdit(project,assetId),key=sheetKey(workspace.sheet);
  if(!record(own(workspace.worksheets,key)))workspace.worksheets[key]=recipe();
  return workspace.worksheets[key];
}

export function selectQpcrWorksheet(project,assetId,sheet) {
  const workspace=workspaceForEdit(project,assetId);
  workspace.sheet=sheetName(sheet);
  editableQpcrParameters(project,assetId);
}

/** Bind auto-discovery to its actual worksheet; never move another sheet's edits. */
export function bindQpcrWorksheet(project,assetId,requestedSheet,resolvedSheet) {
  const actual=sheetName(resolvedSheet);
  if(sheetName(requestedSheet)!==null||actual===null)return false;
  const workspace=workspaceForEdit(project,assetId);
  if(workspace.sheet!==null)return false;
  const key=sheetKey(actual);
  if(!record(own(workspace.worksheets,key)))workspace.worksheets[key]=recipe(own(workspace.worksheets,sheetKey(null)) || {});
  workspace.sheet=actual;
  return true;
}

export function qpcrTableMatches(table,assetId,params) {
  if(!table?.canonical||table.asset_id!==assetId)return false;
  const selected=sheetName(params?.sheet);
  return selected===null||sheetName(table.sheet)===selected;
}
