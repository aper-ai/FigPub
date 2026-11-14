# 高级功能归属核验：Cellpose、流式门控与多通道编辑

## 0.3 更新

本文保留上游功能归属的固定提交证据。0.3 进一步补齐 H/DAB 高级 IHC、纤维/肌管测量、实例拆分合并复核、多曝光质量推荐，以及 WB 自动定位到可编辑 ROI 和完整图稿的链路。新的实现、测试和剩余边界见 [FEATURE_AUDIT_0.3.md](FEATURE_AUDIT_0.3.md)；下文 0.1/0.2 描述属于阶段历史。

## 核心结论

0.1 阶段曾把“Cellpose、完整流式细胞术门控、复杂多通道编辑”一并列为“本版未纳入”，这混合了已有能力未覆盖和新增产品方向两种情况。0.2 已新增真实 Cellpose CPU 推理及多通道显示、原图分析，下面区分上游归属、历史遗漏与现在的实现边界；详细差异及验证记录见 [FEATURE_AUDIT_0.2.md](FEATURE_AUDIT_0.2.md)。

| 用户问到的能力 | GelFigure | ScieFig-Community | Figure Workbench 本版的实际关系 |
|---|---|---|---|
| Cellpose 细胞/细胞核分割 | 本次源码未发现 | **已有真实后端、推理 worker、界面入口和测试**；运行需要相应独立环境和模型权重 | 0.1 未覆盖；0.2 重新实现独立 CPU worker，实际运行官方 `cyto3`、`nuclei`；不含上游全部专用模型、GPU 与专题流程 |
| 完整流式细胞术门控 | 本次源码未发现 | **没有找到 FCS 事件导入与流式门控链路**；已有图像对象的阈值分类，不应混称为完整流式系统 | 按当前源码证据属于新增扩展方向，不能列成已确认存在却未移植的上游功能 |
| 多通道显示、合并、分屏与逐通道设置 | 只有原色 RGB/灰度显示等凝胶展示功能，不提供这里所说的独立荧光通道工作台 | **已有实际实现与往返保存测试** | 0.1 仅基础通道选择；0.2 新增跨来源/帧通道显示、合并/分屏、逐通道色阶与 gamma、ROI 强度、四类阈值、剖面和重叠/相关；不等同上游全部显示增强、对象复核及专题工作流 |
| 通用图像编辑软件的全部高级编辑能力 | 没有据此认定 | 没有据此认定 | “多通道编辑”应指下面列明的具体科研显示/分析操作，不应泛化成任意图像编辑能力 |

核验限定于以下本地上游固定提交：

- GelFigure：`6fe1ff545e25eb6abcee6c490dba7e8678414a02`，仓库 [poplarity/GelFigure](https://github.com/poplarity/GelFigure/tree/6fe1ff545e25eb6abcee6c490dba7e8678414a02)。
- ScieFig-Community：`6cdaec4cf9ba91b75ceb431001ace93c4633e1cd`，仓库 [pamelah-233/ScieFig-Community](https://github.com/pamelah-233/ScieFig-Community/tree/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd)。

上游核验读取源代码、README、模块注册及测试文件，没有修改上游，也没有安装或运行上游的 GPU 工作流。0.2 的新实现另外完成了真实官方权重的 Linux CPU 合成图推理。能确认功能已经落地，不等于完成了每种模型在真实数据上的精度、速度或所有发行平台的验收。

## 1. Cellpose：ScieFig 已有，0.2 新实现官方模型的 CPU 工作流

### 确切实现证据

ScieFig 的 [README：明场细胞测量](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/README.md) 明确说明使用 Cellpose 分割细胞轮廓、计算个数与尺寸，并有局部对比增强策略。

[core/image_analysis/cellpose_backend.py](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/image_analysis/cellpose_backend.py) 中的 `run_cellpose_labels` 不是空接口：

- 检查二维单通道或 H×W×3 图像、数值有限性、模型、直径与最小面积参数。
- 接受 `cyto3`、`nuclei` 和若干应用专用模型名称。
- 将原数组以 `allow_pickle=False` 的 NumPy 文件传递给独立推理 worker。
- 将 CPU/GPU 选择、模型、直径、最小面积及推理参数发送给 worker。
- 读取真实实例标签与元数据，检查返回模型、设备和选项是否一致，并验证标签尺寸和整数范围。

[packaging/cellpose_worker.py](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/packaging/cellpose_worker.py) 的 `main` 实际导入 `torch` 与 `cellpose.models`，创建 `models.CellposeModel`，调用 `model.eval` 并保存实例标签。`select_device` 包含 CPU、CUDA 和 Apple MPS 选择；缺失所需权重或所选设备时有明确异常。

[ui/panels/fluorescence_workspace.py](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/ui/panels/fluorescence_workspace.py) 有 `cellpose_cyto3`、`cellpose_nuclei` 等算法选择；明场控制也位于 [ui/panels/placeholder_panel.py](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/ui/panels/placeholder_panel.py)。后一个文件名包含 `placeholder`，但其中这些控件和信号连接确实有实现，不能仅凭文件名认定功能是占位。

### 成熟度边界

[test_shared_cellpose_backend.py](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/tests/test_shared_cellpose_backend.py) 验证原始 uint16 信号、模型参数传递、错误标签、模型/设备不一致等契约；部分测试使用模拟 worker，不等于真实模型准确率评测。实际运行还依赖 ScieFig 的独立 Cellpose 环境和权重，`worker_command` 缺少该运行环境时会明确报错。

因此，正确描述是：**ScieFig 已经集成 Cellpose；Figure Workbench 0.1 没有覆盖这项既有能力，0.2 已用新写适配器和独立 worker 实现官方 `cyto3`、`nuclei` 的真实 CPU 推理。** 新代码不导入 ScieFig 的运行模块。权重按来源 URL 和 SHA-256 校验，缺少组件或模型时明确报错，不用经典分割假充模型结果。上游自定义肌纤维、肌管等模型以及 CUDA/MPS 工作流仍未覆盖。GelFigure 的主要模块和 README 未出现 Cellpose 分割工作流。

对应实现为 [cellpose_runtime.py](../workbench/cellpose_runtime.py)、[cellpose_worker.py](../workbench/cellpose_worker.py)，验证入口为 [test_cellpose_runtime.py](../tests/test_cellpose_runtime.py)。真实合成回归成功仅证明依赖、权重、计算与坐标链路可运行，不证明实验图像分割准确率。

## 2. 流式门控：没有找到完整 FCS 工作流，不能把阈值分型当成流式套件

### 检查到了什么

对两个固定提交的源码与说明进行 `FCS`、`flow cytometry`、`gating`、`流式`、`门控`、`logicle`、`biexponential`、`compensation`、`spillover`、`flowkit`、`flowio`、`fcsparser` 等检索，并检查实际模块枚举，未找到 FCS 事件读取、事件坐标变换、补偿、层级门控树及门控统计的完整实现。

ScieFig 的 [core/models/__init__.py](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/models/__init__.py) 注册了 qPCR、WB、荧光、IHC、明场、外部图像和衍生图等模型；[MODULE_PLACEHOLDERS](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/ui/panels/placeholder_panel.py) 的工作区枚举也对应这些图像/组版任务，没有流式模块。这个结论限于审计提交，不能推断其他未检查分支或未来发行版本永远不会提供此功能。

源码中的 `cellpose_flow_minimal.py` 或 `flow_threshold` 指 Cellpose 分割中的 flow/dynamics，不是流式细胞术。少数注释中的 `gate` 是信号过滤或界面条件判断，也不是 FCS 事件门控。

### ScieFig 已有的相关能力是什么

[core/image_analysis/object_phenotyping.py::classify_object_signal](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/image_analysis/object_phenotyping.py) 对已分割图像对象进行信号分类。输入是二维实例标签、同尺寸图像信号与固定阈值，可以选择对象区域或核周环；它计算背景校正后的均值并区分阴性、弱阳性、强阳性，同时保存排除与边界检查信息。

[core/measurement_relations.py::classify_measurement](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/measurement_relations.py) 关联已有分割对象与源测量；[MeasurementRelationsController](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/ui/controllers/measurement_relations_controller.py) 提供通道及阳性/强阳性阈值选择。对应的 [工作流测试](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/tests/test_measurement_relations_workflow.py) 包含分类、计数比例、过期结果和 `.sfig` 保存恢复验证。

这可以被描述为“基于图像对象的阈值分型”，或者在非常宽泛的数学意义上说是用阈值划分类别；它不是读取流式仪器事件的 FCS 门控工具。更不能据此认定已具备完整商业流式分析套件的门控、补偿、批量模板和工作区兼容能力。

### README 与当前源码证据也要区分

ScieFig [README 的 1.2.3 段落](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/README.md) 展示多标记组合、A→B 最近邻和半径邻域分析。当前固定源码中直接核验到的是上述单通道对象信号分类链路；检索未定位到 README 所描述的完整多标记组合与最近邻算法实现。因此应分别标为“README 宣称/展示的发行能力”和“此提交源代码直接确认的能力”，不把两种证据混为一谈。即使相关发行包有更丰富的组织图像表型功能，它也不能自动等同于 FCS 流式门控。

因此，正确描述是：**本版没有完整流式门控，但在审计的两个上游提交中也没有找到这项完整工作流；若要新增，属于额外产品扩展。**

## 3. 多通道显示与编辑：ScieFig 已有具体可用功能

以下均可在 [core/models/fluoro_panel.py](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/models/fluoro_panel.py) 直接核验：

| 函数 | 已有操作 | 实现边界 |
|---|---|---|
| `update_channel` | 通道名称、伪彩、显示最小/最大值、可见性、显示处理参数及审计记录 | 属于逐通道显示配置，不等于任意像素绘制 |
| `set_view_mode` | 合并、分屏、单通道 | 普通 RGB 合成图有专门限制，不能随意冒充独立实验通道 |
| `set_layout_mode` | 合并图、合并+分通道、批量网格、自定义布局 | 具体导出布局仍需按对应模型核验 |
| `merge_images_as_channels` | 将多张来源图像组合为通道，保留显示信息与源记录 | 要求图像宽高相同；不能据此宣称具备完整自动配准 |
| `segmentation_input_context` / `segmentation_input_data` | 明确选择 raw 或 adjusted 分割输入，记录使用的显示处理参数 | adjusted 输入是灰度处理数组，定量数据仍保留 raw；不是把屏幕叠加、文字或伪彩当原始测量 |
| `apply_threshold` / `measure_roi` / `compute_channel_overlap` | 阈值、ROI 测量及通道重叠等分析接口 | 不等于完整光谱解混或任意通道编辑系统 |

[core/plotting/fluoro_display.py](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/core/plotting/fluoro_display.py) 还实现黑白点、去噪、显示增强和背景处理；[ui/widgets/fluoro_channel_toolbar.py](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/ui/widgets/fluoro_channel_toolbar.py) 提供合并、分屏和逐通道选择。

[test_fluoro_merge_display.py](https://github.com/pamelah-233/ScieFig-Community/blob/6cdaec4cf9ba91b75ceb431001ace93c4633e1cd/tests/test_fluoro_merge_display.py) 验证合并后保留每个通道的伪彩、对比度和处理参数，保留原始信号，并测试 `.sfig` 往返与 OME-TIFF 导出。这说明它超出了“README 提到、界面尚未接通”的阶段；本次仍没有重新运行整套上游 GUI 与设备适配测试。

GelFigure 的 [js/colour.js](https://github.com/poplarity/GelFigure/blob/6fe1ff545e25eb6abcee6c490dba7e8678414a02/js/colour.js) 开头明确限定为保留原色显示，识别继续使用灰度路径，不做逐通道对比度、gamma、反相或人工伪彩。它的原色 RGB 展示与 ScieFig 的独立科研通道工作流应分别描述。

因此，正确描述是：**ScieFig 已有较丰富的多通道科研显示和分析配置；Figure Workbench 0.2 已重新实现主要显示、来源组合与数值分析链路，但不宣称完全等价。** 新实现保存 `asset_id/series/frame/channel`，逐通道显示和分析选择独立，采用原始值阈值、明确背景、线剖面及重叠/相关指标；详见 [microscopy_measurements.py](../workbench/microscopy_measurements.py) 与 [channel-view.js](../web/modules/channel-view.js)。上游 adjusted 分割输入、更多显示去噪/背景处理、对象级复核与专业组织流程不能由这些通用方法自动替代。“复杂多通道编辑”应具体指上表操作，不能泛化为任意通用图像编辑器的全部能力。

## 4. 对本版功能范围的准确说明

可以使用以下一句话描述现状：

> Figure Workbench 0.3 在既有重构基础上完成 WB 自动 ROI 与图组、H/DAB 高级 IHC、可复核的纤维/肌管测量、实例人工修订和多曝光质控。它没有附带上游全部专用模型或全部传统染色专题。完整 FCS 流式门控没有在审计的两套上游源码中找到，属于额外扩展方向。

本版是有明确边界的重构实现，不能称为两套上游全部功能的等价替代。已有且仍未覆盖的功能应优先按 [FEATURE_AUDIT_0.3.md](FEATURE_AUDIT_0.3.md) 的清单继续验证与实现；不能用“通用 Cellpose 已可运行”概括上游所有细胞/组织工作流已经完成。
