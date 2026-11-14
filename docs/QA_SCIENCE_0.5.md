# 0.5 科学工作流修复与验证记录

记录日期：2026-11-12。状态：**开发验证记录，不是发版验收证书**。

本文记录本轮科学分析、数据导入、复核与结果状态修复，以及已经执行的验证。通过合成样本、已知数值数组和 API 回归，只能证明被覆盖条件下的行为。它们不能替代真实实验数据准确性评估、Windows EXE 验收或真实浏览器交互检查。

## 本轮发现并修复的缺陷

| 缺陷与可复现条件 | 修复后的行为 | 回归依据 |
| --- | --- | --- |
| 仪器导出表前有元数据行，例如重复的 `Sample,Sample,...`；真正的 `Well,Sample,Target,Ct,Group` 在后面。解析器提前把元数据当成歧义表头，拒绝整个文件。 | 只有满足 sample/target/ct 必需列的候选表头才检查别名冲突；真正的重复列仍报错，避免任意取一列。 | `tests/test_workflow_regressions_05.py`：元数据表头与真正歧义表头。 |
| CSV、TSV 和 XLSX 的资产导入器没有与 qPCR 解析器一致地寻找仪器表头；XLSX 活动页为说明页时，上传阶段就失败或预览错误工作表，后续分析进不去。 | 文本表格寻找前 100 行内的规范 qPCR 表头；支持 BOM 标记的 UTF-16 CSV/TSV/TXT。XLSX 根据规范列和 Results 工作表名称选择 qPCR 页。没有 qPCR 候选时保留普通表格/活动页行为。 | 五种格式实际 `/api/assets` 上传、预览、`/api/jobs`、轮询和工程保存回归；另测普通 CSV/XLSX。 |
| qPCR 切换原始表或工作表时沿用全局分组、排除行号与内参设置；选到不存在/无 Ct 的工作表后，失败界面还隐藏了恢复选择器。 | 完整 qPCR 配方按原始文件 ID 与工作表独立保存；旧工程全局配方仅迁移到旧当前表。失败选择器可切换或自动恢复，撤销/重做同步预览。 | `test_qpcr_workspace_flow.mjs` 通过生产 UI 和真实 ASGI 完成两个 XLSX 的已知 2×/4×、n=3、排孔与分组隔离、多工作表、错误恢复、归档重开；另有五个状态边界回归。 |
| WB 的样本、实验、生物重复与技术重复标识相同，但组别不同，目标条带可能除以另一组的内参。 | 内参匹配身份包含组别；显式指向跨组内参也拒绝归一化。同名样本在不同组独立计算。 | 自动与显式跨组匹配均拒绝；各组合法目标/内参均得到预期比值 2。 |
| 用户已经排除重复 qPCR 孔或重复 WB ROI，但保留的那一条仍被重复检查排除，无法恢复分析。 | 重复身份检查只把用户明确纳入的记录视为冲突；排除记录与原始测量继续保留。仍纳入的两个重复不能增加样本量。 | qPCR `included=false` 和 `excluded_source_rows` 两条纠错路径；WB 重复原像素 ROI。 |
| 曝光质量复核中同样存在“排除重复 ROI 后，保留项仍无效”的问题。 | 被明确排除的副本不再使保留项无效；副本仍显示排除原因，不进入曝光选择决定。 | `tests/test_wb_workflow.py` 中重复源像素、明确排除后恢复推荐。 |
| 多实验统计图的标签只有蛋白/基因和组别，无法区分实验；存在归一化和未归一化指标时共用一个轴。 | 多实验图表标签包含实验 ID；同一图中出现不同计量方式时拒绝作图并提示修正对照设计。实际观测值和各实验仍分别保留。 | `tests/test_workflow_regressions_05.py`：实验标签区分、SVG 元数据原值和混合单位拒绝。 |
| 取消正在运行的分析立即从队列容量计数中消失，但底层工作尚未结束。反复取消/提交可以绕过实际资源上限。 | 容量按尚未释放的任务计数。排队中的 Future 实际取消；运行中的任务保留槽位直到退出，返回 `cleanup_pending`。取消任务不写入结果。 | `tests/test_analysis_job_limits_05.py`：阻塞运行任务、排队取消、替换任务、无结果提交。 |
| 多平面分析缺少统一累计读取预算，取消后仍可能继续解码后续通道。 | 普通分析累计原始平面预算为 512 MiB，WB 最多 32 个平面；读取各平面前后检查取消，WB ROI 与 qPCR 记录处理增加检查点。 | 累计读取预算、取消后不再读取下一通道、预先取消不进入计算。 |
| 工程恢复了人工实例修订配方，但服务器重放尚未成功时，“下载复核后 CSV”可以导出原自动分割结果。 | 复核未恢复完成前显示明确状态并阻止编辑/导出；失败后可重试，成功后导出重建结果，原自动结果仍保持不变。 | `tests/test_review_workflows_05.mjs`：首次渲染、请求等待、重放失败、重试成功和下载内容。 |
| 无效实例复核配方修正后，客户端 `invalid` 标志未清除，工作台必须重载才能恢复。 | 验证通过后恢复可用状态；无效记录继续拒绝。 | `tests/test_review_workflows_05.mjs`：修正来源哈希并恢复合法配方。 |
| 实例复核等待保存或计算时，用户做全局撤销；自动保存还未更新工程 revision，迟到的复核结果会覆盖已经撤销的配方。 | 请求同时绑定发起时的复核配方签名；保存后、响应应用前分别核对，同一 revision 内发生的本地修订也会使旧请求失效。保留用户最新配方和原始自动分割。 | `tests/test_review_workflows_05.mjs`：分别在保存等待、服务计算期间撤销，确认旧配方不被写回，原自动分割哈希不变。 |
| 点击实例复核的「取消等待」后，请求被取消，但下一次渲染立即自动重新发起恢复，取消实际无效。 | 取消后明确暂停恢复并显示重试入口；只有用户重试才继续。保存等待期间取消也不会随后提交计算请求；保存的修订不被删除。 | `tests/test_review_workflows_05.mjs`：取消后多次渲染不再请求、手动重试成功，以及保存阶段取消不发请求。 |
| 曝光计算期间用户修改样本信息或 ADC，旧响应返回后覆盖新编辑，提供与当前输入不符的推荐；已完成推荐也未检查后来改变的 ADC。 | 请求绑定到模态框输入和工程中测量/探测器参数签名；变化后保留编辑并要求重算，拒绝应用旧推荐。 | 同时覆盖样本字段、ADC 字段和完成后改变 ADC 的回归。 |
| 曝光 API 允许 10,000 个 ROI，科学引擎最多接受 5,000 个，可能先解码原图再报上限错误。 | API 在读取来源前按相同的 5,000 个 ROI 上限拒绝。 | `tests/test_workflow_api.py`：5,001 个 ROI 请求在来源解析前报错。 |
| 普通 WB 异步任务入口仍允许 10,000 个 ROI，科学引擎最多接受 5,000 个；超限任务可能先占用队列并解码原图，最后才失败。 | 普通 WB 提交入口同样在排队和读取原图前按 5,000 个 ROI 上限拒绝，不生成任务记录或测量结果。 | `tests/test_analysis_job_limits_05.py`：5,001 和 10,000 个 ROI 提交均不进入执行器/解码器；5,000 个 ROI 的入队边界由受控 runner 验证，不将其当作大规模数值或性能验收。 |

这些修复没有将缺失的独立样本身份、组别、控制样本或探测器范围自动猜成有效科学数据。原图、原表、自动分割基础结果以及用户明确排除的记录都继续保留。

## 数值与工作流覆盖

下表列的是已经执行并通过的测试范围，不代表任意输入和任意仪器都已经被验证。

| 范围 | 已验证方法和边界 | 测试文件 |
| --- | --- | --- |
| WB 原始定量 | uint16 相邻一级信号差保留；显示 LUT 不影响计量；有符号背景扣除；边框/rolling-ball 背景；原始探测器饱和；同源像素重复检查；零内参、自内参和跨身份内参拒绝；技术重复先汇总为生物重复；曝光不当作独立 n。 | `test_analysis.py`、`test_workflow_regressions_05.py` |
| WB 自动定位到组图领域逻辑 | 定位坐标转换为原始像素 ROI；检测缺失泳道明确标识参考窗口；真实样本身份必填；目标/内参重叠与出界拒绝；跨文件匹配；连续原图横条保留中间泳道；完整图组几何与来源信息。 | `test_wb_workflow.mjs` |
| WB 多曝光 | 完整实验系列统一选择；饱和和低 SNR 候选拒绝；同实验全部样本/目标/内参完整性；不同实验分开；ADC 与存储位深区分；ROI 面积不一致拒绝；跨文件明确配对；应用后生物重复不增加。 | `test_wb_workflow.py`、`test_workflow_api.py`、`test_review_workflows_05.mjs` |
| qPCR | 已知 Ct 数据：ΔCt / ΔΔCt / 2^-ΔΔCt；技术孔不增加生物重复 n；内参按真实样本匹配；未检出、重复孔、文本 false、明确排除；缺失分组不猜测；实验与组别隔离。新增仪器导入案例中 Control fold=1、Treatment fold=2。 | `test_analysis.py`、`test_statistics_extended.py`、`test_workflow_regressions_05.py`、`test_api.py` |
| 统计与统计图 | Welch、Student、配对 t、ANOVA/Tukey；配对按身份而非行序；多重比较家族/校正；n=1 或退化数据不制造 p 值；实际观测点与精度保存；没有观测值时不凭均值造图；中文字体轮廓和 SVG 原值来源。 | `test_statistics_extended.py`、`test_charts.py`、`test_workflow_regressions_05.py` |
| 多通道与显微原值 | series/frame/channel 身份；不同 dtype；多通道同尺寸要求；显示名称、伪彩、范围、gamma、可见性不改变计量；原始阈值；线剖面与物理长度；椭圆/多边形真实掩模；Pearson/Manders 定义、共同有效像素域与零分母；分屏导出尺寸、翻转/旋转和来源。 | `test_channels.py`、`test_channel_integration.py`、`test_microscopy_measurements.py` |
| IHC | 构造光密度的非负染色分离；Macenko 已知端点方向、固定抽样、组织不足/秩不足拒绝；手动分级与像素加权指数；GMM 四组数据/不足四组拒绝；示例校准、伪影排除、透明背景；锁定校准复用；真 RGB 与伪彩荧光区分。 | `test_histology.py`、`test_workflow_api.py`、`test_histology_domain.mjs` |
| 纤维与肌管 | 已知实例标签保持 ID/坐标/dtype；像素方形最小 Feret 与各向异性物理尺寸；旋转卡壳与暴力凸包比较；中央/周边/外部及歧义核归属；融合指数分母含外部核；分型基于原始均值；阈值/膜闭合；不重复计数的骨架角点；人工拆分后指标重算。 | `test_fibers.py`、`test_instances.py`、`test_histology_domain.mjs` |
| 实例拆分合并 | 标签无损压缩；稀疏 ID/孔洞/ROI 原点；两种子拆分保留前景和原始积分；合并、删除、补画、撤销/重做；不重用已消耗 ID；不相邻合并必须明确选择；旧配方、损坏/超限掩模和工程修订冲突拒绝。 | `test_instances.py`、`test_workflow_api.py`、`test_instance_review.mjs`、`test_review_workflows_05.mjs` |
| 工程保存与异步结果 | 分析提交/完成/保存；项目修订改变后结果不自动附着；取消不提交；复核 API 不改原测量；原图字节和 uint16 数值往返；手工实例配方随工程备份、重开后重放；来源哈希损坏/丢失和非法引用拒绝。 | `test_storage.py`、`test_api.py`、`test_workflow_api.py`、`test_channel_integration.py` |
| Cellpose 运行边界 | 模型缺失/哈希不符明确不可用；参数拒绝；隔离环境；真实子进程取消和超时终止；后处理保持原始数值/坐标/ROI。此处后处理用固定标签，不能证明模型分割精度。 | `test_cellpose_runtime.py`；真实官方模型推理用例本次跳过。 |

## 已执行命令与结果

环境：当前 Linux 工作区的项目 Python 虚拟环境，以及当前 Node 运行时。API 测试通过 FastAPI/ASGI 在进程内请求实际服务处理器，不启动可视浏览器。

```bash
.venv/bin/python -m pytest -q \
  tests/test_analysis.py tests/test_statistics_extended.py tests/test_charts.py \
  tests/test_wb_workflow.py tests/test_workflow_api.py tests/test_histology.py \
  tests/test_fibers.py tests/test_instances.py tests/test_microscopy_measurements.py \
  tests/test_channels.py tests/test_channel_integration.py tests/test_cellpose_runtime.py \
  tests/test_storage.py tests/test_api.py tests/test_workflow_regressions_05.py \
  tests/test_analysis_job_limits_05.py
```

科学修复初轮执行结果（下述独立 QA 增补之前）：**273 passed，2 skipped，1 warning，16.23 s**。

- 跳过：`test_real_official_model_inference_preserves_input` 的 `nuclei / 17 px` 与 `cyto3 / 30 px` 两个参数化用例。它们分别验证模型、权重文件/来源 URL/SHA-256、直径、CPU 运行元数据、原尺寸标签和原图不变。当前没有安装单独的官方 Cellpose CPU 模型组件，也没有启用 `FIGURE_WORKBENCH_TEST_CELLPOSE=1`，不能把跳过记成模型推理通过。`scripts/prepare_cellpose_acceptance.py` 可供明确的 CI/开发验收准备隔离环境与校验权重；应用本身不会自动下载。
- 警告：Starlette 关于 TestClient 使用 httpx 的后续弃用提示；本轮测试没有失败。该提示不是图像分析精度结论。

```bash
node --test tests/test_review_workflows_05.mjs \
  tests/test_instance_review.mjs tests/test_wb_workflow.mjs \
  tests/test_histology_domain.mjs

node_modules/.bin/oxlint web/modules/wb-workflow.js \
  web/modules/instance-review.js tests/test_review_workflows_05.mjs

git diff --check
```

初轮结果：**32 项前端科学/复核测试通过，无跳过**；指定文件 oxlint 及差异空白检查通过。新异步用例通过实际事件处理器和 Happy DOM 重现状态变化；不是截图、布局或鼠标操作验收。

新增 Python 回归集中于 `tests/test_workflow_regressions_05.py` 和 `tests/test_analysis_job_limits_05.py`。同时扩展已有曝光科学/API 回归。这个记录只统计上面明确列出的命令；整个应用其他 UI、桌面启动、导出、期刊模板等验收由总 QA 清单记录。

### 独立 QA 增补

独立检查额外复现了实例复核的撤销竞态、取消后自动重试，以及普通 WB 任务入口的 ROI 上限不一致；修复和回归见上表。没有把新增检查计入初轮已记录的数字。

```bash
node --test tests/test_instance_review.mjs tests/test_review_workflows_05.mjs \
  tests/test_workflow_state.mjs tests/test_wb_workflow.mjs tests/test_analysis_state.mjs

.venv/bin/python -m pytest -q tests/test_analysis_job_limits_05.py
```

对应结果：前端 **41 passed，无失败或跳过**；WB 任务上限和取消资源测试 **9 passed，0.63 s**。复核专项中的 9 项测试包括首次恢复、失败重试、取消暂停、保存等待期间取消、保存/计算期间的本地撤销和曝光旧响应拦截。修改文件的 oxlint 与差异空白检查通过。此处依然是实际事件处理器、领域逻辑和任务管理器验证，Windows 与可视浏览器验收另行执行。

## 后续官方模型实际执行

初轮模型跳过之后，本轮在 Linux 新建了独立验收环境，安装 Cellpose 3.1.1.3、Torch 2.7.1+cpu、NumPy 2.0.2、Numba 0.61.2 和 OpenCV-headless 4.11.0.86。两份权重从记录的官方 URL 下载，并核对固定字节数与 SHA-256 后才运行。

实际执行 `test_real_official_model_inference_preserves_input` 的 `nuclei / 17 px` 与 `cyto3 / 30 px`：**2 passed，12 deselected，8.77 s**。验证了真实推理的原尺寸标签、官方模型元数据、CPU、Numba workqueue 与原始 uint16 输入不变；不把这个用例解释成真实样本分割准确率测试。

这是新建验收环境的结果。历史 `Figure_Workbench_0.2.0_Cellpose_CPU.zip` 的整包 SHA-256 虽已重新核对，但其 Numba/OpenCV 等版本不同，尚未在 Windows 执行；不能把这两项通过写成旧便携组件兼容性已通过。Windows 流水线还需执行对应平台的完整检查。

## 资源和取消的实际边界

- 512 MiB 是单次分析已解码的**输入平面累计预算**，不是整个进程的峰值 RSS 承诺。解码缓冲、浮点中间结果、图像缓存和本地库还需要额外内存；本轮未做所有算法的大图内存峰值基准。
- 预算在读完一个平面后核对，超额时终止并且不再读取后续平面。单平面在解码前仍依赖已有图像导入/解码限制。
- 普通 Python/NumPy/SciPy 分析采用协作取消。单个不可中断的本地计算会在返回后到检查点停止；取消时界面状态可以先变为 cancelled，`cleanup_pending=true` 表示资源未完全释放。排队槽位不会因此提前开放。
- Cellpose 的独立工作进程有终止路径；该路径用实际测试子进程检验过，但不代表真实模型精度已经验收。
- 不能凭这些检查声明“性能领先”。跨设备性能、真实大图吞吐、峰值内存和 UI 响应需要单独记录规模、硬件和比较方法。

## 发版前仍需完成的验收

本轮不得据此发布一个声称“所有功能已无问题”的版本。至少还有以下明确门槛：

1. **Windows 原生 EXE 全流程。** 在干净环境验证双击后进入客户端、首次 WebView2/依赖行为、真实文件选择与拖放、关闭保存、二次启动、模型组件安装失败/重试和目录权限。Linux/ASGI 通过不能代替此项。
2. **可视交互。** 在实际客户端逐步完成 WB、qPCR、RGB IHC、多通道显微、纤维、实例修订和组图导出；检查缩放、滚动、遮挡、输入焦点、错误提示、取消/重试和新用户路线。Happy DOM 不计算实际排版与 WebView 行为。
3. **真实 WB。** 使用经人工确认的不同曝光、噪声、非均匀背景、弯曲泳道、缺失条带和多蛋白原件，核对定位候选、可编辑 ROI、内参配对和导出图稿。确定误检/漏检评价方法与可接受标准；合成定位测试不能充当准确率。
4. **真实 qPCR 导出。** 用拟支持仪器的真实 CSV/TSV/TXT/XLSX（含中文列名、不同编码、多个 Results 页和异常孔）与独立表格计算核对；多有效结果页时应确认实际采用的工作表与原始行号。
5. **真实 IHC/纤维/实例标注。** 用独立标注数据评价染色向量、等级阈值、纤维分割、核归属和人工修订后的几何结果。组织像素加权指数不能描述成逐细胞 H-score；软件输出不能替代实验/临床验证。
6. **便携 Cellpose 推理。** 上述 Linux 新环境已实际通过 nuclei、cyto3 两个测试，不再记为跳过；0.5 Windows 便携组件仍须使用其自身解释器执行同样测试，并在实际 EXE 中验证自动发现与推理。真实图像质量评价、首轮/后续性能和取消时资源清理需分别记录。取消/超时测试使用跨平台的 `subprocess.terminate/kill/wait`，Windows 分支使用 `CREATE_NO_WINDOW`；Windows 是否通过以对应 CI/客户端证据为准。
7. **代表性大工程。** 按实际通道数、原图大小、ROI 数、测量记录与面板数量测试导入、分析、重放、保存、备份和导出的耗时/峰值内存。只有有记录的设备、规模和结果才能成为性能承诺。

发现新失败后，应先保留可复现输入和预期结果，补上针对性回归，再重新检查受影响的实际工作流。此清单没有把未执行项目等同于通过。
