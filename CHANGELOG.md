# Changelog

本项目的重要变化记录在此。版本号遵循 Semantic Versioning。

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
