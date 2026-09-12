# WorkBuddy-QMT Bridge 使用文档

当前版本为 `0.3.4`。本版集中优化首次安装、只读验收、账户配置、升级检查和脱敏诊断，不改变交易、风控、数据库 schema 或 Profile 签名边界。

当前工作区还包含未发布的低延迟调用改造；在创建下一版 Release 前不要把它描述为 v0.3.4 正式资产。

## 第一次使用

请从 [快速开始](QUICKSTART.zh-CN.md) 开始。首次目标只是完成 `OBSERVE_ONLY` 只读连接，不需要先理解人工实盘或有限自动交易。

最短路径：

1. 完整解压 Release ZIP；
2. 双击 `安装、升级或修复.cmd`；
3. 按账户目录中的 `部署说明.txt` 配置 QMT；
4. 启动 Bridge 并运行 `验证QMT桥接.cmd`；
5. 重启 WorkBuddy，调用 `qmt_health`。

## 按任务查找

| 目标 | 文档 |
| --- | --- |
| 完成第一次只读连接 | [快速开始](QUICKSTART.zh-CN.md) |
| 每天启动、查看状态、增减账户 | [日常使用](DAILY-USE.zh-CN.md) |
| 覆盖升级、Profile 保护、备份与回退 | [升级、备份与恢复](UPGRADE-RECOVERY.zh-CN.md) |
| 模拟、人工实盘和有限自动交易 | [P0/P1 高级配置](P0-P1-ADVANCED.zh-CN.md) |
| 解决安装、Worker、Adapter 和 MCP 问题 | [排障与脱敏诊断](TROUBLESHOOTING.zh-CN.md) |
| 判断 Python、WorkBuddy、QMT 与券商支持边界 | [兼容性矩阵](COMPATIBILITY.zh-CN.md) |
| 查询全部 MCP 参数、响应和错误码 | [在线 API 参考](https://peppaboar95.github.io/workbuddy-qmt-bridge/) |
| 查看 P1 软件验证范围 | [P1-VALIDATION.zh-CN.md](P1-VALIDATION.zh-CN.md) |

## 常用入口

```powershell
workbuddy-qmt --version
workbuddy-qmt status --human
workbuddy-qmt verify --human
workbuddy-qmt account list --human
workbuddy-qmt open qmt-ready --human
workbuddy-qmt open logs --human
workbuddy-qmt upgrade-check --human
workbuddy-qmt support-bundle --redact --human
```

如果 `workbuddy-qmt` 不在 PATH 中，可使用 `python -m workbuddy_qmt.manager` 代替。

## 安全边界

- 新环境和新生成的 Adapter 固定从 `OBSERVE_ONLY` 开始；
- 安装、升级、修复和账户配置不会自动启动 QMT，也不会开放交易；
- `setup` 与 `setup --force` 默认保留现有 Profile；
- MCP 不能切换本机运行模式、解除熔断或绕过 Profile/硬风控；
- 不要分享 runtime、数据库、密钥、Token、完整账户号或原始 Profile；
- 软件测试通过不等于任意真实资金柜台已经验收。
