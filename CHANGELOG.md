# Changelog

本项目的重要变化记录在此。版本号遵循 Semantic Versioning。

## Unreleased

### Added

- `prepare_trade`：一次完成必要快照刷新、有限等待和安全预览，不会自动提交订单；
- `wait_trade_intent`：提交后进行一次最长 5 秒的有界等待，超时不会重发；
- 新增组合准备、等待超时、单写者并发和快速轮询回归测试。

### Changed

- QMT Adapter 命令轮询默认从 1 秒缩短为 500ms，Worker 回报摄取轮询从 1 秒缩短为 250ms；
- QMT Adapter 的 5 秒周期快照只刷新账户与持仓；委托/成交以实时回调为主，每 30 秒完整对账一次，并继续在启动和显式同步时完整对账；
- 报单结果不确定或 QMT 返回委托错误时，会请求下一个 5 秒守卫周期提前执行委托/成交对账；
- Worker 内 MCP 请求线程和后台事件摄取使用同一个进程内写事务锁，减少 SQLite 写锁尾延迟；
- MCP 工具说明要求单账户多标的批量同步、复用新鲜账户/持仓快照，并在提交后只调用一次有界等待。

### Safety

- `prepare_trade` 只生成预览，继续保留独立确认与 `submit_trade_intent`；
- `wait_trade_intent` 只读且绝不重发；所有 Profile、授权、硬风控和双端复核保持不变；
- 本轮未加入端到端耗时追踪或额外日志字段。

## 0.3.4 - 2026-09-11

### Added

- 5 分钟只读快速开始、任务型文档和兼容性矩阵；
- `verify`、`open`、`upgrade-check`、`support-bundle --redact`；
- `account list/enable/disable/configure`，支持已有环境安全调整账户；
- 每账户 QMT 部署说明和桌面验证/打开目录入口。

### Changed

- 安装入口更名为“安装、升级或修复”，旧名称继续兼容；
- 安装 wheel 前自动核验 Release 内 SHA-256；
- 向导非法 Y/N 输入会重新询问，完成后自动打开 QMT 文件目录；
- QMT 配置路径使用 ASCII 转义，支持非 GBK Unicode 路径；
- Release ZIP CI 增加隔离首装、重复升级和 Profile 保留检查。

### Safety

- 所有新验收与版本检查命令均不开放交易；
- 账户停用保留文件和 Profile，并要求专用确认词；
- 脱敏诊断包不包含日志正文、数据库、Token、密钥、完整账户号或 Profile 内容；
- setup、升级和强制重新生成继续默认保留已有签名 Profile。

## 0.3.3 - 2026-09-08

### Fixed

- 非交互 setup 的机器可读 JSON 改为 ASCII 转义输出，避免英文 Windows 的 `cp1252` 控制台因中文字符报 `UnicodeEncodeError`；
- Profile 安全回归测试现在同时验证非交互输出可跨 Windows 区域设置传输和解析。

### Safety

- 完整包含 0.3.2 的 Profile 升级保护：普通 setup、安装向导和 `--force` 均保留已有 Profile，重置仍必须指定账户和专用确认词。

## 0.3.2 - 2026-09-08

### Fixed

- 修复重复运行安装/配置向导时，通用覆盖确认可能把已签名 Profile 重置为未验证模板的问题；
- `setup --force` 现在只覆盖变化的 Adapter 和配置文件，不再覆盖已有 `qmt_profile.json`。

### Safety

- 已有 Profile 默认始终保留；
- 重置必须通过 `--reset-profile <账户别名>` 明确指定目标，并同时提供 `--confirm-reset-profile RESET-QMT-PROFILE`；
- 确认重置前仍会为原 Profile 创建带时间戳的备份。

### Tests

- 新增安装向导、强制更新、错误确认、显式重置和备份恢复共 6 项 Profile 安全回归测试。

## 0.3.1 - 2026-09-07

### Changed

- 清理未使用的导入和异常变量，不改变交易、风控或协议行为；
- 发布脚本改为从 `pyproject.toml` 读取版本，并在构建前清理同项目的旧版本产物；
- 安装脚本在目录中存在多个 wheel 时明确失败，避免误装旧版本；
- CI 安装校验改为比较包元数据与运行时版本，移除重复的硬编码版本号。

### Packaging

- 发布 ZIP 构建后自动校验必需文件；
- 修正发布包内容说明，使其与实际 ZIP 目录保持一致。

## 0.3.0 - 2026-08-25

### Added

- LIMITED_AUTO P1 readiness、结构化策略许可、状态查询、恢复和撤销；
- 同一交易日内最长 720 分钟的策略授权；
- 账户、证券、动作、策略/规则版本、交易窗口和下单类型绑定；
- 单笔、累计金额、订单数、频率、并发、持仓敞口、账户回撤和连续失败限制；
- Worker 原子预算预留与 QMT Adapter 授权代次、策略哈希复核；
- schema v1 到 v2 自动迁移；
- 7 项 P1 增量测试。

### Safety

- 默认模式继续保持 OBSERVE_ONLY；
- 信用自动交易默认关闭；
- 熔断、健康异常、回撤或连续失败可暂停或撤销许可；
- PRE_SUBMIT、SUBMIT_CALLED 和 SUBMIT_UNKNOWN 不自动重发；
- 软件 P1 不代替目标 QMT、券商和真实资金账户的现场验收。

### Changed

- MCP 工具总数更新为 29；
- QMT Adapter 协议增加 LIMITED_AUTO 许可和策略绑定字段；
- 安装脚本同时支持 Release wheel 与源码仓库 editable install。
