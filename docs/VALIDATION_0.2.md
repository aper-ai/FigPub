# 0.2.0 验证记录

本记录对应本次交付的源码、Windows 便携包和多通道工作台。所有图像、表格和模型输入均为合成数据，没有使用用户实验数据。最终机器汇总见 [validation.json](../benchmarks/validation.json)；旧版结果单独保留在 [validation_0.1.0.json](../benchmarks/validation_0.1.0.json)，不混入本轮测试数。

## 本轮结果

| 检查 | 实际结果 | 范围 |
|---|---:|---|
| Python 全套 | 210 通过，0 失败、0 跳过 | 已启用真实 Cellpose CPU 组件；本轮总耗时 45.77 秒 |
| JavaScript | 51 通过，0 失败 | 45 项前端领域/状态与 6 项凝胶引擎 |
| 前端静态检查 | 通过 | Oxlint 1.93.0，错误 0 |
| 真实浏览器 | 22 组通过，页面异常 0 | 原工作流 8 组，多通道/真实模型 8 组，输入/布局 6 组 |
| Windows 包装 | 单独记录 | 文件与哈希、Windows 依赖、PE 架构、组件合并；不等于在 Windows 执行 |

Python 的两条提示来自环境依赖：AnyIO pytest 插件别名弃用，以及 Starlette TestClient/httpx 适配弃用；没有因此跳过测试。测试总时长不是单个算法性能。前端单测与浏览器高层场景不是同一种计数，不能简单相加宣传为独立功能数量。

环境为 Linux x86-64、Python 3.12.14、Node 24.22.1；真实浏览器是 Chrome Headless Shell 159.0.8154.2，由 Playwright 驱动。Windows 包使用独立的 Windows x64 Python 3.12.10 运行时，不能把本机 Python/Chrome 结果说成 Windows 动态验收。

## Python 数值与工程回归

| 范围 | 已验证的关键条件 |
|---|---|
| 原始平面 | uint16/float 与只读属性、TCZYX/C×S 轴、series/frame/channel 完整身份、原始文件和像素哈希不变 |
| 多通道 | 逐通道 LUT、Gamma、显隐、合并/单独/分屏；独立原件和混合 dtype；不兼容几何明确拒绝 |
| ROI 与指标 | 原坐标矩形/椭圆/多边形、背景 ROI、原值阈值、线剖面、Pearson/Jaccard/Dice/Manders、空集与常量输入不伪造有效数字 |
| 模型 | 真实官方 nuclei 权重加载、版本/哈希、进程取消与超时、初始化阶段可终止、缺模型不替换为另一种算法 |
| IHC | 显式颜色轴与真 RGB 输入、空间轴恰好为 3/4 时不误判、多通道伪彩不能冒充染色 RGB |
| WB | ROI 原始通道身份、背景净积分、内参匹配、曝光去伪重复、真实生物学 n、n=1 不填补 SD/SEM |
| qPCR | 技术孔先聚合、多内参、组别映射、原表排除标志、逐孔质控、工作表及仪器表头；TXT/CSV/TSV 分隔符与编码一致 |
| 统计 | Welch/Student/配对 t、Holm 校正、ANOVA+Tukey；按实验/靶标隔离、不按行号捏造配对 |
| 工程与任务 | 完整多来源归档、引用/哈希检查、旧工程迁移、原子保存、任务版本隔离、过期结果不覆盖新编辑 |
| 输出 | 原始 crop、分屏图块几何、翻转/旋转、每块比例尺与标签、PPI、PDF/SVG/PPTX 矩形填充及字体 |
| 资源与启动 | 预览去重、有界缓存/并发与忙碌响应、跨平台目录与文件锁、实际 HTTP 启动、端口回退、二次启动 |

文本导入还验证了带前导信息的 Tab/逗号/分号文件、UTF-16 BOM、GB18030 中文以及引号内分隔符和换行。导入预览和 qPCR 使用同一分隔符选择，实际 qPCR 结果、独立样本数及原件字节同时断言，避免只有“文件能上传”的表面测试。

## 真实浏览器操作链

浏览器通过按钮、输入、鼠标拖动、文件选择和下载操作应用；读取状态只用于断言。服务和工程在隔离临时目录运行，测试阻断外部页面资源。输入回归额外主动触发后台重绘，模拟保存/预览完成对正在填写的字段的干扰，不通过注入预设测量结果让测试通过。

### 原有流程：8 组

从空工作区加载明确标注的合成示例；拖动、撤销/重做、翻转/旋转、键盘焦点和比例尺；凝胶检出 6 泳道及 15 条带并人工校准 Marker；WB ROI 身份与结果过期、n=1 和 CSV；qPCR 两组各 3 个生物学重复及实际数据图表；示例 C0 核分割返回 9 个候选；五种图稿下载、完整工程保存重开；16 位 PNG 导入与 1280×800 布局。

脚本：[browser_workflow.py](../tests/browser_workflow.py)。

### 多通道与真实模型：8 组

1. 真实 TCZYX TIFF 导入，改通道名称/Gamma、切帧，保留同系列样式，显示三个单通道加合并图。
2. 在第二个分屏上绘制 ROI，坐标精确回到原始图；两通道均值与原始 uint16 数组直接计算结果一致，显微 ROI 不污染 WB 区域。
3. 改显示参数不使科学结果过期；切换原始帧后结果过期、ROI 隔离，切回原帧恢复对应记录。
4. 椭圆 ROI 的实际像素掩膜、Otsu 阈值，以及非空的双通道重叠和原始相关结果。
5. 在原图拖出采样线，两个通道各 12 个采样点，端点距离 140 px，并生成可见线剖面图。
6. 分屏生成四个独立可编辑面板，保存/重新打开 `.figwork` 后恢复通道配方、ROI 和四份测量。
7. 不同宽高的原件组合被拒绝，错误持久显示在对话框；同尺寸 uint16 与 uint8 组合保留两份原件及来源引用。
8. 通过界面实际核验官方 nuclei 模型、选择原始通道、运行独立 CPU 进程并保存结果；核对 Cellpose 3.1.1.3、官方权重 SHA-256、原始来源和“未使用显示 LUT”。这个单亮斑合成输入返回一个候选，只用于执行链验证。

脚本：[browser_channels.py](../tests/browser_channels.py)。第 8 组需要设置 `FIGURE_WORKBENCH_TEST_CELLPOSE=1` 并提供真实组件；本轮已启用，没有用模型替身通过该场景。

### 输入与布局

连续五次数字输入遇到多次异步重绘后仍可由 Tab 正确提交；Enter 提交文字与空的自动显示范围；Esc 丢弃未提交草稿并关闭弹窗；点击另一工作流或另一素材时先提交正确来源的草稿；checkbox 不会使重绘长期阻塞。另验证数字框“全选 → 强制刷新 → Delete”仍正确清空，以及干净输入框聚焦时“pointerdown → 刷新 → click”仍正确触发操作。900 px 视口下显式三列不被响应式样式覆盖；原始 gap 从 8 改为 40 时实际屏幕间隔从 6.078125 px 变为 30.203125 px，分屏第二通道的 ROI 仍精确还原为原图 x=20、y=25、width=80、height=60。

脚本：[browser_inputs.py](../tests/browser_inputs.py)。这些回归针对实际发现的“换了输入 DOM 后浏览器不再触发 change”问题，不要求使用者等待所有预览完成才编辑。

## Cellpose 与性能记录

另用两个官方模型分别处理 160×192 uint16、六个高斯亮斑的合成图：nuclei 与 cyto3 均实际返回六个候选，输入值不变、坐标位于原图内，实际使用 workqueue/2 线程。模型运行记录与原始哈希见 [cellpose_cpu_smoke.json](../benchmarks/cellpose_cpu_smoke.json)。这是执行回归，不是生物学准确率验证。

四通道大图的逐次耗时、RSS 采样、源文件及每个平面哈希见 [channels_results.json](../benchmarks/channels_results.json)，计时边界和冷/暖缓存定义见 [PERFORMANCE.md](PERFORMANCE.md)。没有进行原两套应用的同机全流程 A/B，不能据此宣布行业速度或分割准确率领先。

## Windows 包与历史渲染证据

本轮实际构建生成 [windows_validation.json](../packaging/windows_validation.json)，主目录的 `WINDOWS_BUILD_MANIFEST.json` 包含所有交付文件的哈希。包装契约的九项定向检查另见 [component_merge_checks.json](../packaging/component_merge_checks.json)，现有真实 CPU 组件的只读校验见 [component_merge_preflight.json](../packaging/component_merge_preflight.json)。这两份是合并前检查，最终交付清单以实际构建输出为准。

**当前环境不能执行 Windows 程序。** Windows 真机启动、原生 DLL 实际加载、字体显示和 PowerPoint/Office 打开验收仍未完成；PE 架构、wheel 依赖闭包及文件哈希不能代替动态验证。原 setuptools 包中的六个未使用 x86/ARM64 启动模板按原路径单独记录，实际 Python 入口及其它使用的 native 文件须为 AMD64。详见 [WINDOWS_BUILD.md](WINDOWS_BUILD.md)。

0.1.0 留有 Chromium 与 Inkscape 的独立 SVG 渲染对照：各 8 组，144 DPI，全图 RGB MAE 低于事先设定的 1.2；原始记录在 [svg_rendering_results.json](../benchmarks/svg_rendering_results.json)。本轮保留该历史结果，**没有重新计入当前浏览器场景总数**。0.2 新增导出几何、通道标签和分屏比例尺由本轮数值/文件结构与浏览器回归覆盖。

## 开发者复现

普通 Windows 用户直接解压、双击 EXE，不需要以下步骤。开发环境可运行：

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
npm ci
npm run lint
npm test
python -m playwright install chromium
python tests/browser_workflow.py
python tests/browser_inputs.py
python tests/browser_channels.py
```

`BROWSER_EXECUTABLE` 可指定已有 Chromium，`FW_BROWSER_RESULTS` 可指定检查结果目录。真实模型回归需另外配置 `FIGURE_WORKBENCH_CELLPOSE_PYTHON`、`FIGURE_WORKBENCH_CELLPOSE_MODELS` 并设置 `FIGURE_WORKBENCH_TEST_CELLPOSE=1`；本轮采用隔离运行时，不将模型依赖装入主 NumPy 环境。模型未配置时，普通开发测试会明确跳过真实模型用例，不应把这种运行报告成已验证模型。

仍未完成的专用组织、肌纤维、肌管、对象人工复核、部分高级染色及非参数统计流程，逐项列在 [FEATURE_AUDIT_0.2.md](FEATURE_AUDIT_0.2.md)。测试没有覆盖所有仪器格式、专有显微格式、旧工程变体或多实验室真实图像。
