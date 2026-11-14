# 前端开发与可复现检查

浏览器应用使用原生 ES 模块，不依赖 Node.js、React 或 npm 包运行。
`package.json` 中的依赖只用于开发检查；交付的本地应用仍由 Python 服务直接提供静态前端。

## 安装开发工具

开发检查使用 Node.js 22 或更新版本。

```bash
npm ci
```

Oxlint 是固定版本的开发依赖，版本和完整性信息保存在 `package-lock.json`。
项目没有 React、shadcn/ui 组件和相应主题系统，因此不安装只适用于该组件体系的
`@shadcn/lint`，也不为了 lint 引入新的应用运行依赖。

## 代码检查

```bash
npm run lint
```

检查浏览器代码、新定位引擎、前端测试和快照基准脚本。正确性问题作为错误处理，
包括无效表达式和未使用的变量；没有为了通过检查关闭这些规则。

## 功能回归

```bash
npm test
```

运行四组测试，目前共 51 项：

- `tests/test_frontend.mjs`（15 项）：原图 ROI 边界、跨素材 ROI 编号、显微区域操作、真实泳道移除间隔、翻转坐标映射、有效 PPI、字体、面板比例、比例尺、图表类型，以及只保存配方的撤销重做。
- `tests/test_analysis_state.mjs`（10 项）：按来源与科学参数选择结果、同源历史记录、图表 measurement ID、过期判定、布局独立性、撤销参数同步、新工程默认值和未入库结果。
- `tests/test_channels.mjs`（20 项）：跨帧显示保留、完整原始平面身份、同尺寸组合校验、分屏布局与分数像素裁切、逐 tile PPI/比例尺、分析通道与显示解绑、ROI 按身份保存/撤销、独立阈值/背景、IHC 原始 RGB、CPU 参数边界、原表排除孔、曝光选择，以及预览去重、过期取消、内存限制和有限重试。
- `tests/test_gel.cjs`（6 项）：定位引擎、空图不伪造泳道、坐标还原、Marker 插值与候选匹配。

只运行前端领域和状态测试：

```bash
npm run test:frontend
```

状态测试会替代自动保存计时器，不请求本地 HTTP 服务、不创建用户工程。
测试明确检查撤销不会回滚已经完成的分析结果、原件资产或最新项目修订号。

## 撤销快照基准

```bash
npm run bench:frontend
```

默认创建 1,000 个合成几何对象，每个对象有 128 个轮廓点。它们仅用于模拟结果载荷，
不代表真实实验数据或分割准确率。脚本输出可保存的 JSON，包含：

| 字段 | 含义 |
|---|---|
| `recipe_median_ms` | 100 次编辑配方快照的中位耗时 |
| `recipe_p95_ms` | 同一组快照样本的第 95 百分位耗时 |
| `full_project_clone_single_sample_ms` | 深复制完整项目一次的参考耗时 |
| `recipe_serialized_bytes` | 编辑配方序列化后的字节数 |
| `full_project_serialized_bytes` | 完整项目序列化后的字节数 |
| `immutable_results_excluded` | 快照是否明确排除了已完成分析结果 |

可显式更改模拟对象数量，范围为 1–10,000：

```bash
node --expose-gc scripts/benchmark_frontend.mjs 5000
```

序列化字节数是载荷大小，不是进程堆内存测量；耗时随硬件和当时负载变化。
这个基准验证撤销结构的性能特征，不用于声称整套应用或科学分析算法的普遍性能排名。

## 模块边界

`domain.js` 放置无 DOM 的单位、几何与源映射逻辑；`state.js` 管理可编辑配方和串行保存；
`api.js` 是 HTTP 边界。原始资产、完成的测量和审计记录不进入撤销栈。

`gestures.js` 在拖动时仅通过 `requestAnimationFrame` 更新当前 SVG 对象，松开指针后提交一个历史记录。
每个通道 tile 都保留独立原始像素坐标系。平移和缩放仅改变显示，不改变 ROI、背景区域或线剖面的源坐标。
`source-overlays.js` 负责矩形、椭圆、多边形、线、泳道与结果轮廓的共同显示。

`channel-domain.js` 定义原始平面身份、显示配方、同尺寸组合和分屏布局。
`channels.js` 只负责通道 API 与配方操作；`channel-view.js` 展示明确的原件/系列/帧/通道。
`channel-preview.js` 提供去重、取消和有界 Blob URL 缓存：最多 64 条、128 MiB 的保守 PNG 加 RGBA 表面预算，
并发 2 个请求。过期请求的完成不会覆盖新配方；HTTP 429 最多重试 3 次，取消后不再重试。
缓存预算不是整个浏览器进程的实测内存上限。组图预览按实际画布尺寸分档，避免为每个小面板读取全尺寸预览。

`gel.js` 从二进制接口读取指定原始平面的 Float32 定位缓冲并传给 Worker；它不执行定量计算。
`analysis-domain.js` 共同构造科学参数、来源身份、输入签名和图表请求；结果与显微轮廓使用同一个选择器。
显示颜色、名称、可见性、Gamma 和图稿布局不进入科学输入签名。测量使用显式选择的原始通道与保存的科学区域。
`analysis.js` 处理原始数值任务与版本校验；`analysis-forms.js` 和 `analysis-results-view.js` 分别管理输入与真实结果展示。
未写入当前工程的计算结果与科学输入变化后的旧结果分别提示，两者均不能冒充可用于作图的已保存记录。

`input-lifecycle.js` 保留未提交的原生输入节点。后台预览或保存状态变化在草稿提交前延后重绘，
避免替换节点后丢失浏览器原生 `change` 事件。Tab/Enter 提交后重绘，Esc 丢弃草稿；指针点击在
当前 focus/change/click 序列完成后重绘，避免输入失焦时把用户正在点击的按钮移除。
`dom-render.js` 在值未变化、绑定与上下文一致时保留聚焦输入及其祖先节点，仅替换其它分支。
这也保留了数字输入框无法通过 `selectionStart` 恢复的原生全选状态。上下文包括工程、素材、
面板、工作流、完整原始平面、弹窗实例及组合来源；输入的 `data-*` 绑定也须一致。
跨上下文不复制旧输入值，SELECT 每次更新选项列表。

## 真实浏览器回归

安装 Python 开发依赖和 Playwright Chromium 后运行：

```bash
python tests/browser_workflow.py
python tests/browser_channels.py
python tests/browser_inputs.py
```

每个脚本启动独立本地服务，使用明确标记的合成素材和临时工程；不读写用户工程。
可通过 `BROWSER_EXECUTABLE` 指定 Chromium 可执行文件，`FW_BROWSER_RESULTS` 指定截图和 JSON 报告目录。

`browser_inputs.py` 在输入与 Tab/Enter/Esc 之间主动触发后台重绘，检查原生节点、值与提交状态，
并验证连续帧切换、空数值、切页、切换原图、checkbox 和 modal 不会把界面留在长期阻塞状态。
它也强制在数字全选与删除之间、按钮按下与点击之间刷新，并验证 900 px 窗口中的三列布局、
按原始像素比例变化的分屏间隔及第二通道 ROI 的精确原图坐标。共 6 个真实浏览器场景。
测试使用可观察状态等待，不用固定延迟掩盖编辑竞态。

上述 Node 测试不替代 Python 数值验证、实际浏览器流程或导出文件检查。
