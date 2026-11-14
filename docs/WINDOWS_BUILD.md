# Windows x64 便携版构建说明 · 0.3

## 本次发行结构

`Figure_Workbench_0.3.0_Windows_Main.zip` 完整解压后，双击 `FigureWorkbench.exe`。主包包含完整 0.3 应用、专用 Windows Python 与全部核心依赖，不需要用户安装开发环境。

可选 `Figure_Workbench_0.2.0_Cellpose_CPU.zip` 是已验证并保持字节不变的 CPU 组件。解压它时，使 `cellpose/` 与 EXE 同级。已有组件可保留。它提供官方 cyto3/nuclei、Cellpose 3.1.1.3、Torch 2.7.1+cpu 和独立 NumPy 2.0.2；与主程序 NumPy 2.5.4 分开。不要混合两个 runtime 的 DLL/site-packages。

用户数据仍位于 `%LOCALAPPDATA%\FigureWorkbench`。软件升级不要求删除该目录。EXE 不是单文件应用，必须保留同级 `app/` 和 `runtime/`。主包未签名，当前没有 Windows 真机动态验收。

## 0.3 实际构建方式

本轮的算法、界面与测试均在 `app/` 中更新，没有新增核心二进制运行依赖。`packaging/upgrade_windows.py` 复用已知 0.2 的原生 AMD64 启动器及官方运行时，并把应用替换成当前源代码。

复用前逐项核验：

1. 两份旧 ZIP 的完整 SHA-256 必须符合固定输入，不接受同名替换。
2. 每个成员必须是安全、规范且不会在 Windows 大小写规则下冲突的相对路径；拒绝链接、路径穿越、设备保留名和尾部空格/点。
3. 两份 ZIP 的并集必须与旧主清单完全对应（清单自身除外）。对全部 24,410 个原文件重新读取、验证长度、CRC 和 SHA-256，包括 1.8 GB 展开的可选 CPU 组件。
4. 当前 `requirements.txt` 与保留的核心依赖版本及 Windows 条件依赖闭包必须相符；任何新依赖不满足都会停止构建。
5. 保留的运行时、启动器和许可逐字节一致，旧 `app/` 不混入新应用。重新扫描主包全部 EXE/DLL/PYD 的 PE 架构，启动入口必须为 AMD64 PE32+。
6. 从同一源码树复制 0.3 应用，并生成本次报告与**只覆盖主包实际文件**的新清单。可选组件单列其版本、哈希和兼容关系，不假装已在主 ZIP 中。
7. 对最终 ZIP 做完整 CRC 检查，记录最终 SHA-256。源码包与主包 `app/` 的文件集合与字节须完全一致。

旧依赖安装时的 wheel RECORD 验证仍属于原构建证据；本轮重新验证安装后全部文件，报告中不会把旧 wheel 安装说成重新下载或重新安装。运行时复用不意味着复用原项目业务代码。

### 固定输入

| 文件 | SHA-256 |
|---|---|
| 0.2 Windows 主 ZIP | `769abacbc8723433cf8b96374b8e7c1f11d7a5f1c5ee47a874a4b41a5554eb43` |
| 0.2 Cellpose CPU ZIP | `c758c1c8c80fe3d37b59980b5928cf2e50e1b144c83845f9c87eb1a2fff71b11` |

构建命令（开发者使用）：

```bash
python packaging/upgrade_windows.py \
  --base-main Figure_Workbench_0.2.0_Windows_Main.zip \
  --cellpose-component-zip Figure_Workbench_0.2.0_Cellpose_CPU.zip \
  --output Figure_Workbench_0.3.0_Windows_x64 \
  --record release_build.json
```

输出目录必须不存在；脚本不会覆盖旧发行包。未指定 `--no-zip` 时生成 ZIP。对外发布时可将生成的 ZIP 命名为 `Figure_Workbench_0.3.0_Windows_Main.zip`，内部顶层目录保持版本标识。

也可继续使用 `assemble_windows.py` 从官方 embedded Python、Windows wheels、原生启动器和 Microsoft VC 运行库完整组装 0.3；若使用 `--cellpose-component`，会生成包含模型的单一完整目录。底层完整组装和组件校验方法保留在 [历史构建说明](WINDOWS_BUILD_0.2.md)，其中的 0.2 验证数字不属于本轮动态验证。

## 验证证据与边界

当前报告为 `app/packaging/windows_validation.json`。`windows_validation_0.2.0.json` 与 `windows_validation_0.1.0.json` 是保留的历史证据。

主包所有 PE 文件的本轮计数、启动器哈希、基础文件校验数、组件兼容身份与当前核心依赖检查写入新报告。文件哈希与 ZIP CRC 证明交付的字节一致性，不证明 Windows 实际加载 DLL、浏览器启动关联或 Office 渲染效果。`windows_runtime_executed` 始终为 `false`，直到真实 Windows 执行验收。

Linux 的完整数值/浏览器测试，以及实际官方 cyto3/nuclei 合成图推理，记录在 [VALIDATION.md](VALIDATION.md)。不能将 Linux 耗时当成 Windows 速度保证，也不能以合成分割验证代替真实样本准确率。

imagecodecs 可选 HEIF/JPEG-XS/Jetraw 库不随包提供。Cellpose 固定使用 Numba workqueue，不依赖可选 TBB 后端。组件中的 setuptools 还原样带有不执行的 x86/ARM64 launcher 模板；组件清单与 0.2 报告保留其身份，本轮不把可选组件所有 PE 笼统记为 AMD64。

PSF Python、字体、数值库、Cellpose/Torch、模型来源和 Microsoft 原许可分别保留。主应用 LICENSE 为 AGPL-3.0；第三方 DLL 不因此改为 AGPL。
