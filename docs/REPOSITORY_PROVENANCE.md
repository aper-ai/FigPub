# 仓库来源与维护基线

## 主维护仓库

Figure Workbench 的主维护仓库为 `aper-ai/figure-workbench`。

本次迁移采用已发布的 **0.6.1** 源码快照。迁移前仓库为 `tuantuan007/figure-workbench`，对应提交为 `ea31f2de229d66da8d7fa331caa1f644d7027de6`。该提交包含原主分支 `e29907761f7bc4036c489acba1ed97c05c533396` 的全部内容，并补充 0.6.1 已验收的任务生命周期及交互修复。

迁移是主维护位置的调整；第三方软件、字体、规格目录及设计参考的来源与许可继续保留。新仓库创建时间不代表底层依赖或第三方资料的原创时间。

## 软件与介绍文件

迁移基线保留 0.6.1 的应用、计算、测试和打包源码。项目首页重新组织为产品定位、功能模块、实跑示例、运行方式、输入输出和验证记录；原完整说明保存在 [使用手册](USER_GUIDE.md)。

实跑图片来自同一提交的 Windows 验收附件，原始像素未修改。图像名称、SHA-256、尺寸、数据类型及对应场景见 [截图来源清单](screenshots/0.6.1/manifest.json)。示例数据采用合成输入，用于功能和流程展示。

## 既有发行与验证

- [0.6.1 发行记录](https://github.com/tuantuan007/figure-workbench/releases/tag/v0.6.1)
- [Windows 验收运行](https://github.com/tuantuan007/figure-workbench/actions/runs/43452637043)
- Windows 完整包 SHA-256：`66881bc4ef5578a9651a09af5aa4dd33bcc7676f8728c80e0a0ccaf247ab6a2e`
- 验收附件 SHA-256：`fffad141737e0a4588f8424f4b6b338c6693d1281bd9887b5b3ec64b6950d25c`

旧发行的验收结果对应上述源码与成品；新增的首页文档与截图不改变既有软件包。新仓库后续代码修改与发行应执行自身对应提交的验收，不能沿用旧提交的测试结论。

现有 Windows 打包流程使用经过摘要校验的 0.4.0 主包作为嵌入式 Python 基础。跨仓库运行该流程前，需在当前仓库提供同名、同摘要的构建基础附件；这一前置条件由打包程序验证。源代码运行与已发布 0.6.1 的本地使用不依赖新的构建执行。

## 许可

项目许可证为 [AGPL-3.0](../LICENSE)。Marker 目录来源及第三方许可见 [licenses](../licenses/) 和 [上游实现对照](UPSTREAM_COMPARISON.md)。
