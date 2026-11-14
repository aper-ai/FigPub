# Windows x64 便携版构建说明 · 0.2

## 给使用者

完整解压 ZIP 后双击 `FigureWorkbench.exe`，工作台自动在默认浏览器中打开。程序使用随包运行环境，不调用系统 Python，不执行首次 pip 安装，不要求 Node.js、CUDA 或管理员权限。

0.2 支持两种构建：**核心版**包含凝胶、WB、qPCR、多通道、经典显微分析与组图；**带 Cellpose CPU 的完整目录包**在同一 ZIP 中额外包含官方 `cyto3`、`nuclei` 权重和独立推理环境，解压后直接使用，无需另外搬移模型目录。是否包含模型以顶层 `WINDOWS_BUILD_MANIFEST.json` 的 `distribution` 和 `optional_components.cellpose` 为准，不凭文件名猜测。

EXE 同级必须保留 `runtime/` 和 `app/`；带模型的版本还必须保留 `cellpose/`。只复制 EXE 会缺少运行文件。`cellpose/runtime/` 与主 `runtime/` 是两个独立环境，不能把它们的 DLL、NumPy 或 site-packages 混合。原独立组件仍可单独构建和分发，原组件说明文件随合并包保留作来源记录；合并版直接从主 EXE 启动。

用户工程和原件保存在 `%LOCALAPPDATA%\FigureWorkbench`，不会写进运行时目录，也不会随软件文件覆盖升级。保留控制台窗口，退出时按 `Ctrl+C`。默认启动日志在 `%LOCALAPPDATA%\FigureWorkbench\logs\launcher-<PID>.log`；显式 `--data-dir` 的日志随该目录保存，无法写入时会回退并显示实际路径。日志按 2 MiB 轮转、保留 1 份备份，清理到最近 20 个日志文件。

## 两套运行环境与许可记录

| 部分 | 组成与路径 | 验证及边界 |
|---|---|---|
| 原生启动器 | `FigureWorkbench.exe`；`packaging/windows_launcher.c` 通过 Zig 0.15.2 编译为 Windows x86-64 PE | 按自身路径定位随包解释器，使用 Windows 参数编码；没有代码签名 |
| 核心运行时 | `runtime/`；PSF 官方 Python 3.12.10 embedded x64，核心固定依赖（包括 NumPy 2.5.4） | `runtime/python312._pth` 只引用随包标准库、依赖和 `../app` |
| 应用 | `app/` 中完整新源码、前端、字体、文档、测试和许可 | 运行时不导入两套上游的 PyQt/JavaScript 应用模块 |
| Cellpose CPU 组件 | `cellpose/runtime/`；独立 Python 3.12.10、Cellpose 3.1.1.3、Torch 2.7.1+cpu、NumPy 2.0.2 | Cellpose 3 的 NumPy 版本与主环境不同；独立 `_pth` 只含组件自身路径，worker 清除继承的 `PYTHONPATH/PYTHONHOME` |
| 官方模型 | `cellpose/models/cyto3`、`nucleitorch_0`、`MODEL_SOURCES.json`、`CELLPOSE-LICENSE.txt` | 按固定 URL、长度和 SHA-256 核验，运行时不自动下载、不接受任意权重路径 |
| Microsoft VC 运行库 | 两个运行时分别保存所需原始 x64 DLL；完整原文在顶层 `licenses/Microsoft-VC-Runtime/` | 合并前比较两套运行库字节哈希；`CELLPOSE_INTEGRATION.json` 明确共享许可记录，不将 Microsoft DLL 归为 AGPL/MIT |

Python 官方嵌入版可作为应用私有运行环境；本构建固定依赖并随应用分发，不让最终用户管理 Python 包。[Python 官方说明](https://docs.python.org/3.12/using/windows.html#the-embeddable-package)

Cellpose 的 worker 在导入 Numba 前固定 **`NUMBA_THREADING_LAYER=workqueue`**，并将 Numba/OMP/MKL/Torch CPU 线程限制到已验证的参数范围，默认 4、允许 1–16。此路径不使用可选 TBB 后端，因此不要求 `tbb12.dll`。元数据保存实际线程层、线程数、模型哈希和计算参数。模型加载和推理都在可取消的独立进程中；组件缺失、哈希错误或依赖加载失败会明确停止，不以其它算法代替。

模型来源及实际 Linux CPU 合成回归见 [FEATURE_AUDIT_0.2.md](FEATURE_AUDIT_0.2.md)、[cellpose_cpu_smoke.json](../benchmarks/cellpose_cpu_smoke.json)。真实推理成功只验证计算链路，不能当作实验图像分割准确率或 Windows 耗时保证。

## 合并构建的强制校验

`assemble_windows.py --cellpose-component <独立组件顶层目录>` 在创建新输出前只读验证组件，然后复制其文件，不移动、删除或修改源组件。它检查：

- 组件的版本、平台和 Python 来源身份；`CELLPOSE_BUILD_MANIFEST.json` 中每个文件的相对路径、长度和 SHA-256。
- 清单与实际文件集合完全对应；拒绝缺失、额外文件、symlink、特殊文件、路径穿越、Windows 保留设备名及大小写碰撞。
- `cellpose/runtime/python.exe` 和 `python312.dll` 的 AMD64 PE32+ 入口，以及独立 `python312._pth` 路径。
- Cellpose/Torch/NumPy 的固定 CPU 依赖与 Windows 条件依赖闭包、组件 lock 与 manifest 一致性。
- 两套官方权重的固定哈希和长度，`MODEL_SOURCES.json` 中的官方 URL、库版本、原始库许可与 wheel 记录。
- 组件与主包 Microsoft DLL 字节一致，且主包完整保存原始 Microsoft 许可；组件原有来源和许可文件仍原样保留。

复制完成后，主 `WINDOWS_BUILD_MANIFEST.json` 再逐项核对合入文件及哈希。原 `CELLPOSE_BUILD_MANIFEST.json` 自身也被主清单覆盖，另外生成 `CELLPOSE_INTEGRATION.json` 记录组件目录、隔离环境、共享许可位置和原清单哈希。每张 manifest 的 `files` 不包含自己的内容哈希，避免自引用；主清单覆盖其它实际交付文件，最终 ZIP 另做完整性及 SHA-256 检查。

独立组件构建脚本也会递归保存 Microsoft 原许可，并保留 `MODEL_SOURCES.json` 所引用且在输入目录存在的 HTTP 下载记录。合并已存在的早期组件时不会回写这些记录；若其 Microsoft 子目录只有来源记录，合并报告明确指向主包已保存的完整原文。

## 实际验证与未验证范围

此包是在 Linux 中交叉编译原生启动器、组装官方 Windows embedded Python 及 Windows 二进制依赖产生的，不是 Linux 解释器改扩展名。

构建检查包括 Windows wheel 平台/Python ABI、条件依赖、wheel RECORD 哈希、组件文件清单、PE 架构与入口、运行资源布局、ZIP 完整性。完整 PE 导入扫描和实际计数由最终构建报告记录。Microsoft DLL 与 Python 发行物均保持官方原文件。

Cellpose 组件中的 setuptools 发行包还原样携带少量 x86/ARM64 的 `cli/gui` 启动模板。这些模板不是本应用的入口，也不由本应用执行；清单逐路径列出，不能把全目录笼统记录为“所有 PE 均 AMD64”。实际 Python/模型运行入口与其它非模板二进制必须通过 AMD64 PE32+ 检查。

imagecodecs 中 HEIF、JPEG-XS、Jetraw 扩展需要额外可选外部编解码库，本版不提供这些库，也不宣称支持所有 TIFF 压缩变体。Cellpose NumPy/Numba 环境中可选 TBB 扩展可能有未提供的 `tbb12.dll` 引用；实际 worker 固定使用 workqueue，此选配后端不进入所支持的运行路径。

**当前没有 Windows 执行器，Linux 环境也不能运行 Wine；没有完成真实 Windows 启动、默认浏览器关联、字体和 Office 互操作验收。** Linux 数值/模型/浏览器测试与 Windows 静态依赖检查各自只支持其实际检查的结论。所有 `windows_runtime_execution_verified` / `windows_execution_tested` 字段仍应为 `false`。不能因为模型在 Linux 成功或 Windows 文件已齐全就改为已完成 Windows 动态验收。

`packaging/windows_validation_0.1.0.json` 保存 **0.1.0 / portable-1 的历史记录**。本次 builder 会在实际输出的 `app/packaging/windows_validation.json` 生成新的 0.2 记录，包括本次依赖数量、全包 PE 架构计数、启动器哈希和组件合并验证；不会复用旧测试计数。其中 44 个核心 wheel、317 个早期 PE、20/11 项旧定向测试及旧时间戳不得直接当作 0.2 合并包的检查结果。最终 0.2 构建应重新记录：

1. 版本/build/构建时间，最终 EXE 实际哈希（若二进制未变化可相同，但应再次计算）。
2. 核心与 Cellpose 两套依赖的各自数量/版本，原组件 manifest 哈希，合入文件数和最终主 manifest/ZIP 哈希。
3. 最终包 PE 数量、架构、DLL 导入检查；可选 codec 与不使用的 TBB 后端单列，不能掩盖必需依赖缺失。
4. 最新核心、前端、模型及打包校验的真实范围和数字，标注 Linux 平台、合成模型验证边界。
5. Windows 动态验收字段保持 `false`，直到实际有 Windows 运行证据。

完整 DLL 导入闭包仍需单独静态审查；builder 的 PE 架构扫描不会冒充它。最终 ZIP 哈希在压缩完成后的构建输出中记录，不写入 ZIP 内自引用。本说明不将历史计数重命名成新验证。完整测试结果另见 [VALIDATION.md](VALIDATION.md)。

## 重新构建

准备官方 Python embedded ZIP、Zig 工具链、固定依赖的 Windows wheel 和 Microsoft x64 运行库。构建脚本只写入新的输出目录，拒绝覆盖已有应用或组件。

先构建启动器和核心依赖：

```bash
python -m pip download -r requirements.txt --only-binary=:all: \
  --platform win_amd64 --python-version 3.12 --implementation cp --abi cp312 \
  --dest windows-wheels

zig cc -target x86_64-windows-gnu -municode -O2 -Wall -Wextra -Werror \
  -DUNICODE -D_UNICODE packaging/windows_launcher.c -o FigureWorkbench.exe
```

将已经准备好的 **独立 CPU wheel 目录**和官方模型输入目录组装为组件。组件根依赖固定为 Cellpose 3.1.1.3、Torch 2.7.1+cpu、NumPy 2.0.2；不要拿核心 wheel 目录代替。完整依赖版本由组件 `windows-packages.lock` 保存。

```bash
python packaging/assemble_cellpose_windows.py \
  --python-zip python-3.12.10-embed-amd64.zip \
  --wheels cellpose-windows-wheels --models official-cellpose-models \
  --vc-runtime microsoft-x64-runtime \
  --output Figure_Workbench_0.2.0_Cellpose_CPU_Windows_x64 --no-zip
```

生成一次解压即可使用的主包：

```bash
python packaging/assemble_windows.py \
  --python-zip python-3.12.10-embed-amd64.zip \
  --wheels windows-wheels --launcher FigureWorkbench.exe \
  --vc-runtime microsoft-x64-runtime \
  --cellpose-component Figure_Workbench_0.2.0_Cellpose_CPU_Windows_x64 \
  --output Figure_Workbench_0.2.0_Windows_x64
```

如果只需要核心版，省略 `--cellpose-component`，并选择新的输出名称，例如 `Figure_Workbench_0.2.0_Core_Windows_x64`。原有核心构建方式继续有效。两种构建都可使用 `--no-zip` 先生成目录做检查，未指定时生成带父目录的 ZIP 并校验。

`app/` 保留本应用和启动器源码；应用 LICENSE 与 Python、字体、数值库、Cellpose/Torch、模型来源和 Microsoft 运行库的原许可分别保留。组件与完整包的清单用于核对保存的字节和来源记录，不是实验图像真实性或模型准确率证明。
