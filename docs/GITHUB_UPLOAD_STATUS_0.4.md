# GitHub 0.4.0 发布记录

目标：[tuantuan007/figure-workbench](https://github.com/tuantuan007/figure-workbench)，私有仓库。

**0.4.0 完整源码与发行附件已发布。** 下载见 [0.4.0 发行页](https://github.com/tuantuan007/figure-workbench/releases/tag/v0.4.0)，需要具有仓库访问权限的 GitHub 账号。

发布日期：2026-11-12（UTC），GitHub Release ID：`442824886`。发行标签 `v0.4.0` 指向下方已核验的源码提交，状态为预发布。

## 源码与附件核验

`main` 与 `release/v0.4.0` 已同步本次完整源码，包括全部附带字体。远端源码树与用于本次交付的本地冻结源码树完全一致：

| 记录 | 标识 |
| --- | --- |
| 远端发行源码提交 | `9a8edb97e6e37b2adcab5ec4bdcbbd2054712cc2` |
| 对应 Git tree | `a39aa0233728bed517a5dbc8dd5e10ff6ff7844d` |
| 本地冻结源码提交 | `462f06922dc922cf94f519865cf51ded35dd6818` |

上述提交记录本次发行使用的源码；后续说明文档更新可能推进分支，不改变这些已核验的源码与发行附件记录。

6 个发行附件已上传，SHA-256 均与本地交付文件逐项核对一致。Windows 主包、单文件浏览器版和完整源码包，以及升级说明与校验文件均可从发行页取得。

## 验证范围

源码和附件发布完成，**不代表新增运行平台验收**。Windows 真机及 0.4 真实浏览器回归仍未完成验收；已有 Linux Python/Node 回归、静态打包检查、合成验证和基准按原记录解释，历史 0.3 Chromium 记录不算作 0.4 已通过。

免安装浏览器版是本地 HTML，不是已部署的公网服务；浏览器 PDF 为栅格输出，完整科学分析继续使用本机版。功能、格式和验证边界见 [0.4 功能核对](FEATURE_AUDIT_0.4.md)、[浏览器版说明](BROWSER_STUDIO.md) 与 [Windows 打包说明](WINDOWS_0_4_BUILD.md)。
