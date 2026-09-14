# WorkBuddy-QMT Bridge

WorkBuddy/MCP 与大 QMT 内置 Python 之间的本机、失败关闭型交易桥接器。

> **重要风险提示**
>
> 本项目能够触发模拟或真实资金账户的委托，不构成投资建议，也不保证盈利。默认模式为 OBSERVE_ONLY。任何真实资金部署都必须先完成目标 QMT 版本、券商柜台、账户字段、委托回报、撤单与恢复流程的现场验收。仓库中的示例映射不能直接用于实盘。

## 5 分钟只读上手

首次目标只有一个：让 WorkBuddy 能查询 QMT，同时保持 `OBSERVE_ONLY`，不实际报单。

1. 从 [GitHub Releases](https://github.com/peppaboar95/workbuddy-qmt-bridge/releases/latest) 下载完整 ZIP 并解压到独立目录；
2. 双击 `安装、升级或修复.cmd`，安装器会自动校验 wheel 哈希并启动中文配置向导；
3. 按自动打开的账户目录中的 `部署说明.txt`，把 `qmt_adapter.py` 放入对应的大 QMT 策略并启动；
4. 双击桌面的 `启动QMT桥接.cmd`，直接按 Enter 使用安全默认模式 `OBSERVE_ONLY`；
5. 双击桌面的 `查看QMT桥接状态.cmd`；它会先验证完整连接，再显示当前状态。所有项目显示“完成”后，重启 WorkBuddy 并调用 `qmt_health`。

完整图文步骤见 [快速开始](docs/QUICKSTART.zh-CN.md)。首次只读连接不需要签名 Profile，也不需要理解人工实盘或有限自动交易。

## 当前版本

- 版本：0.3.5
- 阶段：P1 LIMITED_AUTO 软件控制面完成
- MCP 工具：31 个
- Python：Worker 需要 3.10 或更高版本
- QMT Adapter：兼容 QMT 内置 Python 3.6 环境
- 运行边界：仅绑定 127.0.0.1，不依赖 xtquant

项目主页：https://github.com/peppaboar95/workbuddy-qmt-bridge

“P1 完成”只表示软件侧的结构化授权、额度预留、状态机、熔断和 Adapter 复核已经实现并通过本地测试，不表示任意真实资金柜台已经验收。

## 四种模式

| 模式 | 是否可能调用 QMT 报单 | 授权要求 |
| --- | --- | --- |
| OBSERVE_ONLY | 否 | 无；默认安全模式 |
| SIM_SIGNAL | 是，仅限已人工确认的模拟柜台 | 本机切换模式，映射签名并通过风控 |
| MANUAL_LIVE | 是 | 本机切换模式；逐笔审批或 1–60 分钟账户时间授权 |
| LIMITED_AUTO | 是，可在许可期内重复报单 | 本机切换模式；同一交易日结构化 P1 策略许可 |

SIM_SIGNAL 不是“只生成信号但不下单”。如果错误连接到真实资金柜台，它仍可能调用 passorder，因此必须人工核对账户和柜台。

## 安全设计

- preview_trade 与 submit_trade_intent 两阶段提交；
- 单笔优先使用 prepare_trade 合并快照刷新与预览；提交后使用一次 wait_trade_intent 有界等待；
- 提交时重新执行完整硬风控并比较风险决策指纹；
- Worker 与 QMT Adapter 双端检查账户、模式、签名、TTL 和授权代次；
- LIMITED_AUTO 许可绑定账户、证券、动作、策略/规则版本、交易窗口、下单类型及多层额度；
- 额度原子预留，限制单笔、累计金额、订单数、频率、并发、持仓敞口、回撤和连续失败；
- PRE_SUBMIT 或 SUBMIT_CALLED 状态不会自动重发，未知结果保持 SUBMIT_UNKNOWN；
- 熔断后只能在本机恢复，并安全回到 OBSERVE_ONLY；
- 信用自动交易默认关闭，新信用负债需要额外显式许可和现场验收。

## 安装

普通用户优先从 [GitHub Releases](https://github.com/peppaboar95/workbuddy-qmt-bridge/releases/latest) 下载完整 ZIP。安装器会自动核验 ZIP 内 wheel 的 SHA-256：

- workbuddy-qmt-bridge-0.3.5.zip
- workbuddy-qmt-bridge-0.3.5.zip.sha256
- SHA256SUMS.txt

完整解压后双击 `安装、升级或修复.cmd`。旧名称 `首次安装与配置.cmd` 仍作为兼容入口。需要手工安装 wheel 时，下载：

- workbuddy_qmt_bridge-0.3.5-py3-none-any.whl

Windows PowerShell：

    python -m pip install .\workbuddy_qmt_bridge-0.3.5-py3-none-any.whl
    workbuddy-qmt setup

从 0.3.2 开始，重复运行 setup 或使用 `setup --force` 都会保留已有 `qmt_profile.json`。有意重置时必须指定账户，并提供专用确认词：

    workbuddy-qmt setup --reset-profile main_stock --confirm-reset-profile RESET-QMT-PROFILE

从源码开发安装：

    git clone https://github.com/peppaboar95/workbuddy-qmt-bridge.git
    cd workbuddy-qmt-bridge
    python -m pip install -e .

## 从只读到交易

1. 运行 setup，创建本机配置、密钥、Worker 令牌和账户隔离目录；
2. 将生成的 qmt_adapter.py 与 qmt_adapter.json 部署到对应 QMT 策略实例；
3. 保持 OBSERVE_ONLY，完成心跳、账户、持仓、委托、成交和行情字段采集；
4. 在目标模拟柜台完成 P0 映射验证并签名 Profile；
5. 在 SIM_SIGNAL 完成最小数量、报单、成交、撤单、重启与未知结果恢复校对；
6. 真实资金场景先使用 MANUAL_LIVE 小额验收；
7. 只有 readiness 无阻断且策略边界明确时，才创建 LIMITED_AUTO P1 许可。

MANUAL_LIVE 的 1–60 分钟时间授权只免除逐笔人工审批；每笔仍必须重新同步、预览并通过硬风控。LIMITED_AUTO 的许可最长 720 分钟，但不能跨交易日，并受完整策略边界和累计预算约束。

## 低延迟调用建议

- 单笔交易优先调用 `prepare_trade`，它会把账户、持仓、委托、成交和目标行情合并成一次同步，等待新快照后直接返回预览；它不会提交订单；
- 多标的交易先用一次 `request_sync`，把同一账户的全部目标放入 `symbols`，随后复用账户和持仓快照逐笔 `preview_trade`，不要按标的重复同步；
- `submit_trade_intent` 返回 `QUEUED` 后，只调用一次 `wait_trade_intent`。如果等待超时，稍后按 `intent_id` 查询，绝不重新提交预览；
- 新生成 Adapter 的命令轮询为 500ms，Worker 回报摄取为 250ms。已有部署必须重新生成并部署 `qmt_adapter.py`/`qmt_adapter.json` 后才会使用新的 Adapter 周期；
- Adapter 每 5 秒刷新账户和持仓；委托/成交由 QMT 回调实时更新，并每 30 秒完整对账兜底。启动、`prepare_trade` 和显式 `request_sync` 仍会刷新委托/成交；
- 上述优化不减少预览、确认、提交时硬风控或 Adapter 报单前复核。

## LIMITED_AUTO P1 流程

1. 在本机把 Worker 和对应 Adapter 切换为 LIMITED_AUTO；
2. 调用 check_limited_auto_readiness，确认队列、快照、心跳、死信和 SUBMIT_UNKNOWN 均无阻断；
3. 向操作者展示完整许可策略，并在明确确认后调用 authorize_limited_auto；
4. 自动任务对每笔交易继续执行同步、预览、提交；
5. 使用 get_limited_auto_status 观察剩余时间、订单数、金额预算和暂停原因；
6. 健康、回撤或连续失败守卫触发时自动暂停；条件恢复后只能显式 resume_limited_auto；
7. 结束时调用 revoke_limited_auto。撤销许可不会自动撤销券商侧已有委托。

## 已验证与未验证

本版本的本地增量测试覆盖：

- schema v1 到 v2 迁移；
- 结构化 P1 许可；
- 预览、提交与额度原子占用；
- 策略版本绑定；
- 暂停、恢复与授权代次轮换；
- Adapter 防篡改复核。

仍需按目标环境完成：

- 真实 QMT 与券商构建能力矩阵；
- 真实资金柜台的委托、成交、备注和撤单回报；
- Windows ACL、备份、恢复和 SUBMIT_UNKNOWN 演练；
- 信用账户六类动作、字段量纲、资格、额度与负债规则；
- 小额人工实盘验收。

## 数据与隐私

runtime、数据库、WAL/SHM、日志、队列、执行日志、签名 Profile、密钥、Worker 令牌、账户号和本机 MCP 配置都不得提交到仓库或附加到 Issue。诊断材料必须先脱敏。

## 文档

- 快速开始：docs/QUICKSTART.zh-CN.md
- 日常使用：docs/DAILY-USE.zh-CN.md
- 升级、备份与恢复：docs/UPGRADE-RECOVERY.zh-CN.md
- P0/P1 高级配置：docs/P0-P1-ADVANCED.zh-CN.md
- 排障与脱敏诊断：docs/TROUBLESHOOTING.zh-CN.md
- 兼容性矩阵：docs/COMPATIBILITY.zh-CN.md
- **在线 API 参考（GitHub Pages）**：https://peppaboar95.github.io/workbuddy-qmt-bridge/
  - 单文件离线 HTML，零外部依赖；当前源码文档覆盖全部 31 个 MCP 工具、错误码、风控原因码与配置参考。可另存为 `.html` 本地打开。
- 发布与安装：docs/README-RELEASE.zh-CN.md
- P1 验证说明：docs/P1-VALIDATION.zh-CN.md
- 配置示例：examples/README.md
- 安全政策：SECURITY.md

## License

本项目采用 MIT License，详见 LICENSE。
