# Figure Workbench 0.4.0 Windows 便携包

用户完整解压 `Figure_Workbench_0.4.0_Windows_Main.zip` 后，双击 `FigureWorkbench.exe`，浏览器会打开本机工作台。无需手动安装 Python、pip 或 Node.js。必须保留同目录的 `runtime/` 和 `app/`；这个 EXE 是便携目录包的启动器，不能单独复制运行。

工程和原始图像仍存放在 `%LOCALAPPDATA%\FigureWorkbench`。更新程序目录时保留该数据目录；跨电脑迁移工程使用应用提供的 `.figwork` 导出。

## 构建输入

新版构建脚本为 `packaging/upgrade_windows_0_4.py`。旧 `upgrade_windows.py` 留作 0.3.0 构建，不覆盖其固定输入和历史验证行为。

| 输入 | SHA-256 | 用途 |
| --- | --- | --- |
| `Figure_Workbench_0.3.0_Windows_Main.zip` | `d1d9e2f871054b2a563e789f149f68a0a55b60d9ce678db926ce3eeffa36861a` | 复用经过核验的 Windows x64 启动器、CPython 3.12.10、核心依赖、许可证 |
| `Figure_Workbench_0.2.0_Cellpose_CPU.zip` | `c758c1c8c80fe3d37b59980b5928cf2e50e1b144c83845f9c87eb1a2fff71b11` | 验证可沿用的独立 CPU 选件；不重复放进主包 |
| 当前项目源码 | 在构建快照中逐文件记录 | 替换主包 `app/`，同时生成字节一致的源码包 |

两个二进制输入必须逐字节匹配固定哈希。脚本不会下载替代运行时、安装新的 Windows 依赖或静默切换到其他旧版本。当前 `requirements.txt` 会按 Windows / CPython 3.12 环境标记重新检查；若新增依赖不能由旧运行时满足，会停止构建。

## 先做只读检查

在项目根目录使用具备 `packaging` Python 库的构建环境运行；这一步不执行 Windows 程序，也不需要 Wine：

```bash
python packaging/upgrade_windows_0_4.py \
  --base-main /path/to/recovered_release/Figure_Workbench_0.3.0_Windows_Main.zip \
  --cellpose-component-zip /path/to/recovered_release/Figure_Workbench_0.2.0_Cellpose_CPU.zip \
  --output dist/Figure_Workbench_0.4.0_Windows_x64 \
  --dry-run
```

`--dry-run` 校验完整 ZIP 哈希、清单和每个声明文件的长度及 SHA-256，静态读取保留二进制的 PE 架构，并列出当前源码版本是否已就绪。不生成发行目录或 ZIP。若额外指定 `--record`，只会按该显式选项写一个新的 JSON 检查记录，不覆盖已有记录。

该检查会完整读取约 625 MB 的两个输入 ZIP，并解压流式核验成员；需要一定时间。它不会把 CPU 组件的约 1.8 GB 解压到发行目录。

## 正式构建

完成代码修改和应用测试后，确认 `workbench/__init__.py` 与 `package.json` 的应用版本均为 `0.4.0`，再移除 `--dry-run`：

```bash
python packaging/upgrade_windows_0_4.py \
  --base-main /path/to/recovered_release/Figure_Workbench_0.3.0_Windows_Main.zip \
  --cellpose-component-zip /path/to/recovered_release/Figure_Workbench_0.2.0_Cellpose_CPU.zip \
  --output dist/Figure_Workbench_0.4.0_Windows_x64 \
  --record dist/windows_0_4_build_receipt.json
```

将产生以下配套内容：

- `dist/Figure_Workbench_0.4.0_Windows_x64/` 与 `Figure_Workbench_0.4.0_Windows_Main.zip`。
- `dist/Figure_Workbench_0.4.0_Source/` 与 `Figure_Workbench_0.4.0_Source.zip`。
- 指定的 JSON 构建回执，含主包和源码包的实际文件长度、SHA-256、ZIP 完整性结果及未修改 CPU 组件的信息。

输出目录、两个 ZIP 和回执都必须使用未占用的路径。已有发行包不会被覆盖。`--source-output` 可为源码目录指定一个不同的同级目录名；`--no-zip` 只生成两个目录，暂不生成压缩包。不要在 `web/`、`workbench/` 等被打包的源码目录内部放构建输出，统一使用 `dist/` 或源码树外目录。

## 源码与主包一致性

构建首先把当前源码复制到一个单独快照。每个复制文件都会复核长度和 SHA-256，防止写入过程中发生变化。随后在快照中生成本次 Windows 验证报告及 `SOURCE_MANIFEST.json`，再直接从同一份 `app/` 复制源码发行目录。

源码发行目录与主包 `app/` 的**全部文件路径和字节均须一致**，包括生成的验证报告和源码清单本身。`SOURCE_MANIFEST.json` 的 `files` 列表不包含它自身，避免自引用哈希；这一排除在字段 `manifest_self_excluded` 中明确记录。主包外层 `WINDOWS_BUILD_MANIFEST.json` 再记录源码清单的哈希及主包全部其他文件。

此过程不改写开发目录中的 `packaging/windows_validation.json`，也不会把构建结果误写回正在开发的源码。回执与发行包内报告才是本次构建的记录；仓库保留的旧报告仍按自身版本解释。

源码包含新的 `web/browser-studio.*`、`web/browser/` 和 `scripts/build_browser_studio.mjs`。所有层级的 `dist/`、`.venv/`、`node_modules/`、版本控制目录、Python 缓存和测试浏览器缓存都会排除，防止把安装环境或独立浏览器大文件再次塞入 `app/`。符号链接、Windows 路径别名和文件名大小写冲突会被拒绝。

自包含的 `Figure_Workbench_0.4.0_Browser.html` 由浏览器构建脚本独立生成和发布；它不作为 Windows 运行时必需文件，也不重复打进源码目录。若后续把它放在 `dist/browser-studio/`，相同排除规则仍然有效。

## CPU 选件与清单结构

0.3 主包的 `WINDOWS_BUILD_MANIFEST.json` 只列出主包文件；CPU 选件另有自己的 `CELLPOSE_BUILD_MANIFEST.json`。新脚本分别核验，避免把 0.2 旧总清单的结构错误套用到 0.3。

既有 CPU ZIP 还含 `CELLPOSE_INTEGRATION.json`。该旁车未列入组件原始文件清单，因此不能简单忽略所有额外文件。新版只允许这一个已知集成旁车和组件清单自身，验证其组件身份、原始清单哈希及独立运行环境声明，并通过固定整个 ZIP 的哈希对它进行认证。任何其他未列出的文件都会失败。

新版主包清单记录 CPU 组件版本仍是 `0.2.0`、归档哈希未改变、`files_modified: false`、`bundled_in_main_archive: false`，并记录模型哈希及静态兼容依据。用户可以继续使用既有选件，无需重新下载同一份二进制。

## 验证边界

构建程序只做静态检查：固定输入身份、逐文件字节、依赖声明、PE32+ / AMD64 架构、主包与源码一致性及 ZIP CRC。对应测试可在 Linux 上运行：

```bash
python -m pytest tests/test_windows_upgrade.py tests/test_windows_composition_upgrade.py
```

**没有执行 Windows 原生程序。** 构建报告中的 `windows_runtime_executed` 和主清单的 `windows_runtime_execution_verified` 均保持 `false`。静态架构检查不能证明全部 DLL 导入闭包、Windows 浏览器关联、字体效果、Office 渲染或真实用户路径下的启动行为。应用测试报告有自己的版本和执行平台；打包脚本不会把旧报告重新宣称为 0.4 Windows 真机验收。

Windows 发布前仍应按项目验收流程执行真机启动、导入保存、核心分析、组图与导出操作，再以单独记录补充相应结果。
