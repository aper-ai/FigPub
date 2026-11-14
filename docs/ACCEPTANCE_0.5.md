# 0.5 发布前验收

记录日期：2026-11-12。本文规定发布前的验收范围和门槛，并保留开发期间的失败记录。用户明确要求充分测试功能、结果和完整流程后再发版；正式状态以 [GitHub Releases](https://github.com/tuantuan007/figure-workbench/releases) 中同版本、同提交的发行记录与附件证据为准。

## 验收规则

1. 数值结果使用已知输入与独立预期值检查，不能只断言接口返回 200 或文件存在。
2. 核心流程从实际导入开始，经过编辑、计算、保存、导出及重新打开，核对原件字节、坐标、位深、样本数、物理尺寸与持久化结果。
3. 失败输入、取消、重试、旧响应、撤销、重复启动、保存失败都需要覆盖。只测默认成功路径不足以验收。
4. Python/Node、真实 Chromium、Windows EXE/WebView2 分别记录。DOM 模拟、静态 PE 检查、语法检查不算真实客户端通过。
5. 通过证据必须属于相同源码提交与源码内容。构建器永远只证明静态包检查；只有运行后的报告可以证明原生窗口行为。
6. 缺少报告、失败、跳过、空场景列表、陈旧结果或包与源码不一致均阻止发布。

## 功能与结果覆盖矩阵

| 功能 | 结果与边界检查 | 实际交互验收 |
| --- | --- | --- |
| 启动与退出 | Windows GUI 子系统；锁和所有者身份；保存失败不假称成功；后台生命周期；已有窗口恢复；组件缺失诊断 | `desktop_windows_smoke.py`：实际启动 EXE、验证 WinForms 与 WebView2、二次启动、保存后关闭、进程退出、重启恢复 |
| 新手入口 | 四任务入口、前置条件、任务导入路由、未完成状态、错误恢复；导入失败不选用旧图 | `browser_onboarding.py`，同时验收首页、关闭握手和工程切换 |
| 原始素材 | PNG/JPEG/TIFF、16 位多通道、CSV/TSV/TXT/XLSX、受支持 PDF/SVG；损坏、超限、原件哈希 | 既有九套浏览器回归及原生导入；工程归档核对原件 |
| WB | 条带候选到原图 ROI；缺失泳道提示；组别、实验、样本和重复身份配对；零内参、跨组内参、重复与排除；连续源条带图与真实统计图 | `browser_wb_workflow.py`、`browser_workflow.py`；已知比值与来源坐标数值测试 |
| 多曝光 | 完整系列、饱和、ADC、噪声、面积、缺失控制与重复；推荐响应过期拒绝；应用可撤销 | WB 浏览器流程及实际异步事件处理器回归 |
| qPCR | 仪器前导元数据、多工作表、编码、多个内参；原表行号排除；技术孔先聚合；ΔCt/ΔΔCt/fold、真实生物学 n；数据表与工作表设置隔离 | `browser_onboarding.py`、`browser_workflow.py`、原生 qPCR；DOM→真实 ASGI 导入、数值、图表、归档与切表回归 |
| 多通道显示与显微 | series/frame/channel 身份；范围、伪彩、显隐、合并/分屏不改原值；异 dtype 组合；阈值、强度、线剖面、共定位 | `browser_channels.py`、`browser_inputs.py`；原始 uint16 级差与物理单位数值测试 |
| IHC | 固定/Macenko 染色向量、非负分离、强度等级、组织面积指数；伪影、校准与批次锁定 | `browser_histology_fibers.py` 及已知光密度/向量的数值测试 |
| 纤维/肌管 | 无损标签、面积、Feret、骨架、中央核/外部核、融合指数、分型；人工修订后重算 | `browser_histology_fibers.py` 及几何/标签/原始值数值测试 |
| 实例复核 | 拆分、合并、删除、补画；原始积分、坐标和标签 ID；撤销/重做；恢复失败禁导出；取消不重排；迟到结果不覆盖撤销 | `browser_instance_review.py` 及实际处理器异步回归；CSV/uint32 TIFF 与恢复配方核对 |
| Cellpose | 两个官方模型的字节/哈希、CPU 实际推理、输入不变、元数据；实际子进程取消与超时 | Python `nuclei`、`cyto3` 两个真实模型用例不得跳过；通道浏览器流程包含模型运行 |
| Cellpose 便携组件 | 与已检查环境同版本的官方 wheel、RECORD、模块字节；独立嵌入 Python、`.pth`、许可、完整清单和原模型；不复制开发环境绝对路径 | `check_portable_cellpose.py` 使用候选包自身解释器加载依赖并实际运行两个模型；后续浏览器及实际 EXE 必须绑定同一组件清单，客户端按解压到 EXE 同级的方式检查自动发现 |
| 统计图 | 真实独立观测、n=1 不造误差条、实验身份标签；混合指标不共用轴；统计检验与多重比较 | 数值/图表测试和 WB/qPCR 全流程，核对 SVG 中实际数据来源 |
| 组图 | 规则网格、跨行列、组变换、对齐/分布、编号、期刊尺寸、撤销/重做、字体、有效 PPI | `browser_composition.py`、`browser_inputs.py`、原生组图；几何与历史数值测试 |
| 导出和保存 | PDF/SVG/PNG/TIFF/PPTX/`.figwork`/原始包；尺寸、字体、像素、源关系、损坏归档、旧格式迁移、原子保存 | 导出文件解析与回读；浏览器 PDF/工程；原生另存、取消、PNG/PDF、原件下载与重开 |
| 离线轻量组图 | 单 HTML 无运行时网络；布局、字体、撤销、保存恢复、PNG/SVG/栅格 PDF；分析格式边界明确拒绝 | `test_browser_studio_ui.py` 真实浏览器；与本机工程双向互通数值/归档测试 |

上表是可检查的功能范围，不是“任意图像均准确”的承诺。条带与分割输出仍是需要复核的候选；真实实验数据的识别准确率需要独立标注和适用数据集，本版本不据合成数据宣称性能或准确率领先。

## 执行入口与证据

开发环境先安装 `requirements-dev.txt` 与 `npm ci`，Windows 额外安装 `requirements-desktop.txt`。真实模型验收单独使用 `scripts/prepare_cellpose_acceptance.py` 创建隔离的官方 CPU 环境；这是开发/CI 显式准备，应用不自动执行。

```text
python scripts/check_python.py --output test-results
python scripts/check_frontend.py --output test-results
npm run build:browser
python scripts/run_acceptance.py --output test-results/acceptance
python tests/desktop_windows_smoke.py --app-dir <解压后的候选包目录> --output test-results/desktop
python scripts/release_gate.py --reports test-results --expected-commit <待发布提交>
```

`check_python.py` 保留完整 JUnit 与源码摘要，跳过真实模型用例时返回失败。`check_frontend.py` 保留 lint、全部 Node JUnit 与源码摘要。浏览器矩阵逐套保留日志、场景报告及截图，某一套失败仍继续收集其他套结果；每次清除旧的该套报告，避免沿用上次成功记录。

可选模型组件由 `packaging/build_cellpose_candidate.py` 从固定的 Python 基础和同版本官方 wheels 重新安装，逐文件对比已验收环境中的模块与资源。静态构建记录始终标为未运行；`scripts/check_portable_cellpose.py` 随后验证完整清单、执行候选解释器 probe 和两个实际模型，再将该组件用于浏览器验收。发布检查拒绝用开发虚拟环境结果替代便携组件，也拒绝主包引用不同的模型包。

`.github/workflows/desktop-acceptance.yml` 在 Windows 上执行，构建新的 GUI 启动器和与当前源码相同的候选包。它只写开发分支的测试产物，**没有创建 tag、移动 main 或发布 Release 的动作**。测试证据和候选包均绑定到该次提交；源码有修改必须重新验收。

`.github/workflows/publish-verified-release.yml` 是单独的发行步骤，只在明确建立 `release/validated-0.5.0` 分支时触发。它必须找到该提交完整成功的验收运行，核对原始 artifacts 的来源、大小和哈希，重新执行发布门槛，再逐个核对主包、源码包、CPU 组件和离线 HTML 与实际测试记录一致。它不重新构建或打包交付文件。附件先上传到草稿，回读全部名称、大小、digest 和目标提交无误后才转为正式发行；已有同名发行或 tag 会阻止覆盖。

## 当前能与不能证明的内容

当前 Linux 环境已执行数值、API、归档、前端领域逻辑和 DOM→真实 ASGI 的流程验证。科学计算详细输入与结果记录见 [QA_SCIENCE_0.5.md](QA_SCIENCE_0.5.md)。最近一次测试的准确数量以对应运行报告为准，不把历史版本结果累计为本版通过数。

初轮桌面下载协议与 qPCR 切表修复后的 Linux 回归为 Python 530 passed、Node 188 passed。第一轮 Windows 失败修复后的统一 Linux 回归为 **Python 566 passed、0 skipped（40.46 s）；Node 193 passed、0 skipped；前端 lint 通过**。Python 已显式启用两个官方模型的实际推理。唯一 Python 警告为 Starlette TestClient 对 httpx 接口的弃用提示。这些是 Linux 验证，不是第二轮 Windows 客户端验收结果。

本轮另外在新建的隔离 CPU 环境中实际运行了官方 `nuclei` 与 `cyto3`，两项推理回归通过；权重在执行前按固定字节数和 SHA-256 验证。这个结果不等于历史 0.2 Windows CPU ZIP 已验收：旧 ZIP 中的 Numba/OpenCV 等依赖版本与新环境不同，旧组件兼容性仍待真实 Windows 检查，GitHub 的 0.4 发行附件也未包含该旧 CPU ZIP。

当前 Linux 环境真实 Chromium 启动被系统的本地 socket 限制阻止，Windows 原生程序也不能在此执行，因此实际浏览器和原生客户端转到 GitHub 的真实 Windows 执行环境验证。不能用 DOM 模拟或静态检查替代。只有 Windows 流水线完成且所有发布门槛通过，才能继续准备正式发行。

第一轮 Windows 验收（提交 `15e76db601eac17e35831522feee353198ef1418`，[运行记录](https://github.com/tuantuan007/figure-workbench/actions/runs/43233640830)）明确失败，未发布。它暴露了 Windows 后台服务就绪失败、ZIP 原始路径校验的跨平台差异、TIFF 映射句柄未释放、多选点击状态、新手导入时序以及独立 HTML 打包后的脚本错误。WB、IHC/纤维、细胞实例复核和通用流程的场景虽通过，相关进程退出时的 TIFF 占用失败仍计为整套未通过。本分支修复这些问题，并重新运行完整矩阵，不沿用第一轮的部分成功作为最终验收。

第二轮 Windows 验收（提交 `20c61cd8c10e6ec2235e4799a583ee469880b817`，[运行记录](https://github.com/tuantuan007/figure-workbench/actions/runs/43238729668)）仍未通过，未发布：566 项 Python、193 项 Node、lint、实际便携组件的两个官方模型及 7 套浏览器流程通过，但通用画布拖动流程、离线 HTML 的 CSP 等待检查和原生 CSV 保存对话框失败。实际 EXE 已打开 WinForms/WebView2 窗口，完成本地组件发现、真实依赖检查、素材导入和 qPCR 数值计算；未完成的保存与退出流程不能据此视为通过。下一轮必须使用修复后的同一源码重新执行全部矩阵。

第三轮 Windows 验收（提交 `579fd2a39cc5f865f357db4360f09e3e04638365`，[运行记录](https://github.com/tuantuan007/figure-workbench/actions/runs/43243959646)）也没有发布。634 项 Python 中 633 项通过、0 跳过；唯一失败是新增发行归档测试构造异常名称时被 Windows 提前规范化，实际交给验证器的已经是合法路径，后续修正测试样本而保留生产拒绝规则。196 项 Node、lint、便携组件真实推理及 **九套真实浏览器的 66 个场景**通过。离线 HTML 没有网络请求和页面错误，11 个场景涵盖真实拖放、编组缩放、物理尺寸与中文导出、工程重开和存储拒绝恢复。原生客户端本轮实际弹出了系统另存对话框，宿主记录下载完成事件，但预期路径中的文件检查仍失败，因此原生保存与退出验收仍不成立。自动化对话框操作必须读回实际文件名、核对系统返回的最终路径并检查落盘内容后，才能认定保存通过。

第四轮 Windows 验收（提交 `e020339669bdf23d2901611c2e0f61b59924b876`，[运行记录](https://github.com/tuantuan007/figure-workbench/actions/runs/43248836726)）仍被发布门槛阻止：636 项 Python、196 项 Node、lint、便携组件实际推理及 7 套浏览器流程通过；输入布局和快速入门的两套流程读取了重绘前的旧 DOM 节点，分别得到零尺寸和错误的焦点归属。后续检查同时发现弹窗后台重绘会重置普通按钮焦点，增加生产修复与真实事件回归，而不是仅重跑等待偶然通过。原生客户端已确认窗口、组件发现、导航约束和导入，但另存对话框虽显示测试输入的完整路径，系统返回值仍指向默认 Downloads 文件；因此保存仍明确失败，不能仅凭可见文本或完成事件认定通过。后续验收改为真实系统键盘和鼠标操作，并保留返回路径、文件内容与哈希的严格核对。独立复核另外加强 WB 全流程的已知配对比值与组统计断言，以及离线 PNG/PDF 的实际栅格内容断言；所有改动须在同一源码提交重新执行完整矩阵。

第五轮 Windows 验收（提交 `095855ddd2aebe173a516498efa7cce7d4605c3e`，[运行记录](https://github.com/tuantuan007/figure-workbench/actions/runs/43252927329)）通过了 636 项 Python、202 项 Node、lint、实际便携模型和九套浏览器的 66 个场景，全部无跳过。WB 的三条完整流程已执行独立原始积分、配对比值与组统计断言，图表实际散点位置通过；离线成品 147 个像素抽查点的最大 RGB 差为 1，PDF 与 PNG 像素一致；快速入门的双向 24 次 Tab 检查通过。原生客户端前 7 个场景通过，真实键盘和鼠标输入后系统保存路径与目标一致，qPCR CSV 六个样本的全部字段、工程归档原件字节和取消保存都通过。其后 PNG 格式选择因测试直接点击视觉隐藏的 radio，被卡片文字遮挡而超时；测试改为与用户及已通过的浏览器流程一致的可见卡片点击，再断言选中状态。原生导出、完整关闭与重启流程尚未完成，因此第五轮仍未发版，后续必须重新执行同提交的完整矩阵。

第六轮 Windows 验收（提交 `cd0480d0f40e8df5d0883db4558549b17a14a5d9`，[运行记录](https://github.com/tuantuan007/figure-workbench/actions/runs/43257288030)）再次通过 636 项 Python、202 项 Node、lint、便携模型以及九套浏览器的 66 个场景。原生前 12 个场景通过：PNG/PDF 的真实保存、尺寸与内容，原件字节、重复启动恢复同一窗口、保存失败后取消关闭、未失焦输入在关闭时提交、完整进程族退出和端口释放都已验证。第 13 个重启场景在测试读取页面 URL 时超时；后端恢复数据断言已通过，失败截图也显示正确标题、4 份素材、3 个图稿面板和 1 次分析记录。根因是测试用 `time.sleep()` 轮询 Playwright 缓存属性，未处理刚连接时的导航事件。后续改用真实页面的 `wait_for_url()` 与 DOMContentLoaded，保持 30 秒预算且不替客户端导航；完整重启、客户端模型推理及最后关闭场景仍需重新执行，因此第六轮仍未发布。

大图上限、内存预算、科学专题覆盖和迁移边界见 README 与科学记录。通过测试不扩大支持范围，也不把没有独立标注的真实样本质量评估写成通过。
