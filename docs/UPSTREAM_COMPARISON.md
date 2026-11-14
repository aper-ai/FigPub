# GelFigure、ScieFig 与 Figure Workbench：比较及重构范围

本文记录固定提交的代码审计与 Figure Workbench 0.2 的实现边界。GelFigure 专注凝胶图的识别、标注和出版排版；ScieFig 覆盖更多实验类型，并提供统一组版工作台。Figure Workbench 将两者有价值的工作流程整理为同一套原件、原图坐标、分析结果和毫米画布模型，重新实现运行时代码。0.1 未覆盖的主要多通道显示/分析和官方 Cellpose CPU 路径已在 0.2 补入，详细差异见 [FEATURE_AUDIT_0.2.md](FEATURE_AUDIT_0.2.md)。自动识别准确率及专业实验适用性仍需结合真实材料验证。

## 1. 审计对象与证据范围

| 项目 | 确切仓库 | 本次审计提交 |
|---|---|---|
| GelFigure | [poplarity/GelFigure](https://github.com/poplarity/GelFigure) | `6fe1ff545e25eb6abcee6c490dba7e8678414a02` |
| ScieFig | [pamelah-233/ScieFig-Community](https://github.com/pamelah-233/ScieFig-Community) | `6cdaec4cf9ba91b75ceb431001ace93c4633e1cd` |

提交信息同时记录于 [UPSTREAM_REFERENCE.json](UPSTREAM_REFERENCE.json)。下文中的上游代码链接固定到上述提交，避免以后版本变化导致描述错位。上游判断主要来自实际保存格式、测量函数、渲染及导出路径；新实现的判断来自本仓库代码、自动化测试和记录的基准结果。没有将 README 截图、单个合成示例或代码阅读推断当作全面准确率评测。

GelFigure 的此提交在 `js/app.js` 中声明 `0.6.14`；ScieFig README 提供 `1.2.5` 下载信息。版本标签不能替代提交身份。[GelFigure 入口](https://github.com/poplarity/GelFigure/blob/6fe1ff545e25eb6abcee6c490dba7e8678414a02/js/app.js)；[ScieFig README](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/README.md)。

## 2. 共同点

两者都围绕科研图像的整理、注释与交付设计，支持将原图处理状态与图上文字、线条等信息分开保存。GelFigure 保留原文件字节与 SHA-256，并提供处理记录；ScieFig 的面板模型、审计事件和 `.sfig` 清单同样包含来源与处理状态。因此，新工程对来源追踪的设计是在这些已有能力基础上统一契约，并非假定上游完全没有记录功能。[GelFigure 工程保存](https://github.com/poplarity/GelFigure/blob/6fe1ff545e25eb6abcee6c490dba7e8678414a02/js/app.js)；[ScieFig 面板模型](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/models/panel.py)；[ScieFig 工程读写](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/sfig_io.py)。

两者都允许人工复核自动处理结果，并提供面向排版的物理尺寸、字体或分辨率控制。它们的矢量导出都可以包含真实文字、线条和栅格图像；文件扩展名为 SVG/PDF，并不意味着内部所有科学图像或文字都能逐对象修改。[GelFigure 导出说明](https://github.com/poplarity/GelFigure/blob/6fe1ff545e25eb6abcee6c490dba7e8678414a02/README.md)；[ScieFig Composer](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/ui/widgets/composer_canvas.py)。

两者的核心图像工作都能在本地完成。具体部署方式不同：GelFigure 使用浏览器本地 JavaScript；ScieFig 使用 Python/Qt 桌面程序及相关分析依赖。Figure Workbench 采用本地 Python 服务与浏览器界面，数值计算、原件存储和导出由本地服务执行。[GelFigure 开发与隐私说明](https://github.com/poplarity/GelFigure/blob/6fe1ff545e25eb6abcee6c490dba7e8678414a02/README.md)；[ScieFig 运行说明](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/README.md)；[本项目服务入口](../workbench/server.py)。

## 3. 主要区别

| 维度 | GelFigure | ScieFig | 对新实现的影响 |
|---|---|---|---|
| 主要任务 | DNA/蛋白凝胶的泳道、Marker、尺寸标注和排版 | qPCR、WB、荧光、IHC、明场及多图组版 | 保留凝胶专门交互，同时提供统一分析与组图入口 |
| 运行结构 | HTML/CSS/JavaScript，静态网页 | Python、PyQt6、多个面板模型和绘图/分割模块 | 新 Python 核心与界面解耦，分析模块不依赖 Qt |
| 凝胶识别 | 包含倾斜估计、泳道、条带、Ladder 匹配及人工锚点流程 | WB 侧重条带裁切、对齐、处理标签及灰度分析 | 新 Worker 实现定位和候选匹配；候选须确认后才用于标定 |
| WB 定量 | README 明确未提供 densitometry | 有灰度积分、背景扣除与内参归一化 | 新增原始位深测量与明确的样本身份模型 |
| qPCR | 无对应实验分析模块 | 导入仪器表格、内参/对照处理与绘图 | 重新实现表格映射、技术重复聚合、ΔΔCt 与真实观测值作图 |
| 显微分析 | 无对应通用分割模块 | 已有核/细胞分割、Cellpose、DAB、多通道及组织分析代码；README 另展示多标记/邻域工作流，固定源码尚未定位其完整实现 | 0.2 新增官方 Cellpose CPU、四类原值阈值、多通道 ROI/剖面/重叠相关；专用模型与染色专题未全部覆盖 |
| 工程容器 | `.gelfig` 为 JSON 或 gzip JSON，嵌入选中曝光 | `.sfig` 为 ZIP，包含项目、Composer、原件和哈希清单 | `.figwork` 统一保存全部已导入原件、分析记录及画布 |
| 多曝光/多源 | 浏览器历史可以保存多曝光；下载的 `.gelfig` 只含选中曝光 | `.sfig` 可收集多个面板和 WB 条带原件 | 明确区分容器内已包含的来源与无法从旧文件恢复的来源 |
| 组版层次 | 凝胶专门排版规则丰富 | 通用 Composer 与模块内布局较丰富 | 当前提供统一子图、基础标注和物理画布，复杂旧布局需要复核 |
| 源码许可 | MIT | AGPL-3.0-only | 本项目按 AGPL-3.0-only 交付，沿用目录数据保留 MIT 归属 |

表内依据见固定提交的 [GelFigure README](https://github.com/poplarity/GelFigure/blob/6fe1ff545e25eb6abcee6c490dba7e8678414a02/README.md)、[GelFigure 识别引擎](https://github.com/poplarity/GelFigure/blob/6fe1ff545e25eb6abcee6c490dba7e8678414a02/js/engine.js)、[ScieFig README](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/README.md)、[qPCR 计算器](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/analysis/qpcr_calculator.py)、[Composer 模型](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/models/composer.py)及两者 [MIT](https://github.com/poplarity/GelFigure/blob/6fe1ff545e25eb6abcee6c490dba7e8678414a02/LICENSE)、[AGPL](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/LICENSE)许可文件。

## 4. 代码审计发现怎样落实到新实现

### WB 的原始位深与背景算法

ScieFig 的 `core/densitometry.py::qimage_to_gray_numpy` 明确转换到 `QImage.Format_Grayscale8`，`measure_densitometry` 从该数组开始测量；其中饱和判断也使用 255。这个结论仅指该 WB 定量路径，不能推广成“ScieFig 所有显微模块均只处理 8-bit”。同文件中的 `rolling_ball_background` 注释说明使用箱式均值滤波近似，输出字段还经过 `_round4`。[源代码](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/densitometry.py)。

新 [analysis.py](../workbench/analysis.py) 直接接收源数组：先截取原图 ROI，再将局部数值转换成 float64 计算，保留原件 dtype 与字节。显示色阶、gamma、反相不会修改测量输入。背景方法明确分为边界中位数、真实 rolling-ball/ellipsoid 背景和不扣背景；signed background-subtracted integral 与 positive-only integral 分开输出。饱和依据原始数值范围或用户明确给定的 ADC 上限，结果保留计算精度。行为由 [test_analysis.py](../tests/test_analysis.py) 的位深、显示独立性、背景、饱和和原图 ROI 测试覆盖。

这里的默认饱和边界来自数组 dtype，可在核心/API 参数中用 `adc_bits`、`saturation_value`、`black_level` 明确覆盖；目前界面没有单独的 ADC 范围字段。12 位采集值存于 uint16 时，65535 并不是实际探测器上限，不能仅凭保存 dtype 判定未过曝。文件保持 16 位数值不等于自动知道采集动态范围。

### 实验身份、重复与统计图

新 WB 行记录分别保存实验、样本、生物重复、技术重复、曝光、目标/内参及蛋白身份。归一化要求目标和内参身份能够明确匹配；重复曝光不自动成为独立样本，重复源 ROI 会被标记，技术重复先聚合再计算生物重复的组统计。`n=1` 时标准差和标准误为空。[WB 实现](../workbench/analysis.py)。

新 qPCR 使用技术 Ct 聚合、参考基因归一化、同实验对照的生物样本 ΔCt 基线和 `2^-ΔΔCt`。若缺少显式生物重复编号而采用样本名作为身份，会给出提示；没有生物学实验设计信息的软件无法自行证明样本独立。它提供可选的生物样本 ΔCt 比较，不自动生成显著性星号。[qPCR 实现](../workbench/qpcr.py)。

新 [charts.py](../workbench/charts.py) 使用实际观测值生成点、均值/SD 或箱线图。中文标签使用打包 Noto SC 字体实际排版并转为 SVG 路径，避免依赖接收电脑的字体；SVG 描述元数据保留绘图数值与来源参数，新工程还保存完整测量及原件。**统计图内的路径文字通过重新生成图表修改，不承诺为独立文本对象。** 单张显微图中的对象数也会注明不等于生物学重复数。[图表测试](../tests/test_charts.py)；[API 源记录关联](../workbench/server.py)。

ScieFig README 已明确说明 WB 展示图的三次重复是用于展示绘图效果的模拟点。这是示例说明，不能据此断言其软件会伪造用户实验数据。本项目示例同样明确标为合成数据，真实结果区只使用实际分析完成后的结果。[上游 WB 示例说明](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/README.md)。

### 多来源归档与矢量交付

ScieFig 存在不同的“来源导出”路径：`core/export/wb_exporter.py::export_wb_source_data` 写出一个 `raw_source_path` 及清单，而 `.sfig` 的 `_collect_archive_members` 会遍历 WB strips、显微图及其他面板。两者覆盖范围不同，不能把前者的限制描述成 `.sfig` 整体不支持多源。[WB 来源包](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/export/wb_exporter.py)；[多源工程容器](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/sfig_io.py)。

ScieFig Composer 对 SVG 和部分图表有 `QSvgRenderer` 专用路径，其他外部来源会经过 `render_preview`/`drawImage`；外部 PDF 的该 Composer 路径可能成为位图。与此同时，`ExternalImagePanel.render_export` 可以返回原始 SVG/PDF 路径。因此，矢量性必须针对具体来源和导出入口判断。[Composer 绘制路径](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/ui/widgets/composer_canvas.py)；[外部面板](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/models/external_image_panel.py)。

新 [storage.py](../workbench/storage.py) 对原件采用完整 SHA-256 身份，便携工程包含全部已导入来源。新 [exporting.py](../workbench/exporting.py) 在 PDF/SVG 路径保留适用的源矢量内容，并把本应用文字、线条与图像分别组织；PNG/TIFF 是最终显示成图。PPTX 的本应用文字和基础形状可编辑，源 PDF/SVG 面板在 PPTX 路径会栅格化，不能宣称任意导入图表都成为 PowerPoint 原生对象。[导出回归测试](../tests/test_exports.py)。

## 5. 新运行时、数据沿用与许可归属

本项目重新实现了浏览器界面、凝胶定位 Worker、Python 分析核心、原件存储、异步任务、预检、导出和迁移解析器。运行时不导入两套上游应用的 JavaScript 引擎或 PyQt 模块，也没有复制 ScieFig 的染色分离或 WB 算法模块。标准数值操作依赖 NumPy、SciPy、scikit-image 等第三方库；独立重写业务代码不等于宣称发明了这些标准算法。

明确沿用的是 **65 条 Ladder 事实目录数据：23 条 DNA、42 条蛋白**，位于 [web/data/ladders.json](../web/data/ladders.json)。数量以这个实际 JSON 为准，原 README 的文字计数可能滞后。型号、条带尺寸、参考带等来自 GelFigure 的目录，保留其 MIT 归属与 [GelFigure-MIT.txt](../licenses/GelFigure-MIT.txt)。来源目录见 [MARKER_DATA_SOURCES.md](MARKER_DATA_SOURCES.md)；该文件是上游出处资料，不应被理解为本次重新核验了每个厂商网页或批次。

新 [gel-engine.js](../web/gel-engine.js) 的投影、稳健阈值、网格拟合、Marker 候选匹配和单调对数插值代码重新编写。候选匹配只给出供用户确认的结果；相同条带规格无法凭图像确定厂商品牌。胶体系和预染蛋白的表观分子量仍需按实际产品说明复核。

项目代码按 [LICENSE](../LICENSE) 中的 AGPL-3.0-only 交付；上游归属说明、数据许可和字体许可位于 [licenses](../licenses)。字体、第三方库、模型权重与研究者原始资料各自的许可范围应分别保留。这里的代码来源描述不表示上游对本项目作了官方背书。

## 6. 当前覆盖与尚未覆盖的范围

| 工作流 | 本版实际覆盖 | 需要继续完善或人工复核的范围 |
|---|---|---|
| 凝胶定位与标定 | Worker 异步定位、条带、倾斜建议、Marker 候选、人工锚点校准；定位映射回原图像素 | 没有在广泛真实凝胶集合上证明优于上游；多曝光自动排序、复杂模板、自动标签避让与全部旧交互规则未等价复刻 |
| WB | 原始位深 ROI、三类背景、目标/内参、实验/重复/曝光身份、QC、真实组统计 | 实验独立性依赖用户录入；跨膜比较、曝光线性区间和归一化设计需要研究者判断 |
| qPCR | CSV/TSV/TXT/XLSX、常见中英文字段映射、仪器表头搜索、参考基因/对照、ΔΔCt、可选统计 | TXT 只接受结构明确的分隔表格；不承诺支持每一种仪器格式；采用标准等效率 ΔΔCt，未实现完整效率校正模型 |
| 多通道显示与分析 | 合并/单通道/分屏、跨来源与帧组合、伪彩/黑白点/gamma；原值 ROI 强度、背景 ROI/固定值、manual/Otsu/Triangle/Li、双线性线剖面、Jaccard/Dice/Pearson/Manders | 同尺寸不等于已经配准；没有自动光谱解混、上游全部显示增强、对象分类/复核或 adjusted 检测输入；详细定义与空数据行为见 0.2 审计 |
| 显微分割与染色 | 经典 CPU 核分割、官方 `cyto3`/`nuclei` 独立 CPU 推理、原图强度、H-DAB 非负分离、阳性面积与标定 | 上游自定义模型与 GPU、H&E/特殊染色/肌纤维/肌管专题、Macenko/GMM 和 IHC 示例校准/批次阈值未覆盖；当前 DAB 连通区域不冒充单细胞或 H-score |
| 组图与导出 | 毫米画布、面板移动/尺寸、基础标注、PDF/SVG/PNG/TIFF/PPTX、原件与工程包 | 富文本、任意形状、旧模块内部复杂排版及跨软件完全一致的文本编辑性仍有限 |
| 预检 | 来源、哈希、ROI、有效分辨率、饱和/显示裁切、字体及画布范围等规则检查 | 哈希证明保存字节的一致性；无法证明图像真实性、实验设计正确或期刊接受 |
| 迁移 | 支持明确可解析的 `.gelfig`/`.sfig` 子集，多源、毫米单位和基础标注 | 详细映射与降级规则见 [MIGRATION.md](MIGRATION.md)；不能称为无损迁移 |

对应实现与测试包括 [analysis.py](../workbench/analysis.py)、[qpcr.py](../workbench/qpcr.py)、[microscopy.py](../workbench/microscopy.py)、[microscopy_measurements.py](../workbench/microscopy_measurements.py)、[cellpose_runtime.py](../workbench/cellpose_runtime.py)、[preflight.py](../workbench/preflight.py)、[test_gel.cjs](../tests/test_gel.cjs)、[test_analysis.py](../tests/test_analysis.py)、[test_microscopy_measurements.py](../tests/test_microscopy_measurements.py)、[test_cellpose_runtime.py](../tests/test_cellpose_runtime.py) 以及 [test_migration.py](../tests/test_migration.py)。Windows 包包含独立运行时的构建与静态依赖检查；Linux 上通过的测试不能替代真实 Windows 安装、启动与使用验收。

## 7. 性能证据怎样解读

[benchmark_core.py](../benchmarks/benchmark_core.py) 使用可复现的合成 `uint16 4096×4096` 图像、24 个 `128×48` ROI，比较两种**相同 float64 边界中位数积分语义**的实现：每个 ROI 都转换整幅图像的朴素基线，与先取 ROI 再转换的新实现。它没有运行上游应用，也没有把 8-bit 与 16-bit 的不同计算语义拿来做速度排名。

本次记录的 [results_core.json](../benchmarks/results_core.json)：

| 检查 | 记录值 |
|---|---:|
| 新路径首次调用 | 12.22 ms |
| 新路径后续 5 次测量中位数 / P95 | 5.24 / 5.65 ms |
| 朴素整图重复转换基线中位数 | 779.30 ms |
| 同语义数值结果一致 | 是 |
| 源 dtype 与原始字节不变 | 是 |
| 另一个 `256×384`、半径 12 px rolling-ball 单次案例 | 137.42 ms |

该结果来自 Linux、Intel Xeon Platinum 8573C、Python 3.12.14 环境，具体依赖版本及采样值都在 JSON 中。它支持“减少无效整图转换能显著降低这一 ROI 工作负载的成本”，不支持“全面快于两套上游”“识别准确率更高”或行业速度排名。真实图像、硬件、ROI 密度、背景半径与解码方式均会改变时间。

多通道显示是另一项工作负载，不能套用上述约 5 ms 的 ROI 计时。[channels_results.json](../benchmarks/channels_results.json) 记录 4×2048² uint16 合成源：1024 像素合并预览的原平面缓存后渲染中位数约 225 ms，PNG 编码另计；原尺寸合并约 164 ms，四通道加合并图的原尺寸分屏约 500 ms。它们是后端渲染计时，未包含完整用户交互，亦未将相同配方的已编码预览缓存命中当成一次重新渲染。峰值 RSS 按 5 ms 间隔采样，可能漏过更短峰值；完整参数、逐次数据、原始字节验证和重跑脚本均随记录保留。
