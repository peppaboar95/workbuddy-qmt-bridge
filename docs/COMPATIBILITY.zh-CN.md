# 兼容性矩阵

本页区分“软件自动测试通过”和“仍需目标环境现场验收”。没有列为已验证的组合，不应被理解为不可用，但不能直接据此开放真实报单。

## Worker 与安装环境

| 项目 | 状态 | 说明 |
| --- | --- | --- |
| Windows 10/11 | 支持目标 | 安装器、桌面脚本和本机路径按 Windows 设计 |
| Python 3.10 | CI 自动测试 | 单元测试、安装与构建流程 |
| Python 3.11/3.12 | 元数据支持 | 尚未加入当前 CI 矩阵 |
| Python 3.13 | CI 自动测试 | 单元测试、wheel 构建和隔离安装 |
| Python 3.14 | CI 自动测试 | 单元测试 |
| 非 Windows | 不作为普通用户支持目标 | 核心 Python 代码可运行，但安装器、QMT 和桌面入口不适用 |

## WorkBuddy

| 项目 | 状态 | 说明 |
| --- | --- | --- |
| `%USERPROFILE%\.workbuddy\mcp.json` | 当前集成路径 | 向导只合并 `mcpServers.qmt-bridge` |
| MCP stdio | 软件实现并测试 | 当前源码 31 个工具；具体 WorkBuddy 构建仍需首次连接验证 |
| WorkBuddy 最低构建号 | 尚未固定 | 使用 `verify` 与 `qmt_health` 作为当前电脑的验收依据 |

## 大 QMT 与券商柜台

| 项目 | 状态 | 说明 |
| --- | --- | --- |
| QMT 内置 Python 3.6 | Adapter 兼容目标 | Adapter 避免使用高版本 Python 语法 |
| 普通股票限价买卖 | 软件映射框架完成 | operation、price_type、字段和状态码必须按目标构建完成 P0 |
| 信用账户 | 默认关闭 | 六类动作、资格、额度、负债与字段量纲必须逐柜台验收 |
| 任意具体 QMT/券商构建 | 未提供通用认证 | Profile 必须绑定并记录现场验证的 QMT 与券商构建 |
| 真实资金自动交易 | 不因软件测试自动获准 | 必须完成 P0、模拟闭环、小额人工实盘和 P1 策略边界验收 |

提交兼容性问题时请使用 `support-bundle --redact`，并另外提供不含账户号的 QMT 构建、券商构建、Windows 与 Python 版本。不要发送登录凭据或原始 Profile。
