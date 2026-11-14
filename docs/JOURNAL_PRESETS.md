# 可核验期刊预设

数据模块：`web/modules/journal-presets.js`。核验日期：**2026-11-12**。

预设提供真实物理宽度与可追溯来源，帮助组图；它不等同于投稿合规认证。所选预设、当前画布尺寸、导出格式及源图质量是不同信息，必须分别判断。

## 已纳入的尺寸

| 预设 | 宽度及含义 | 最大图高 | 核验范围 |
| --- | --- | --- | --- |
| Nature 单栏 | 89 mm，印刷单栏宽度 | 170 mm | Nature 主刊主图最终图稿 |
| Nature 双栏 | 183 mm，印刷双栏宽度 | 170 mm | Nature 主刊主图最终图稿 |
| eLife 一般图 | 至少 100 mm，上传图物理宽度下限 | 未确认 | eLife 图稿上传 |
| eLife 整页宽图 | 至少 200 mm，预计占整页宽的上传图下限 | 未确认 | eLife 图稿上传 |
| Cell 单栏 / 1.5 栏 / 整页宽 | 最大 85 / 114 / 174 mm，来自 2025-09-11 官方页面快照 | 未确认 | 历史指南中的研究论文 / 综述双栏版式；现行规则待复核 |
| Cell 自定义 | 保留当前尺寸；当前官方数值待核验 | 未确认 | 记录目标期刊，暂不施加数值规则 |
| 通用 | 85 / 120 / 180 mm，自由选择 | 无期刊限制 | 软件通用预设，不代表某一期刊标准 |

没有一个预设规定固定高度，因此全部 `height_mm` 为 `null`。Nature 的 `max_height_mm: 170` 是高度上限，应用模板时可以保留更低的现有高度；不应把上限直接设为每张图的高度。

### Nature

一手来源：

- [Nature research figure guide — Building and exporting figure panels](https://research-figure-guide.nature.com/figures/building-and-exporting-figure-panels/)
- [Nature — Final submission](https://www.nature.com/nature/for-authors/final-submission)

现行 figure guide 明确列出 89 mm / 183 mm 印刷栏宽与 170 mm 主图最大高度。最终投稿指南出现的 247 mm 是整页深度，不能用作主图最高值。该预设不代表所有 Nature Portfolio 子刊的要求，也不代表 Extended Data 的规则。

一般图中文字建议 5–7 pt；最终投稿指南另要求面板标号使用小写字母、8 pt 粗体正体。两者分别记录，避免把 8 pt 的面板编号误报为正文超限。程序只对显式记录字号的项目给出提示，不猜测嵌入图片中文字的字号，也不重写手动标号。

主图需要可编辑矢量文件和可编辑文字。选择正确毫米尺寸不能让 PNG 或整图栅格化的 PDF 自动符合此要求。最终投稿指南要求照片在最大印刷尺寸下至少 300 dpi，单独记录为 `resolution_by_type.photograph.min_ppi`。该要求不能直接外推为纯线图或混合图的通用标准，因此顶层 `min_ppi` 保留 `null`。源图有效 PPI 继续由项目已有的预检计算。

### eLife

一手来源：[eLife 官方投稿平台 Author Instructions](https://elife-rp.msubmit.net/html/elife-rp_author_instructions.html)。本次通过该地址的公开 HTTP 正文核验。

该指南要求上传图至少 300 dpi、一般图物理宽度至少 10 cm；预计占整页宽的图至少 20 cm，并仍保持至少 300 dpi。它明确适用于上传的图，因此记录 `min_ppi: 300` 和 `resolution_scope: 'all-uploaded-figures'`。这并不表示提高导出 DPI 可以恢复低分辨率原始图片的细节。

100 / 200 mm 是上传尺寸的下限，而非固定的单栏 / 双栏模板。程序只在当前宽度低于所选类别下限时提示；宽于下限不会被误报。指南另要求 RGB 色彩空间，并不支持多页图及多页 figure supplements；本模块没有声称自动核验导出文件的色彩空间或页数。

### Cell

官方入口：[Cell Press — Figure Guidelines](https://www.cell.com/information-for-authors/figure-guidelines)；另有历史入口 `https://www.cell.com/figureguidelines`。

本次访问未能取得现行指南正文，返回访问错误或 `Site Unavailable` 页面。随后通过正常 HTTP 成功读取了 [2025-09-11 归档的官方 Figure Guidelines](https://web.archive.org/web/20250911134049/https://www.cell.com/information-for-authors/figure-guidelines)，核实了发布者当时的原文；不是把第三方博客的转述当作来源。

历史指南在研究论文、综述等双栏版式中给出最大宽度：单栏 8.5 cm、1.5 栏 11.4 cm、整页 17.4 cm。程序换算为 85 / 114 / 174 mm，并把条目命名为“2025 指南”，记录 `verification_status: 'archived'`、`source_date: '2025-09-11'` 与完整快照地址。其三栏评论版式及 STAR Protocols 另有尺寸，不混入这三项。

该历史来源还明确要求大写面板标号、文字约 6–8 pt；分辨率按目标印刷尺寸分为彩色或灰度至少 300 dpi、黑白至少 500 dpi、线图至少 1000 dpi。这些值保存在 `resolution_by_type` 中，未给所有图统设一个 300 ppi 阈值。

历史正文可核验，不代表它截至 2026-11-12 仍然完整有效。预检会提示使用了历史规则，暂不据此判定当前投稿尺寸不合格。`verified_fields` 在这些条目中仅表示已对照该历史来源的字段。页面取不到，也不意味着官方没有现行栏宽要求。

另保留 `cell-custom`，明确标为 `verification_status: 'unverified'`，尺寸为 `null`，选用时保留当前画布。取得现行官方正文后，才能更新状态、字段来源及核验日期。

## 数据接口

```js
import {JOURNAL_PRESETS, journalPresetById, journalPreflight}
  from './modules/journal-presets.js';

const preset = journalPresetById('nature-single');
const issues = journalPreflight(project);
```

目录及其嵌套数组、对象都被冻结，避免某个项目修改全局模板。查不到 ID 时返回 `null`。

条目的共同字段包含 `id`、`title`、`journal`、`category`、`verification_status`、`scope`、`width_mm`、`height_mm`、`max_height_mm`、`min_ppi`、`source_url`、`source_urls`、`checked_at`、`notes`、`verified_fields`。`field_sources` 对已核验的不同字段进一步记录来源。通用预设的 `checked_at` 是目录整理日期，配合空 `verified_fields` 和 `not-applicable` 状态使用，不表示期刊核验。

`width_semantics` 必须保留：`print-column` 表示印刷栏宽，`minimum` 表示上传最小宽度，`maximum` 表示所述类别的最大宽度，`suggestion` 表示软件建议，`custom` 表示保留自定义值。调用方只应用有限正数宽度；遇到 `null` 必须保留画布，不能用数值强制转换产生零宽。

所选模板保存到 `project.settings.composition.journal_preset_id`。应用时可把完整模板快照保存到 `journal_preset_snapshot` 以便溯源；预检从内置目录读取已核验规则，不执行导入快照自行声明的规则。

## 预检边界

`journalPreflight(project)` 返回 `{severity, code, message, panel_id?}[]`，不修改项目、不阻止保存或导出，不重复后端的源图 PPI、裁切、边界及内容完整性检查。

当前可核验项包括 Nature 主图高度、所选 Nature 栏宽与当前宽度的差异、eLife 上传宽度下限、Nature 已显式填写的面板标号及文字字号。栏宽变化给出信息提示，允许作者按编辑要求采用自定义尺寸。字体检查只覆盖可识别的编辑对象；图片内部的文字及未声明字号的对象仍需要人工核对。

未知 ID、使用历史规则、尚未核验的 Cell 自定义项分别给出明确提示。没有选用模板或选用通用模板时，不生成期刊规范问题。目录没有提供某项规则时，预检不会推断出合规结论或额外拒绝条件。
