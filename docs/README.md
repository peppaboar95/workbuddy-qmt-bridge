# 文档导航

第一次使用请从 [快速开始](QUICKSTART.zh-CN.md) 开始，先完成 `OBSERVE_ONLY` 只读连接。

## 使用与运维

| 目标 | 文档 |
| --- | --- |
| 安装并完成第一次只读连接 | [快速开始](QUICKSTART.zh-CN.md) |
| 查看 Release 解压目录与常用入口 | [发布包使用说明](README-RELEASE.zh-CN.md) |
| 日常启动、查询状态与管理账户 | [日常使用](DAILY-USE.zh-CN.md) |
| 升级、保护 Profile、备份与回退 | [升级、备份与恢复](UPGRADE-RECOVERY.zh-CN.md) |
| 处理安装、Worker、Adapter 与 MCP 问题 | [排障与脱敏诊断](TROUBLESHOOTING.zh-CN.md) |
| 了解 Python、WorkBuddy、QMT 与券商支持边界 | [兼容性矩阵](COMPATIBILITY.zh-CN.md) |
| 完成 P0 映射验证与 P1 授权配置 | [P0/P1 高级配置](P0-P1-ADVANCED.zh-CN.md) |

## API 与配置参考

- [在线 API 参考](https://peppaboar95.github.io/workbuddy-qmt-bridge/)：MCP 参数、响应、错误码、风控原因码和配置说明。
- [离线 API 参考](index.html)：同一份单文件 HTML，可直接用浏览器打开。
- [配置示例](../examples/README.md)：Bridge、Adapter、Profile 与 WorkBuddy MCP 示例。

## 版本与验证记录

当前版本为 `0.3.6`。各版本说明记录当时的改动与升级要求；[P1 软件验证记录（v0.3.0）](P1-VALIDATION.zh-CN.md) 是历史验证记录。软件测试通过不等于目标 QMT、券商或真实资金账户已经完成现场验收。

| 版本 | 发布说明 |
| --- | --- |
| v0.3.6 | [启动模式自动同步与使用体验整理](RELEASE-v0.3.6.md) |
| v0.3.5 | [MCP 下单延迟与 QMT 对账优化](RELEASE-v0.3.5.md) |
| v0.3.4 | [首次使用与运维体验优化](RELEASE-v0.3.4.md) |
| v0.3.3 | [Profile 升级保护与 Windows 输出兼容](RELEASE-v0.3.3.md) |
| v0.3.2 | [Profile 升级保护](RELEASE-v0.3.2.md) |
| v0.3.1 | [维护与发布整理](RELEASE-v0.3.1.md) |
| v0.3.0 | [LIMITED_AUTO P1](RELEASE-v0.3.0.md) |

## 维护者入口

- [GitHub 发布清单](GITHUB-PUBLISH-CHECKLIST.md)
- [项目变更记录](../CHANGELOG.md)
- [贡献指南](../CONTRIBUTING.md)
- [安全政策](../SECURITY.md)

本页是源码仓库的完整索引。Release ZIP 中的文档入口为 [发布包使用说明](README-RELEASE.zh-CN.md)，打包文件清单由 `tools/build_release.ps1` 维护。
