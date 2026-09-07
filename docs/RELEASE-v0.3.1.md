# v0.3.1 — 维护与发布整理

v0.3.1 是不改变交易行为的维护版本，清理了未使用代码，并收紧安装与发布流程，降低旧版本产物混入发布目录或被安装脚本误选的风险。

## Changes

- 删除 10 处未使用的导入或异常变量；
- 发布脚本从 `pyproject.toml` 自动读取版本，构建前清理同项目旧产物；
- 发布 ZIP 生成后校验 wheel、安装脚本、许可证、说明、验证记录和示例是否齐全；
- 安装脚本检测到同目录有多个 wheel 时停止并给出处理提示；
- CI 通过安装元数据校验运行时版本，不再重复硬编码版本号；
- 发布包内容说明与实际 ZIP 目录保持一致。

## Upgrade notes

- 从 0.3.0 升级时，停止旧 Worker，运行新版 `首次安装与配置.cmd`，然后重启 Worker 和 WorkBuddy；
- 数据库 schema、`bridge.json`、签名 Profile、MCP 工具和 Adapter 协议均未变化；
- 已部署的 0.3.0 QMT Adapter 与 0.3.1 兼容，不要求仅为本次维护升级重新部署或签名；
- 从 0.2.5 或更早版本直接升级时，仍须遵循 0.3.0 的数据库与 Adapter 协议迁移要求；
- 升级后继续从 `OBSERVE_ONLY` 检查状态，不会自动恢复任何交易授权。

## Assets

- `workbuddy_qmt_bridge-0.3.1-py3-none-any.whl`
- `workbuddy-qmt-bridge-0.3.1.zip`
- `workbuddy-qmt-bridge-0.3.1.zip.sha256`
- `SHA256SUMS.txt`

四个资产必须来自同一次最终构建。目标 QMT、券商和真实资金账户的现场验收不包含在本次维护版本的软件测试结论中。
