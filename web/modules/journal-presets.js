/**
 * Publication presets are factual data, not a promise of journal acceptance.
 * Dimensions use millimetres; text uses points. No source image is resampled.
 * See docs/JOURNAL_PRESETS.md for source scope and verification limitations.
 */
const CHECKED_AT = '2026-11-12';
const NATURE_GUIDE = 'https://research-figure-guide.nature.com/figures/building-and-exporting-figure-panels/';
const NATURE_FINAL = 'https://www.nature.com/nature/for-authors/final-submission';
const ELIFE_GUIDE = 'https://elife-rp.msubmit.net/html/elife-rp_author_instructions.html';
const CELL_GUIDE = 'https://www.cell.com/information-for-authors/figure-guidelines';
const CELL_SNAPSHOT = 'https://web.archive.org/web/20250911134049/https://www.cell.com/information-for-authors/figure-guidelines';

function freezeTree(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    for (const nested of Object.values(value)) freezeTree(nested);
    Object.freeze(value);
  }
  return value;
}

const nature = {
  journal: 'Nature', category: 'journal', verification_status: 'verified',
  scope: 'main-figure-final-artwork', width_semantics: 'print-column',
  height_mm: null, max_height_mm: 170, min_ppi: null,
  resolution_by_type: {photograph: {min_ppi: 300, applies_at: 'maximum-print-size'}},
  label_style: 'lower', label_font_size_pt: 8,
  body_font_size_pt: {min: 5, max: 7}, editable_vector_required: true,
  source_url: NATURE_GUIDE, source_urls: [NATURE_GUIDE, NATURE_FINAL], checked_at: CHECKED_AT,
  verified_fields: ['width_mm', 'max_height_mm', 'body_font_size_pt', 'editable_vector_required', 'label_style', 'label_font_size_pt', 'resolution_by_type.photograph.min_ppi'],
  field_sources: {
    width_mm: NATURE_GUIDE, max_height_mm: NATURE_GUIDE,
    body_font_size_pt: NATURE_GUIDE, editable_vector_required: NATURE_GUIDE,
    label_style: NATURE_FINAL, label_font_size_pt: NATURE_FINAL,
    'resolution_by_type.photograph.min_ppi': NATURE_FINAL,
  },
  notes: [
    '适用于 Nature 主刊主图的最终图稿；不套用于全部 Nature Portfolio 期刊或 Extended Data。',
    '89 / 183 mm 为官方印刷栏宽；主图最高 170 mm。247 mm 是整页深度，不是主图高度上限。',
    '面板标号为小写字母、8 pt 粗体正体；其余文字建议 5–7 pt。',
    '主图要求可编辑矢量文件及可编辑文字。PNG 或将整图栅格化后封装的 PDF 不因此变成合规主图。',
    '未将照片的分辨率建议外推为所有线图和混合图通用的 300 ppi 标准。',
  ],
};

const elife = {
  journal: 'eLife', category: 'journal', verification_status: 'verified',
  scope: 'figure-upload', width_semantics: 'minimum',
  height_mm: null, max_height_mm: null, min_ppi: 300,
  label_style: null, label_font_size_pt: null,
  resolution_scope: 'all-uploaded-figures',
  source_url: ELIFE_GUIDE, source_urls: [ELIFE_GUIDE], checked_at: CHECKED_AT,
  verified_fields: ['min_width_mm', 'min_ppi', 'resolution_scope'],
  field_sources: {min_width_mm: ELIFE_GUIDE, min_ppi: ELIFE_GUIDE, resolution_scope: ELIFE_GUIDE},
  notes: [
    '官方要求上传图至少 300 dpi；一般图的物理宽度至少 10 cm，预计占整页宽的图至少 20 cm。',
    '100 / 200 mm 表示上传尺寸下限，不是固定单栏 / 双栏排版宽度。',
    '未确认统一的最大高度或面板标号字号，因此不施加相关限制。',
    '官方另要求 RGB 色彩空间，并不支持多页图或多页 figure supplements。',
  ],
};

const cellArchived = {
  journal: 'Cell', category: 'journal', verification_status: 'archived',
  scope: 'two-column-article-final-artwork', width_semantics: 'maximum',
  height_mm: null, max_height_mm: null, min_ppi: null,
  label_style: 'upper', label_font_size_pt: null, body_font_size_pt: {min: 6, max: 8},
  resolution_by_type: {
    color_or_grayscale: {min_ppi: 300}, black_and_white: {min_ppi: 500}, line_art: {min_ppi: 1000},
  },
  source_url: CELL_GUIDE, source_urls: [CELL_GUIDE, CELL_SNAPSHOT],
  source_snapshot_url: CELL_SNAPSHOT, source_date: '2025-09-11', checked_at: CHECKED_AT,
  verified_fields: ['max_width_mm', 'label_style', 'body_font_size_pt', 'resolution_by_type'],
  field_sources: {
    max_width_mm: CELL_SNAPSHOT, label_style: CELL_SNAPSHOT,
    body_font_size_pt: CELL_SNAPSHOT, resolution_by_type: CELL_SNAPSHOT,
  },
  notes: [
    '尺寸来自 2025-09-11 归档的官方 Cell Press Figure Guidelines；本次核验确认了历史正文，现行页面仍未返回可用正文，投稿前需复核。',
    '适用于研究论文、综述等双栏版式：单栏 / 1.5 栏 / 整页宽的最大宽度分别为 85 / 114 / 174 mm。不套用于三栏评论版式或 STAR Protocols。',
    '历史指南要求面板大写字母，文字约 6–8 pt；未单独规定面板标号固定字号。',
    '历史分辨率按图类区分：彩色或灰度至少 300 dpi、黑白至少 500 dpi、线图至少 1000 dpi，均按目标印刷尺寸判断。',
  ],
};

function generic(width) {
  return {
    id: `generic-${width}`, title: `通用 · ${width} mm`, journal: null,
    category: 'generic', verification_status: 'not-applicable', scope: 'custom-layout',
    width_mm: width, height_mm: null, max_height_mm: null, min_ppi: null,
    width_semantics: 'suggestion', label_style: null, label_font_size_pt: null,
    source_url: null, source_urls: [], checked_at: CHECKED_AT, verified_fields: [],
    notes: ['软件提供的通用组图宽度，可自由修改；不代表任何期刊已核验的图稿标准。'],
  };
}

export const JOURNAL_PRESETS = freezeTree([
  {...nature, id: 'nature-single', title: 'Nature · 单栏 89 mm', width_mm: 89},
  {...nature, id: 'nature-double', title: 'Nature · 双栏 183 mm', width_mm: 183},
  {...elife, id: 'elife-standard', title: 'eLife · 一般图最低 100 mm', width_mm: 100, min_width_mm: 100},
  {...elife, id: 'elife-full', title: 'eLife · 整页宽图最低 200 mm', width_mm: 200, min_width_mm: 200},
  {...cellArchived, id: 'cell-single', title: 'Cell · 单栏 85 mm（2025 指南）', width_mm: 85, max_width_mm: 85},
  {...cellArchived, id: 'cell-one-half', title: 'Cell · 1.5 栏 114 mm（2025 指南）', width_mm: 114, max_width_mm: 114},
  {...cellArchived, id: 'cell-double', title: 'Cell · 整页宽 174 mm（2025 指南）', width_mm: 174, max_width_mm: 174},
  {
    id: 'cell-custom', title: 'Cell · 自定义（官方数值待核验）', journal: 'Cell',
    category: 'journal', verification_status: 'unverified', scope: 'custom-layout',
    width_mm: null, height_mm: null, max_height_mm: null, min_ppi: null,
    width_semantics: 'custom', label_style: null, label_font_size_pt: null,
    source_url: CELL_GUIDE, source_urls: [CELL_GUIDE], checked_at: CHECKED_AT,
    verified_fields: [],
    notes: [
      '核验时官方 Figure Guidelines 页面未返回可用正文，尚未确认现行尺寸、字号和各类图像分辨率要求。',
      '选择此项仅记录目标期刊并保留当前画布尺寸。可先用通用预设组图，再按官方最新指南或编辑要求调整。',
      '未把第三方转引的 85 / 114 / 174 mm 标成已核验的现行 Cell 规范；这也不意味着官方没有尺寸规定。',
    ],
  },
  generic(85), generic(120), generic(180),
]);

const presetsById = new Map(JOURNAL_PRESETS.map(preset => [preset.id, preset]));

/** Returns immutable catalogue data, or null for an absent/unknown identifier. */
export function journalPresetById(id) {
  return presetsById.get(id) ?? null;
}

const finitePositive = value => typeof value === 'number' && Number.isFinite(value) && value > 0;
const DIMENSION_TOLERANCE_MM = 0.05;

/**
 * Non-blocking checks of facts that this catalogue can verify.
 * Does not mutate the project, infer image types, or repeat source-PPI checks.
 * A saved snapshot is provenance only: imported snapshots cannot invent rules.
 */
export function journalPreflight(project) {
  const presetId = project?.settings?.composition?.journal_preset_id;
  if (!presetId) return [];
  const preset = journalPresetById(presetId);
  if (!preset) return [{
    severity: 'warning', code: 'JOURNAL_PRESET_UNKNOWN',
    message: '项目记录的期刊模板不在当前目录中，请重新选择模板并复核原始投稿指南。',
  }];
  if (preset.category === 'generic') return [];
  if (preset.verification_status === 'archived') return [{
    severity: 'warning', code: 'JOURNAL_RULES_ARCHIVED',
    message: `${preset.title} 根据 ${preset.source_date} 的官方网页快照提供参考；现行规范尚未核验，请在投稿前复核。`,
  }];
  if (preset.verification_status !== 'verified') return [{
    severity: 'warning', code: 'JOURNAL_RULES_UNVERIFIED',
    message: `${preset.journal} 的现行官方图稿数值尚未核验；当前画布保留自定义尺寸，请按官方指南复核。`,
  }];

  const issues = [];
  const add = (severity, code, message, panel) => {
    const issue = {severity, code, message};
    if (panel?.id != null) issue.panel_id = panel.id;
    issues.push(issue);
  };
  const figure = project?.figure ?? {};
  if (finitePositive(preset.max_height_mm) && finitePositive(figure.height_mm)
      && figure.height_mm > preset.max_height_mm + DIMENSION_TOLERANCE_MM) {
    add('warning', 'JOURNAL_MAX_HEIGHT', `${preset.journal} 此类主图最高 ${preset.max_height_mm} mm；当前高度为 ${figure.height_mm} mm。`);
  }
  if (finitePositive(preset.min_width_mm) && finitePositive(figure.width_mm)
      && figure.width_mm < preset.min_width_mm - DIMENSION_TOLERANCE_MM) {
    add('warning', 'JOURNAL_MIN_WIDTH', `${preset.title} 要求上传宽度至少 ${preset.min_width_mm} mm；当前宽度为 ${figure.width_mm} mm。`);
  } else if (preset.width_semantics === 'print-column' && finitePositive(figure.width_mm)
      && Math.abs(figure.width_mm - preset.width_mm) > DIMENSION_TOLERANCE_MM) {
    add('info', 'JOURNAL_PRESET_WIDTH_CHANGED', `当前宽度 ${figure.width_mm} mm 与所选 ${preset.title} 不同；可重新应用模板，或按编辑要求保留自定义尺寸。`);
  }

  for (const panel of Array.isArray(project?.panels) ? project.panels : []) {
    if (!panel || !String(panel.label ?? '').trim()) continue;
    if (preset.label_style === 'lower' && /^[A-Z]+$/.test(String(panel.label).trim())) {
      add('warning', 'JOURNAL_PANEL_LABEL_STYLE', 'Nature 主图面板标号使用小写字母；手动标号请自行复核。', panel);
    }
    if (finitePositive(preset.label_font_size_pt) && finitePositive(panel.label_font_size_pt)
        && Math.abs(panel.label_font_size_pt - preset.label_font_size_pt) > 0.01) {
      add('warning', 'JOURNAL_PANEL_LABEL_SIZE', `Nature 主图面板标号字号为 ${preset.label_font_size_pt} pt；当前为 ${panel.label_font_size_pt} pt。`, panel);
    }
  }
  const range = preset.body_font_size_pt;
  if (range) {
    for (const annotation of Array.isArray(project?.annotations) ? project.annotations : []) {
      if (!annotation || annotation.type !== 'text' || !String(annotation.text ?? '').trim()
          || !finitePositive(annotation.font_size_pt)) continue;
      if (annotation.font_size_pt < range.min || annotation.font_size_pt > range.max) {
        add('warning', 'JOURNAL_TEXT_SIZE', `Nature 主图正文文字建议 ${range.min}–${range.max} pt；文字标注「${String(annotation.text).slice(0, 32)}」为 ${annotation.font_size_pt} pt。`);
      }
    }
  }
  return issues;
}
