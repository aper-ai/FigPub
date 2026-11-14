# 0.2 既有功能补全、问题修正与剩余边界

本轮针对“补全已有功能，检查错误和遗漏”的要求，重点补齐多通道显示与原始值分析、真实 Cellpose CPU 推理，并核对 WB、qPCR 和传统染色工作流。**0.2 已提供可运行的新工作流，但尚不是两套上游所有功能的等价替代。** 本文把已经完成、此前表述需要更正、以及源代码已证实但仍未覆盖的功能分别记录。

## 1. 审计范围与证据

| 项目 | 固定提交 | 核验方式 |
|---|---|---|
| GelFigure / `poplarity/GelFigure` | `6fe1ff545e25eb6abcee6c490dba7e8678414a02` | 入口事件、识别引擎、显示模块、保存及导出代码 |
| ScieFig-Community / `pamelah-233/ScieFig-Community` | `6cdaec4cf9ba91b75ceb431001ace93c4633e1cd` | 面板注册、菜单/控制器、实际分析函数、模型 worker、保存和测试代码 |
| Figure Workbench | 本交付 0.2 源码 | 前端请求构建、API/任务分派、原始平面读取、数值测试及独立模型实际运行 |

ScieFig 的 [`panel_from_dict`][S1] 实际接入 WB、qPCR、fluorescence、brightfield、IHC、external image、derived plot；对应界面见 [`ui/main_window.py`][S2]。仅有 README 图片、类名或未接线函数，均不单独算作完成了可用工作流。当前审计没有重新运行整套上游 Qt GUI，也没有完成生物数据准确率或真实 Windows 机器的全流程验收。

业务代码、变量契约和计算逻辑在本项目重新编写；运行时不调用或直接复制两套上游的 JavaScript/PyQt 分析模块。明确沿用的上游数据仅为 **65 条 Ladder 事实目录**，保留 GelFigure 的 MIT 归属。NumPy/SciPy/scikit-image、Cellpose/Torch 及官方模型等是单独的第三方依赖，不能因此声称这些通用算法或权重由本项目原创。完整许可归属见 [UPSTREAM_COMPARISON.md](UPSTREAM_COMPARISON.md) 和随包许可文件。

## 2. 0.2 已补入的主要闭环

| 已确认的上游能力或实际缺口 | 0.2 新实现 | 可复核位置和边界 |
|---|---|---|
| ScieFig 多通道独立显示与组合 | 合并、单通道、分屏；逐通道名称、伪彩、可见性、黑白点、gamma；跨原件/系列/帧组合 | `web/modules/channel-*.js`、`channels.js`、`workbench/channels.py`；组合要求同尺寸，不能据此推断已做空间配准 |
| 原始通道与显示图混用风险 | 来源标识完整包含 `asset_id/series/frame/channel`；分析通道独立勾选，不因显示隐藏、换色或 gamma 改动而变化 | `storage.py::read_plane`、`jobs.py`、`microscopy_measurements.py::resolve_channels`；保留各自 dtype，不先堆叠到统一显示数组 |
| ScieFig 的 ROI 强度与逐通道背景 | 矩形、椭圆、多边形 ROI；逐通道原始均值/积分/范围，独立背景 ROI 或固定背景值；带符号扣背景值保留 | 新方法 `channel_intensity`；只有明确要求扣背景才执行，不把负结果截为 0 |
| ScieFig manual/Otsu/Triangle/Li 阈值 | 四种方法计算原始值阈值与阳性区域；结果保存阈值、有效像素、阳性面积及连通域 | 新方法 `threshold`；自动阈值在每个通道单独计算；“连通区域数”不称作已确认的细胞数 |
| ScieFig 线剖面 | 原图坐标双线性采样、有限像素线宽均值、px/可选 µm 距离 | 新方法 `line_profile`；ROI 和背景参数不参与剖面，越界宽度样本不补假 0 |
| ScieFig 二值通道重叠 | 阈值交并面积、Jaccard/Dice、各方向重叠比例，并增加定义明确的原值 Pearson/Manders | 新方法 `channel_overlap`；仅描述像素关联，不据此宣称分子相互作用或生成像素独立样本显著性 |
| ScieFig 真实 Cellpose | 独立 CPU 组件、官方 `cyto3`/`nuclei`、模型身份校验、原图实例测量和轮廓 | `cellpose_runtime.py` / `cellpose_worker.py`；实际加载权重并调用 `CellposeModel.eval`，不是阈值算法替代品 |
| Cellpose 组件状态与取消 | 界面查询实际依赖/模型状态；任务取消会终止独立进程；超时亦终止；缺权重明确失败 | `cellpose_status`、`_run_worker`、`jobs.py::_run`；依赖加载、初始化与推理均在可取消的同一个推理进程中 |
| WB/qPCR 的操作闭环 | UI 接入明确对照组、统计选择；qPCR 工作表/样本分组/孔排除/技术重复 QC；生物学重复聚合后比较 | `analysis-domain.js`、`analysis-forms.js`、`analysis.py`、`qpcr.py`、`statistics.py`；没有实验独立性信息时不能自行制造 n |

WB/qPCR 当前可选检验包括 Welch t、等方差 Student t、按生物学 ID 配对的 t、ANOVA + Tukey；多项对照比较记录 Holm 校正。结果包含检验适用性、样本量和不可计算原因。它们并不覆盖上游 `core/statistics.py` 中所有非参数检验，剩余项见第 6 节。

## 3. 本轮发现并修正的问题

### 来源、几何和显示边界

- **不能只用 asset ID 作为多通道字典键。** 同一 TIFF 的不同系列、帧和通道可能来自同一个文件。后端现在按稳定 channel ID 提交独立原始 YX 平面，科学参数保留完整来源。经典单通道接口也不会把内部提取后的 `channel=0` 写成原始 C0。
- **小尺寸 IHC 图像的轴歧义。** 明确声明 `channel_axis=-1` 时，若末轴不是 RGB/RGBA 的 3/4 个样本，必须失败，不能因为第一维恰为 3/4 就把空间轴当颜色轴。未声明轴时，只接受无歧义的 CYX/YXC；API 同时要求真实 RGB 平面，伪彩通道组合不作为 IHC 输入。
- **非矩形 ROI 的真实边界。** 椭圆与多边形按原图像素中心判定；排除边界对象使用形状边缘，不只检查外接矩形。分数像素椭圆几何完整保存，重放时不改成取整后的另一个椭圆。
- **计数不依赖是否绘制轮廓。** 关闭轮廓或限制输出对象详情数量，不改变实际总连通域/实例计数；输出轮廓和质心加回原图 ROI 偏移。
- **空数据不是 0 的替身。** NaN/Inf、恒定图像、空阈值掩膜、零方差、零分母分别产生明确的 `null`、状态和警告；没有把不可计算相关性显示为 0，也没有虚构 SD。

### 原始多维读取、显示与资源边界

`planes.py` 与 `storage.read_plane` 明确区分系列、非空间帧轴、逻辑通道 C 与样本分量 S；同一来源的 T/Z 或 C×S 不再靠数组长度猜测。未压缩、Deflate/LZW、多系列 TIFF、16 位 RGB PNG、混合位深多原件和完整 `.figwork` 往返见 [test_channels.py](../tests/test_channels.py)。普通 RGB 分量不是自动识别出的染料，Alpha 默认不参与显示。当前每个配方最多 32 个通道，界面默认列出原系列的前 32 个，额外通道不会从原件中删除。

显示的默认范围取全幅原值后再裁切；大图百分位初始化使用确定性有限采样，并返回 `sampled/sample_count/sample_stride`。单通道查看不受合并显隐开关限制。分屏加隐藏泳道明确拒绝；分屏导出按独立原像素块放置，预检按各块最终毫米尺寸计算有效 PPI。原图分析始终绕过显示 LUT。

多通道实际性能记录在 [channels_results.json](../benchmarks/channels_results.json)，生成器为 [bench_channels.py](../benchmarks/bench_channels.py)。该记录仅覆盖 Linux 上 4×2048²、uint16 合成 TIFF；区分空应用缓存与后续原平面缓存、渲染与 PNG 编码，并采样 RSS。它没有清除操作系统文件缓存，也没有测量 Windows 或上游程序。逐通道/分块计算和有界缓存减少临时副本，但原尺寸分屏与最终导出仍需与像素数相应的内存，不能称为恒定内存或任意大图实时渲染。

### 模型运行与交互契约

- 独立 Python 路径保留虚拟环境入口，不解析成基础解释器符号链接；子进程移除继承的 `PYTHONPATH/PYTHONHOME`，防止核心 NumPy 与模型 NumPy ABI 混用。
- `cellpose_status(probe=False)` 使用已缓存的真实探测结果；尚未探测时明确 `not_probed`，不会永远误报不可用，也不会在普通 health 请求中启动长时间模型依赖加载。
- 实际推理不先启动无法响应任务取消的额外同步探测；模型导入、初始化、计算都由可终止进程完成。取消后的结果不提交到项目。
- Windows 组件不要求 TBB：worker 在导入 Numba 前固定 `NUMBA_THREADING_LAYER=workqueue`，并限制 `NUMBA_NUM_THREADS`、OMP、MKL 与 Torch CPU 线程。实际模型结果记录线程层与线程数。
- 前端与核心统一线宽、采样点数、Cellpose 直径和阈值范围。方法参数保存在完成的测量记录中，结果查找按科学参数签名判定是否过期；显示配色变动不自动成为新的科学分析。

有关前端/后端的完整回归数与交付验证，以 [VALIDATION.md](VALIDATION.md) 为准；本文件中的模型测试只针对下述范围。

## 4. 多通道与 Cellpose 参数/返回值概览

统一调用仍为 `analyze_microscopy(image, params, cancel_event=...)`。`cancel_event` 属于运行控制，不写入科学参数 JSON。多通道 `image` 是 `{channel_id: 原始只读 YX 数组}`；持久化请求只存来源身份，不把像素数组塞进 JSON。

```json
{
  "method": "channel_intensity",
  "channels": [
    {"id": "c0", "name": "DAPI", "source": {"asset_id": "原件 SHA-256", "series": 0, "frame": 2, "channel": 0}},
    {"id": "c1", "name": "标记", "source": {"asset_id": "另一原件 SHA-256", "series": 0, "frame": 0, "channel": 1}}
  ],
  "roi": {"type": "ellipse", "x": 10, "y": 20, "width": 80, "height": 60},
  "background_method": "fixed",
  "background_values": {"c0": 120, "c1": 50},
  "um_per_px": 0.5
}
```

上述数值仅演示请求结构，不是实验结果。所有通道必须具有相同原始宽高，最多 32 个；它们是否已经物理配准由数据准备过程决定。`frame` 按存储层声明的非空间/非通道轴展平，具体坐标来自来源 descriptor，不凭文件名猜测。

| `method` | 主要参数 | 返回的科学数据 |
|---|---|---|
| `channel_intensity` | `channels`、可选 `roi`；`background_method=none/roi/fixed`；`background_roi` 或 `background_values` | `rows` 每通道的 `source/source_dtype/source_bit_depth`、有效像素、原始均值/积分/范围、背景均值与有符号校正值 |
| `threshold` | `threshold_method=manual/otsu/triangle/li`；manual 可用公共 `threshold`，界面支持逐通道值并提交 `thresholds[id]={method,value}` | `rows` 增加实际阈值、阳性面积/比例；`objects` 保存连通区域、面积、原图坐标及轮廓；`count` 是连通区域总数 |
| `line_profile` | `line={x1,y1,x2,y2}`、`samples`、`line_width_px`；不使用区域/背景 | `profiles[]` 中的 `values/distance_px/distance_um/valid_width_samples`；同时有便于 CSV 导出的逐点 `rows` |
| `channel_overlap` | 至少 2 个所选通道、可选 ROI、逐通道原始值阈值 | `pairs[]` 保存阈值、共同有效像素、各通道阳性面积、交并面积及相关指标；常量/空值有状态与备注 |
| `cellpose` | 1 个原始通道；`model=cyto3/nuclei`；`diameter_px`、`flow_threshold`、`cellprob_threshold`、`min_area`；可选 ROI/标定 | `objects/rows` 为模型候选实例；`summary` 为数量/面积/原始强度；`method.source_channel` 和 `method.model_record` 保存来源、版本、模型哈希与实际参数 |

公共结果包含 `kind="microscopy"`、`warnings`、`method`、`elapsed_ms`；`method.measurement_basis="original_pixels"`、`display_lut_used=false`。Cellpose 可对检测输入进行明确记录的 1–99% 归一化；非矩形 ROI 外部和非有限像素仅为检测填充中位数，最终标签和强度计算再排除这些区域。原件及定量像素不因此改变。

### 指标定义与无数据行为

阈值阳性严格定义为 **原始强度 > 实际阈值**。阳性比例以该通道 ROI 中有限像素数为分母。Otsu/Triangle 默认 4096 个直方图 bin，结果记录实际阈值；常量图像不强行拆出两类。界面的数值格式化不改变保存的 float64 结果。

重叠在两个通道共同有限的 ROI 像素上计算。令阳性集合分别为 A、B，Jaccard 为交集/并集，Dice 为 `2×交集/(|A|+|B|)`，方向比例分别为交集/各自阳性面积。原值 Pearson 采用中心化相关；原值 Manders M1 为 `sum(A 强度，其中 B > 阈值 B)/sum(A 强度)`，M2 交换 A/B；Manders overlap coefficient 为未中心化点积除以两个 L2 范数。Manders 要求非负原始强度，负值/零分母时为 `null`。双空阳性集合的 Jaccard/Dice 也为 `null`，不记为“完全共定位”。这些定义同时写入结果 `method.metrics`。

线剖面端点使用原图像素中心坐标，范围为 `[0, width−1] × [0, height−1]`；核心采样点数 2–10000，线宽 1–101 px，界面采样上限为 4096。区域强度与背景分别取各自 ROI 的有限像素均值，校正积分为原始积分减去背景均值乘有效像素数；不会悄悄改成局部平滑或显示黑平衡。

## 5. 官方模型来源、实际运行与可复现性

使用 Cellpose **3.1.1.3**，隔离组件为 Torch **2.7.1+cpu**、NumPy **2.0.2**；此次实测还包含 SciPy 1.18.1、Numba 0.68.1、llvmlite 0.50.0、OpenCV headless 5.0.0.93。精确运行版本保存在结果 metadata；核心应用不导入 Torch。

| 模型 | 官方下载地址 | 字节数 | SHA-256 |
|---|---|---:|---|
| `cyto3` | <https://www.cellpose.org/models/cyto3> | 26,566,255 | `2dc3087a8abd7da46d1ab0ddd5824639933cc3ff63b382af3fa1939a392db93c` |
| `nuclei` / `nucleitorch_0` | <https://www.cellpose.org/models/nucleitorch_0> | 26,563,614 | `89ca45e4a45048d5010d29621466b1274b91b3dcb9714dce9f9e90a9a8671303` |

原始下载记录、字节哈希、官方 Cellpose BSD 许可随模型组件保留；请求不能指定任意可执行程序或任意权重路径。worker 每次实际推理再次核对模型长度和哈希，不在分析时自动下载模型。上游 ScieFig 的专用模型权重没有冒用这些通用模型名称。

### 实际 CPU 回归

可复现生成器在 [smoke_cellpose_cpu.py](../benchmarks/smoke_cellpose_cpu.py)，结果在 [cellpose_cpu_smoke.json](../benchmarks/cellpose_cpu_smoke.json)。它生成 160×192、uint16、六个高斯亮斑的明确合成测试图，原始数组 SHA-256 为 `c7741268774828a8f51d95ee378f83d1b01a38189a06a98163f8777a0b01df6c`。测试实际启动独立进程、导入 Torch/Cellpose、加载上述官方权重并运行 CPU 网络。

| 模型 | 实际返回候选数 | 前景面积 / px² | 本次进程内推理耗时 | 含进程启动的墙钟耗时 | 原始数组保持不变 |
|---|---:|---:|---:|---:|---|
| nuclei | 6 | 2,175 | 1,068.88 ms | 8,431.14 ms | 是 |
| cyto3 | 6 | 7,689 | 1,787.74 ms | 8,453.95 ms | 是 |

两次记录的实际 Numba 线程层均为 `workqueue`、线程数为 2，输出质心均位于原图边界内。模型测试与多通道数值回归位于 [test_cellpose_runtime.py](../tests/test_cellpose_runtime.py)、[test_microscopy_measurements.py](../tests/test_microscopy_measurements.py)，本次启用真实官方模型的最终运行是 **37 passed / 9.00 s**，无跳过；仅有环境自带 anyio pytest 插件的弃用提示。它们包括真实进程取消、超时、取消前不启动同步探测、缺模型、不符哈希、16 位原值、混合 dtype、几何偏移、IHC 轴歧义和无效数据。

**这只是合成回归，不是生物学准确率验证。** 返回 6 个候选不能当作细胞真实计数的准确率证据；两个模型面积不同也不能据此评判哪个更准确。耗时来自 Linux 单机、单幅小图的一次记录，包含缓存和启动影响，不是 Windows 耗时保证或与上游的性能排名。真实 Windows 组件的组装/静态依赖检查和实际机器运行验证应分别记录。

## 6. 已有且仍未覆盖的上游能力

下列条目有实际源码证据，应作为后续补全范围，而不是说“两套上游也没有”。优先级表示后续建议，不代表本交付已经实现。

| 优先级 | 已证实的上游能力 | 固定提交证据 | 0.2 的准确边界 |
|---|---|---|---|
| P1 | IHC 自动染色向量、GMM 强度分级及其 `h_score` 字段 | [`stain_engine.py::estimate_stain_vectors_macenko/deconvolve_h_dab/classify_dab_gmm`][S3]，[`IHCPanel._analyze_dab_threshold`][S4] | 新实现为固定/用户指定 H-DAB 向量的非负分离和组织阳性面积。未实现 Macenko/GMM；上游该分级以组织像素强度为基础，不能冒称逐细胞临床评分 |
| P1 | IHC 引导式组织/背景/伪影处理、阳阴示例重新校准、批次阈值锁定与恢复 | [`IHCPanel.auto_analyze_dab/recalibrate_dab_from_examples/revert_dab_to_auto`][S4]、[`ihc_analysis_controller.py`][S5] | 当前可用手动/Otsu DAB 阈值与 ROI；没有等价的示例校准、批次控制和复核状态机 |
| P1 | 明场 H&E、特殊染色、结晶紫、Oil Red O 的任务预设与辅助分析 | [`BRIGHTFIELD_STAIN_TASK_IDS`、`create_measurement/create_profile_preset_measurement/auto_analyze_he`][S6] | 通用强度、核分割或 Cellpose 不等价于这些染色量化规则；本版没有这些独立专题工作台 |
| P1 | 图像对象信号分型、核周环、排除/复核关联 | [`object_phenotyping.py::classify_object_signal`][S7]、[`measurement_relations_controller.py`][S8] | 新增像素重叠/相关和实例列表，不含对象与其它测量的分类关联、核周环统计或逐对象编辑复核闭环 |
| P1 | GelFigure 多曝光切换和质量推荐、自动倾斜校正、完整凝胶样式模板 | [`app.js::loadMany/autoDeskew`][G1]、[`engine.js::exposureScore/deskew`][G2] | 新版已有独立源/曝光身份、定位倾斜建议和基础排版；没有完全复现多曝光推荐、自动校正交互及丰富模板。倾斜建议值不等于已经应用旋转 |
| P2 | 肌纤维面积、中央/周边核、肌管及专用模型流程 | [`brightfield_panel.py`][S6]、[`muscle_nuclei.py`][S9]、[`myotube_controller.py`][S10]、[`cellpose_backend.py`][S11] | 官方 cyto3/nuclei 只覆盖通用 CPU 推理；没有训练或打包上游专用模型，也没有肌纤维/肌管的整套复核、分类与导出 |
| P2 | 上游通道显示去噪/局部增强、raw/adjusted 检测选择、OME-TIFF 写出 | [`fluoro_panel.py::segmentation_input_data/save_image_data_as_ome_tiff`][S12]、[`fluoro_display.py`][S13] | 现有黑白点/gamma/伪彩和原值分析；不宣称支持所有处理选项或完整 OME-TIFF 输出工作流。原件下载仍保留原 OME 字节 |
| P2 | Mann–Whitney、Wilcoxon、Kruskal–Wallis 等上游非参数比较 | [`core/statistics.py`][S14] | 新共享统计目前提供参数检验子集；缺失检验不能被悄悄替换为 t 检验 |
| P2 | qPCR 旧式 XLS/更广泛仪器方言与原有部分自动分组规则 | [`qpcr_parser.py`][S15]、[`qpcr_calculator.py`][S16] | 当前支持 CSV/TSV/TXT/XLSX、表头搜索与显式分组；TXT 需结构明确的逗号/Tab/分号多列表格，不能承诺任意仪器导出或旧二进制 XLS 都可直接读取 |

这些差异之外，旧工程迁移仍是明确子集：原始来源、可解析几何和单位优先保留，无法恢复的状态输出告警；旧结果不混入新计算结果。具体见 [MIGRATION.md](MIGRATION.md)。

## 7. 需要更正的旧表述与未证实的扩展

1. **“Cellpose 两个项目都没有”不正确。** ScieFig 有真正的后端、worker、界面与测试。0.1 的缺失是新实现遗漏，0.2 已补通用官方模型 CPU 路径；仍不是所有专用流程等价完成。
2. **“复杂多通道只是 README 展示”不正确。** ScieFig 有保存、合并、分屏、阈值、ROI、线剖面和重叠等实际代码。新版现在已有主要闭环，但不能把通用图像编辑器的任意功能都归入该名称。
3. **“完整流式门控是已有却未搬过来的能力”缺乏证据。** 两个固定提交没有找到 FCS 事件读取、补偿、logicle/biexponential 变换和层级门控工作区的完整链路。图像对象阈值分型、Cellpose 的 flow 参数都不是 FCS 门控。详见 [UPSTREAM_FEATURE_STATUS.md](UPSTREAM_FEATURE_STATUS.md)。
4. ScieFig README 展示多标记组合、最近邻/邻域分析；固定源码直接确认到的是单信号对象分型等子集，未定位 README 所述完整多标记/空间邻域实现。应分别记录发行说明与源码证据，不自行填补缺失实现。
5. 专门、可持久化的“局部放大框/Inset 联动”在本次 ScieFig/GelFigure 源码核验中未得到完整证据。普通画布缩放、裁切和复制面板不能自动称为该专门工作流；将来增加属于明确的新功能，不能无依据列成已确认的上游遗漏。

## 8. 复核与后续验收原则

首先使用用户实际原图核对源系列/帧/通道，再核对 ROI、物理标定、背景和阈值，最后复核实例轮廓与结果。若用多文件合并分析，先确认空间对应关系；软件拒绝不同尺寸，但相同尺寸仍可能未配准。Cellpose 候选应由研究者对照原图审核，不把单图对象数当生物学 n。比较实验组时保存样本/实验身份与排除依据，统计图读取完成的测量记录。

后续补全应为第 6 节每项分别规定输入、测量定义、复核/修改、保存重开、导出和真实数据验证标准。只有函数或按钮出现不能算完整交付；已经完成的通用 CPU、多通道和统计基础也不应继续描述成“完全没有”。

[S1]: https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/models/panel.py
[S2]: https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/ui/main_window.py
[S3]: https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/image_analysis/stain_engine.py
[S4]: https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/models/ihc_panel.py
[S5]: https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/ui/controllers/ihc_analysis_controller.py
[S6]: https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/models/brightfield_panel.py
[S7]: https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/image_analysis/object_phenotyping.py
[S8]: https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/ui/controllers/measurement_relations_controller.py
[S9]: https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/image_analysis/muscle_nuclei.py
[S10]: https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/ui/controllers/myotube_controller.py
[S11]: https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/image_analysis/cellpose_backend.py
[S12]: https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/models/fluoro_panel.py
[S13]: https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/plotting/fluoro_display.py
[S14]: https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/statistics.py
[S15]: https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/parsers/qpcr_parser.py
[S16]: https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/analysis/qpcr_calculator.py
[G1]: https://github.com/poplarity/GelFigure/blob/6fe1ff545e25eb6abcee6c490dba7e8678414a02/js/app.js
[G2]: https://github.com/poplarity/GelFigure/blob/6fe1ff545e25eb6abcee6c490dba7e8678414a02/js/engine.js
