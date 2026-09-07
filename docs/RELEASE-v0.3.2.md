# v0.3.2 — Profile 升级保护

v0.3.2 修复重复运行安装/配置向导时，通用覆盖确认可能把已签名 QMT Profile 重置为未验证模板的问题。交易、风控、数据库 schema、配置 schema 和 Adapter 协议均未变化。

## Changes

- 重复运行安装向导或 `setup` 时，已有 `qmt_profile.json` 默认原样保留；
- `setup --force` 只覆盖变化的 Adapter 和配置文件，不再覆盖 Profile；
- 有意重置时必须使用 `--reset-profile <账户别名>` 明确指定目标；
- 重置还必须同时提供 `--confirm-reset-profile RESET-QMT-PROFILE`；
- 重置前继续为原 Profile 创建带时间戳的备份；
- 新增 6 项 Profile 安全回归测试，覆盖普通重跑、非交互 force、专用确认和备份。

## Upgrade notes

- 从 0.3.0 或 0.3.1 升级时，停止旧 Worker，运行新版 `首次安装与配置.cmd`，然后重启 Worker 和 WorkBuddy；
- 已签名 Profile 会由新版向导保留，不需要仅为本次升级重新完成 P0 或重新签名；
- 数据库 schema、`bridge.json`、Profile schema、MCP 工具和 Adapter 协议均未变化；
- 如果 Adapter/配置确有变化，通用更新确认仍会先备份再覆盖这些文件，但不会连带重置 Profile；
- 升级后继续从 `OBSERVE_ONLY` 检查状态，不会自动恢复任何交易授权。

## Intentional Profile reset

只有确定要废弃旧 P0 映射时，才在停止 Worker 和对应 QMT 策略后执行：

```powershell
workbuddy-qmt setup `
  --reset-profile main_stock `
  --confirm-reset-profile RESET-QMT-PROFILE
```

## Assets

- `workbuddy_qmt_bridge-0.3.2-py3-none-any.whl`
- `workbuddy-qmt-bridge-0.3.2.zip`
- `workbuddy-qmt-bridge-0.3.2.zip.sha256`
- `SHA256SUMS.txt`

四个资产必须来自同一次最终构建。目标 QMT、券商和真实资金账户的现场验收不包含在本次补丁版本的软件测试结论中。
