# 仓库来源与维护基线

## 主维护仓库

Figure Workbench 的主维护仓库为 `aper-ai/figure-workbench`。

本次迁移采用已发布的 **0.6.1** 源码快照。迁移前仓库为 `tuantuan007/figure-workbench`，对应提交为 `ea31f2de229d66da8d7fa331caa1f644d7027de6`。该提交包含原主分支 `e29907761f7bc4036c489acba1ed97c05c533396` 的全部内容，并补充 0.6.1 已验收的任务生命周期及交互修复。

迁移是主维护位置的调整；第三方软件、字体、规格目录及设计参考的来源与许可继续保留。新仓库创建时间不代表底层依赖或第三方资料的原创时间。

## 软件与介绍文件

迁移基线保留 0.6.1 的应用、计算、测试和打包源码。本轮变更涉及介绍文档、实跑截图及其来源清单，以及 Windows CI 获取基础运行时附件的发行标签。应用、算法和打包器源码仍与 `ea31f2de229d66da8d7fa331caa1f644d7027de6` 一致。项目首页重新组织为产品定位、功能模块、实跑示例、运行方式、输入输出和验证记录；原完整说明保存在 [使用手册](USER_GUIDE.md)。

迁移时的 0.6.1 截图来自该版本 Windows 验收附件，来源记录保留在[历史截图清单](screenshots/0.6.1/manifest.json)中。0.6.2 源码移除了这些历史截图文件；历史像素与报告仍可在 0.6.1 验收附件中核对。当前文档仅保留明确标注的 AI 演示输入，新的实际软件截图由对应提交的 Windows 验收生成。所有文档图像均不进入客户端运行目录。

## 既有发行与验证

- [本仓库 0.6.1 发行镜像](https://github.com/aper-ai/figure-workbench/releases/tag/v0.6.1)
- [原始 0.6.1 发行记录](https://github.com/tuantuan007/figure-workbench/releases/tag/v0.6.1)
- [原始 Windows 验收运行](https://github.com/tuantuan007/figure-workbench/actions/runs/43452637043)
- Windows 完整包 SHA-256：`66881bc4ef5578a9651a09af5aa4dd33bcc7676f8728c80e0a0ccaf247ab6a2e`
- 验收附件 SHA-256：`fffad141737e0a4588f8424f4b6b338c6693d1281bd9887b5b3ec64b6950d25c`

0.6.1 发行镜像保留六个原始附件的文件名、字节内容和 SHA-256；其中源码包对应原验收提交，主分支另含本次文档、截图与 CI 下载标签调整。原始发行的验收结果对应上述源码与成品；本次迁移不改变既有软件包。新仓库后续代码修改与发行应执行自身对应提交的验收，不能沿用旧提交的测试结论。

## Windows 构建基础

现有 Windows 打包流程使用经过摘要校验的 0.4.0 主包作为嵌入式 Python 基础。原附件从[迁移前 v0.4.0 发行记录](https://github.com/tuantuan007/figure-workbench/releases/tag/v0.4.0)保留到本仓库独立的 [build-runtime-0.4.0 构建资料](https://github.com/aper-ai/figure-workbench/releases/tag/build-runtime-0.4.0)，不作为最新软件发行。

- 文件名：`Figure_Workbench_0.4.0_Windows_Main.zip`
- SHA-256：`5aac14044bbd5000f4fc31754a6bd1d08f2e9809d501393af86359cf24f03fb8`

Windows CI 的 `gh release download` 从本仓库的 `build-runtime-0.4.0` 标签获取上述附件；其余构建流程、打包器和摘要校验保持不变。基础 ZIP 内部的 0.4.0 文件名与清单版本保留原值。普通用户下载 0.6.1 完整工具包，源代码运行与已发布 0.6.1 的本地使用不依赖新的构建执行。

## 许可

项目许可证为 [AGPL-3.0](../LICENSE)。Marker 目录来源及第三方许可见 [licenses](../licenses/) 和 [上游实现对照](UPSTREAM_COMPARISON.md)。
