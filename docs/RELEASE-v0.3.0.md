# v0.3.0 — LIMITED_AUTO P1

v0.3.0 完成 LIMITED_AUTO P1 软件控制面。自动化任务可以使用同一交易日内有效、最长 720 分钟的结构化策略许可，不再受 MANUAL_LIVE 1–60 分钟人工时间授权的限制。

## Highlights

- readiness、许可状态、授权、恢复和撤销工具；
- 账户、证券、动作、策略/规则版本、交易窗口和 sizing 类型绑定；
- 单笔与会话金额、订单数、频率、并发、持仓敞口、账户回撤和连续失败守卫；
- Worker 原子预算预留；
- QMT Adapter 独立复核策略哈希和授权代次；
- 健康、回撤与连续失败触发自动暂停；
- 信用自动交易默认关闭。

## MANUAL_LIVE 与 LIMITED_AUTO

- MANUAL_LIVE：逐笔审批，或对单一账户开放 1–60 分钟不限笔数时间授权；
- LIMITED_AUTO：最长 720 分钟但不能跨交易日，每笔必须落在预先批准的策略、证券、动作、窗口和预算边界内；
- 两者的每笔委托都必须重新同步、预览、通过硬风控并接受 QMT Adapter 最终检查。

## Upgrade notes

- 数据库 schema 从 v1 迁移到 v2；
- MCP 工具总数更新为 29；
- QMT Adapter 协议和配置字段已变化；
- 升级后必须重新安装 wheel，并把新版 qmt_adapter.py 和新增配置字段部署到 QMT；
- 账户绑定、QMT/券商构建和映射没有变化时，不需要仅因软件升级重新签名 Profile；
- 新环境和升级后的首次核对都应从 OBSERVE_ONLY 开始。

## Assets

- workbuddy_qmt_bridge-0.3.0-py3-none-any.whl
- workbuddy-qmt-bridge-0.3.0.zip
- workbuddy-qmt-bridge-0.3.0.zip.sha256
- SHA256SUMS.txt

发布者必须从同一次最终构建上传全部四个文件。使用者应以 SHA256SUMS.txt 和 ZIP 单独哈希核对下载内容。

## Validation

- 从源码构建 wheel 与安装 ZIP 成功；
- wheel 独立目标目录安装成功；
- 包版本、MIT License expression、项目 URL 和 5 个命令入口存在；
- 新旧 0.3.0 wheel 的 17 个 Python 源文件逐文件哈希一致；
- 7 项 P1 增量测试全部通过；
- ZIP 包含中文安装脚本、wheel、LICENSE、发布文档、验证说明、示例和内部 wheel 哈希。

目标 QMT、券商和真实资金账户的现场验收不包含在软件测试结论中。信用账户仍不能视为已开放。

## Known limits

- 一个账户同一时刻只允许一个活动策略许可；
- 许可不能跨交易日，最晚到当日 15:00；
- 当前 P1 主路径为股票限价委托；
- 不自动拆单、追单或尝试第二价格；
- SUBMIT_UNKNOWN 永不自动重发；
- 熔断恢复只能在本机完成；
- 许可撤销不会自动撤销已经进入券商系统的委托；
- 示例 Profile 均保持 verified=false，不能直接用于交易。
