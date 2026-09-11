# v0.3.4 — 首次使用与运维体验优化

v0.3.4 集中降低首次安装、QMT 部署、状态验收、账户调整和问题反馈门槛。交易、风控、数据库 schema、MCP 交易工具和 Profile 签名边界均未放宽。

## Changes

- 新增 `安装、升级或修复.cmd`，安装 wheel 前自动校验 `SHA256SUMS.txt`；旧安装文件名继续可用；
- 新增“5 分钟只读上手”和五篇任务型文档；
- 每个 QMT 账户目录自动生成 `部署说明.txt`，交互式安装完成后自动打开 `qmt_ready`；
- 新增 `verify --human` 统一检查安装、MCP、Worker、QMT 文件和 Adapter 状态；
- 新增 `account list/enable/disable/configure`，已有环境不再需要手工编辑 JSON 来启用信用账户；
- 新增 `open`、`--version` 和只读 `upgrade-check`；
- 新增 `support-bundle --redact`，默认不包含日志正文、数据库、Token、密钥、完整账户号或 Profile 内容；
- Y/N 向导遇到非法输入时会重新询问，不再静默当成“否”；
- QMT Adapter 配置路径改用 ASCII 转义字面量，支持运行目录中不能用 GBK 表示的 Unicode 字符；
- Release ZIP 增加快速开始、任务文档和真实的隔离 setup/重复升级测试。

## Upgrade notes

- 停止 Worker 和全部 QMT 策略后，完整备份 runtime；
- 把本版 ZIP 解压到新目录，双击 `安装、升级或修复.cmd`；
- 已有签名 Profile 默认原样保留，不需要仅为本次升级重新完成 P0 或重新签名；
- 新生成的 `部署说明.txt` 不参与 Profile 签名；
- 升级后在 `OBSERVE_ONLY` 下运行 `verify --human`，再重启 WorkBuddy。

## Assets

- `workbuddy_qmt_bridge-0.3.4-py3-none-any.whl`
- `workbuddy-qmt-bridge-0.3.4.zip`
- `workbuddy-qmt-bridge-0.3.4.zip.sha256`
- `SHA256SUMS.txt`

四个资产必须来自同一次最终构建。目标 QMT、券商和真实资金账户的现场验收不包含在本次软件测试结论中。
