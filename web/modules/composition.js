/** Composition commands shared by every local-workbench entry point. */
import {state,mutate,emit,notify,selectedPanels,selectedPanel,selectPanel} from './state.js';
import {clone,uid,panelFromAsset,defaultEdit,assetFor,round,escapeHtml as e} from './domain.js';
import {applyPanelLayout,resizeFigure,ensureCompositionSettings,reconcilePanelLabels,layoutOptions,panelBounds} from './layout-engine.js';
import {JOURNAL_PRESETS,journalPresetById,journalPreflight} from './journal-presets.js';

export function selectionBounds(panels) {
  if(!panels.length)return null;
  const boxes=panels.map(panelBounds),left=Math.min(...boxes.map(box=>box.left)),top=Math.min(...boxes.map(box=>box.top));
  const right=Math.max(...boxes.map(box=>box.right)),bottom=Math.max(...boxes.map(box=>box.bottom));
  return {x_mm:left,y_mm:top,width_mm:right-left,height_mm:bottom-top,left,top,right,bottom};
}

function requireUngrouped(project) {
  if(project.panels.some(panel=>panel.group_id))throw new Error('当前包含已编组面板。请先解组再自动排列，或使用「等比适配」保留组内布局。');
}

export function arrangeComposition(overrides={}) {
  requireUngrouped(state.project);
  mutate(project=>applyPanelLayout(project,overrides),'应用组图布局');
  state.tab='compose';state.compositionInspectorTab='layout';emit();
}

/** A whole batch is planned before being committed. Original sources stay intact. */
export function addAssetsToComposition(assetIds,{layout=true,position=null,unusedOnly=false}={}) {
  const existing=unusedOnly?new Set(state.project.panels.map(panel=>panel.asset_id)):new Set();
  const assets=[...new Set(assetIds)].map(id=>assetFor(state.project,id)).filter(asset=>asset&&asset.kind!=='table'&&!existing.has(asset.id));
  if(!assets.length){notify('没有可加入的新图像、PDF 或 SVG 素材','info');return;}
  if(state.project.panels.length+assets.length>1000)throw new Error('面板总数不能超过 1000。请拆分为多个图稿。');
  const newIds=[];
  if(layout)requireUngrouped(state.project);
  mutate(project=>{
    for(const asset of assets){
      const panel=panelFromAsset(project,asset,clone(project.settings.asset_edits?.[asset.id] || defaultEdit(asset)));
      panel.label='';panel.label_mode='auto';
      if(position&&assets.length===1){panel.x_mm=round(Math.max(0,Math.min(position.x,project.figure.width_mm-panel.width_mm)));panel.y_mm=round(Math.max(6,Math.min(position.y,project.figure.height_mm-panel.height_mm)));}
      project.panels.push(panel);newIds.push(panel.id);
    }
    if(layout)applyPanelLayout(project);
  },`加入 ${assets.length} 个面板${layout?'并自动排版':''}`);
  state.selectedPanelIds=newIds;state.selectedPanelId=newIds.at(-1);state.selectedAssetId=assets.at(-1).id;state.selectedAnnotationId=null;state.tab='compose';state.compositionInspectorTab=layout?'layout':'selection';emit();
}

export function resizeComposition(width,height,mode) {
  if(width===state.project.figure.width_mm&&height===state.project.figure.height_mm)return;
  const behavior=mode || state.project.settings.composition?.resize_mode || 'fit';
  if(behavior==='reflow')requireUngrouped(state.project);
  mutate(project=>resizeFigure(project,width,height,{mode:behavior}),'调整画布并适配版式');
}

export function applyJournalPreset(id=state.compositionPresetDraft || state.project.settings.composition?.journal_preset_id || 'generic-180') {
  // The select commits its own value. Keep no uncommitted preset outside the
  // undoable project, including when geometry validation rejects a selection.
  state.compositionPresetDraft=null;
  const composition=ensureCompositionSettings(state.project);
  if(!id){
    if(composition.journal_preset_id)mutate(project=>{const options=ensureCompositionSettings(project);options.journal_preset_id=null;options.journal_preset_snapshot=null;},'改为自定义画布');
    return;
  }
  const preset=journalPresetById(id);
  if(!preset)throw new Error('请选择可用的尺寸预设。');
  const mode=composition.resize_mode || 'fit',width=preset.width_mm || state.project.figure.width_mm;
  const height=preset.height_mm || Math.min(state.project.figure.height_mm,preset.max_height_mm || Infinity),sizeChanged=width!==state.project.figure.width_mm||height!==state.project.figure.height_mm;
  if(id===composition.journal_preset_id&&!sizeChanged)return;
  if(mode==='reflow')requireUngrouped(state.project);
  mutate(project=>{
    const options=ensureCompositionSettings(project);
    if(preset.label_style)options.numbering.style=preset.label_style;
    if(preset.label_font_size_pt)for(const panel of project.panels)if(panel.label_mode==='auto')panel.label_font_size_pt=preset.label_font_size_pt;
    reconcilePanelLabels(project);
    // A new preset can enlarge captions without changing the canvas size.
    // Include that adaptation in this one explicit preset-selection transaction.
    resizeFigure(project,width,height,{mode});
    const applied=ensureCompositionSettings(project);applied.journal_preset_id=id;applied.journal_preset_snapshot=clone(preset);
  },`应用 ${preset.title} 尺寸预设`);
}

export function compositionIssues(project) {return journalPreflight(project);}

function unitsForSelection() {
  const chosen=selectedPanels(),groups=new Map();
  for(const panel of chosen){const key=panel.group_id || panel.id;if(!groups.has(key))groups.set(key,[]);groups.get(key).push(panel);}
  return [...groups.values()].map(members=>({members,bounds:selectionBounds(members)}));
}

export function alignComposition(direction) {
  const units=unitsForSelection();if(!units.length)return;
  const bounds=units.length===1?{left:6,top:6,right:state.project.figure.width_mm-6,bottom:state.project.figure.height_mm-6}:selectionBounds(selectedPanels());
  const shifts=units.map(unit=>{
    const b=unit.bounds;let dx=0,dy=0;
    if(direction==='left')dx=bounds.left-b.left;if(direction==='center')dx=(bounds.left+bounds.right-b.left-b.right)/2;if(direction==='right')dx=bounds.right-b.right;
    if(direction==='top')dy=bounds.top-b.top;if(direction==='middle')dy=(bounds.top+bounds.bottom-b.top-b.bottom)/2;if(direction==='bottom')dy=bounds.bottom-b.bottom;
    return {ids:unit.members.map(panel=>panel.id),dx,dy};
  });
  mutate(project=>{for(const shift of shifts)for(const panel of project.panels)if(shift.ids.includes(panel.id)){panel.x_mm=round(panel.x_mm+shift.dx);panel.y_mm=round(panel.y_mm+shift.dy);}},'对齐选中面板');
}

export function distributeComposition(axis) {
  const units=unitsForSelection(),horizontal=axis==='x';
  if(units.length<3){notify('等间距分布至少需要选择三个面板或组合','info');return;}
  const start=horizontal?'left':'top',end=horizontal?'right':'bottom',size=horizontal?'width_mm':'height_mm';
  units.sort((a,b)=>a.bounds[start]-b.bounds[start]);
  const minimum=units[0].bounds[start],maximum=Math.max(...units.map(unit=>unit.bounds[end]));
  const gap=(maximum-minimum-units.reduce((sum,unit)=>sum+unit.bounds[size],0))/(units.length-1);
  if(gap<-.001)throw new Error('选区没有足够的空隙。请先拉开外侧面板，再执行等间距分布。');
  let cursor=minimum;const shifts=units.map(unit=>{const delta=cursor-unit.bounds[start];cursor+=unit.bounds[size]+Math.max(0,gap);return {ids:unit.members.map(panel=>panel.id),delta};});
  mutate(project=>{for(const shift of shifts)for(const panel of project.panels)if(shift.ids.includes(panel.id)){const key=horizontal?'x_mm':'y_mm';panel[key]=round(panel[key]+shift.delta);}},'等间距分布面板');
}

export function groupComposition(remove=false) {
  const panels=selectedPanels();if(!panels.length)return;
  if(!remove&&panels.length<2){notify('按住 Shift 或 Ctrl 选择至少两个面板','info');return;}
  const ids=new Set(panels.map(panel=>panel.id)),groupId=uid('group');
  mutate(project=>{for(const panel of project.panels)if(ids.has(panel.id)){if(remove)delete panel.group_id;else panel.group_id=groupId;}},remove?'解散面板组合':'编组面板');
}

export function duplicateComposition() {
  const selected=selectedPanels();if(!selected.length)return;
  if(state.project.panels.length+selected.length>1000)throw new Error('面板总数不能超过 1000。');
  const ids=[],groups=new Map();
  mutate(project=>{for(const panel of selected){const copy={...clone(panel),id:uid('panel'),x_mm:round(panel.x_mm+4),y_mm:round(panel.y_mm+4),label:'',label_mode:'auto'};
    if(copy.group_id){if(!groups.has(copy.group_id))groups.set(copy.group_id,uid('group'));copy.group_id=groups.get(copy.group_id);}
    project.panels.push(copy);ids.push(copy.id);
  }},'复制选中面板');
  state.selectedPanelIds=ids;state.selectedPanelId=ids.at(-1);state.compositionInspectorTab='selection';emit();
}

export function selectAllComposition() {state.selectedPanelIds=state.project.panels.map(panel=>panel.id);state.selectedPanelId=state.selectedPanelIds.at(-1)||null;state.selectedAnnotationId=null;state.compositionInspectorTab='selection';emit();}

export function changeCompositionOption(element) {
  if(element.matches('[data-journal-choice]')){
    try {applyJournalPreset(element.value);}
    catch(error){element.value=state.project.settings.composition?.journal_preset_id || '';throw error;}
    return true;
  }
  if(element.matches('[data-layout-option]')){
    const key=element.dataset.layoutOption,value=element.tagName==='SELECT'?element.value:Number(element.value);
    const current=layoutOptions(state.project);
    try {
      if(!Object.hasOwn(current,key))throw new Error('未知的布局参数。');
      if(typeof value==='number'&&(!Number.isFinite(value)||element.value.trim()===''||value<0||value>1000||(['columns','rows'].includes(key)&&!Number.isInteger(value))))throw new Error('布局参数必须是有效的非负数，行列数应为整数（0 表示自动）。');
      if(value===current[key])return true;
      requireUngrouped(state.project);
      mutate(project=>applyPanelLayout(project,{[key]:value}),'调整布局参数');
    } catch(error){element.value=String(current[key] ?? '');throw error;}
    return true;
  }
  if(element.matches('[data-numbering-option]')){const key=element.dataset.numberingOption,value=element.type==='checkbox'?element.checked:element.value;if(ensureCompositionSettings(state.project).numbering[key]!==value)mutate(project=>{ensureCompositionSettings(project).numbering[key]=value;},'设置自动编号');return true;}
  if(element.matches('[data-figure-option]')){const key=element.dataset.figureOption,value=element.type==='checkbox'?element.checked:element.value;if(ensureCompositionSettings(state.project)[key]!==value)mutate(project=>{ensureCompositionSettings(project)[key]=value;},'设置组图行为');return true;}
  if(element.matches('[data-label-mode]')){const panel=selectedPanel();if(panel)mutate(project=>{const item=project.panels.find(entry=>entry.id===panel.id);item.label_mode=element.value;if(element.value!=='manual')item.label='';},'设置面板编号方式');return true;}
  return false;
}

export const compositionActions={
  'composition-inspector':element=>{state.compositionInspectorTab=element.dataset.view;emit();},
  'apply-layout':()=>arrangeComposition(),
  'layout-preset':element=>arrangeComposition({preset:element.dataset.layout || 'grid',columns:Number(element.dataset.columns)||0,rows:Number(element.dataset.rows)||0}),
  'apply-journal-preset':()=>applyJournalPreset(),
  'compose-all-assets':()=>addAssetsToComposition(state.project.assets.map(asset=>asset.id),{unusedOnly:true}),
  'select-all-panels':selectAllComposition,
  'group-panels':()=>groupComposition(),
  'ungroup-panels':()=>groupComposition(true),
  'distribute-panels':element=>distributeComposition(element.dataset.axis),
  'renumber-auto':()=>mutate(project=>reconcilePanelLabels(project),'更新自动编号'),
  'renumber-all':()=>mutate(project=>reconcilePanelLabels(project,{force:true}),'将全部面板改为自动编号'),
  'select-composition-panel':(element,event)=>selectPanel(element.dataset.id,{toggle:event.shiftKey||event.ctrlKey||event.metaKey}),
};

const option=(value,title,current)=>`<option value="${e(value)}" ${value===current?'selected':''}>${e(title)}</option>`;
const section=(title,html)=>`<section class="inspector-section composition-section"><div class="inspector-heading"><h3>${title}</h3></div>${html}</section>`;
const numeric=(label,key,value,unit='mm')=>`<label class="field"><span>${label}</span><div class="input-with-unit"><input id="layout-${key}" type="number" min="0" max="1000" step="${unit==='mm'?'any':'1'}" data-layout-option="${key}" value="${value}" aria-label="${label}" required><small>${unit}</small></div></label>`;

export function renderCompositionControls(project) {
  const composition=ensureCompositionSettings(project),layout=layoutOptions(project),numbering=composition.numbering;
  const chosen=composition.journal_preset_id || '',preset=journalPresetById(chosen);
  const notes=(preset?.notes || []).slice(0,2).map(note=>`<p class="microcopy">${e(note)}</p>`).join('');
  return section('期刊与尺寸预设',`<label class="field"><span>投稿目标</span><select id="journal-preset" data-journal-choice>${option('','自定义画布',chosen)}${JOURNAL_PRESETS.map(item=>option(item.id,item.title,chosen)).join('')}</select></label>${notes}${preset?.source_url?`<a class="composition-source" href="${e(preset.source_url)}" target="_blank" rel="noopener noreferrer">查看规范来源 · ${e(preset.checked_at || '')}</a>`:''}<label class="field"><span>改变画布时</span><select id="composition-resize-mode" data-figure-option="resize_mode">${[['fit','等比适配，保留布局'],['reflow','按网格重新排版'],['canvas','只改画布，检查越界']].map(([value,title])=>option(value,title,composition.resize_mode || 'fit')).join('')}</select></label>`)
    +section('自动布局',`<div class="layout-presets">${[[2,2],[3,2],[2,3],[3,3]].map(([cols,rows])=>`<button class="button small" data-action="layout-preset" data-columns="${cols}" data-rows="${rows}" title="${cols} 列 × ${rows} 行">${cols} 列 × ${rows} 行</button>`).join('')}</div><label class="field"><span>布局方式</span><select id="layout-preset" data-layout-option="preset">${[['grid','规则网格'],['hero-left','左侧大图 + 右侧小图'],['hero-top','上方大图 + 下方小图']].map(([value,title])=>option(value,title,layout.preset)).join('')}</select></label><div class="field-grid">${numeric('列数（0 自动）','columns',layout.columns,'列')}${numeric('行数（0 自动）','rows',layout.rows,'行')}${numeric('水平间距','gap_x_mm',layout.gap_x_mm)}${numeric('垂直间距','gap_y_mm',layout.gap_y_mm)}${numeric('边距','margin_mm',layout.margin_mm)}</div><label class="checkbox-label"><input type="checkbox" data-figure-option="auto_import" ${composition.auto_import!==false?'checked':''}>拖入画布后自动排版</label><p class="microcopy">参数修改后自动生效。保持原图比例；不裁掉内容。空间不足会提示调整参数。</p>`)
    +section('自动编号',`<div class="field-grid"><label class="field"><span>编号样式</span><select id="numbering-style" data-numbering-option="style">${[['upper','A, B, … AA'],['lower','a, b, … aa'],['numeric','1, 2, 3']].map(([value,title])=>option(value,title,numbering.style)).join('')}</select></label><label class="field"><span>编号顺序</span><select id="numbering-order" data-numbering-option="order">${[['visual','从上到下、从左到右'],['source','面板加入顺序']].map(([value,title])=>option(value,title,numbering.order)).join('')}</select></label></div><label class="checkbox-label"><input type="checkbox" data-numbering-option="enabled" ${numbering.enabled?'checked':''}>显示自动编号</label><p class="microcopy">手工编号会锁定保留；自动编号跳过已占用值，支持超过 26 张图。</p><button class="button small full" data-action="renumber-all" title="将手工编号也改为自动编号">全部改为自动编号（覆盖手填）</button>`);
}

export function renderSelectionControls() {
  const panels=selectedPanels();if(panels.length<2)return '';
  return section(`已选择 ${panels.length} 个面板`,`<div class="inline-actions"><button class="button small" data-action="group-panels">编组</button><button class="button small" data-action="ungroup-panels">解组</button><button class="button small" data-action="duplicate-panel">复制</button></div><div class="layout-presets">${[['left','左对齐'],['center','水平居中'],['right','右对齐'],['top','顶部对齐'],['middle','垂直居中'],['bottom','底部对齐']].map(([direction,title])=>`<button class="button small" data-action="align-panel" data-align="${direction}">${title}</button>`).join('')}</div><div class="inline-actions"><button class="button small" data-action="distribute-panels" data-axis="x">水平等间距</button><button class="button small" data-action="distribute-panels" data-axis="y">垂直等间距</button></div><p class="microcopy">Shift / Ctrl 点选，拖动整体移动。组合在对齐与分布时保持内部位置。</p>`);
}
