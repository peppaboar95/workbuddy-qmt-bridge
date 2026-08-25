# 配置示例说明

本目录中的 JSON 是结构和默认值参考。首次安装时应优先运行 `首次安装与配置.cmd`，由向导在本机生成带绝对路径的配置；不要把另一台电脑的账户号、路径、密钥、令牌或已签名 Profile 直接复制过来。

## 文件用途

| 文件 | 用途 |
| --- | --- |
| `bridge.example.json` | Worker 全局、账户、数量规则和风控参数的完整示例 |
| `qmt_adapter.stock.example.json` | 普通账户 QMT Adapter 配置结构参考 |
| `qmt_adapter.credit.example.json` | 信用账户 QMT Adapter 配置结构参考 |
| `qmt_profile.unsigned.example.json` | 普通账户未签名 Profile 结构参考 |
| `qmt_profile.credit.unsigned.example.json` | 信用账户未签名 Profile 结构参考 |
| `workbuddy.mcp.example.json` | WorkBuddy MCP 手工配置示例 |
| `workbuddy.mcp.example.README.md` | MCP 参数和合并方式说明 |

## bridge.example.json

将它作为手工配置起点时，复制为运行目录中的 `config/bridge.json`。相对路径以 `bridge.json` 所在目录为基准。

### 全局参数

| 参数 | 示例默认值 | 要求和作用 |
| --- | --- | --- |
| `data_dir` | `../data` | 数据库、队列、日志和运行状态目录；建议保留相对路径 |
| `host` | `127.0.0.1` | 只允许数字形式的本机回环地址；保持默认最稳妥 |
| `port` | `17642` | Worker 本机端口，范围 1–65535；MCP endpoint 必须使用同一端口 |
| `default_mode` | `OBSERVE_ONLY` | 仅在数据库首次创建时设置初始模式；以后在 `启动QMT桥接.cmd` 中选择本次模式 |
| `max_message_bytes` | `65536` | 单条消息上限，范围 1024–16777216；生成的 Adapter 会同步此值 |
| `key_file` | `../data/secrets/message_keys.json` | 本机消息签名密钥文件；不要手工共享或放入发布包 |
| `worker_token_file` | `../data/secrets/worker.token` | 本机 Worker API 令牌；不要写入 MCP 配置 |
| `accounts` | 普通启用、信用关闭 | 账户配置数组；`alias` 和 `adapter_instance` 必须唯一 |

全项目只使用四种运行模式：

| 模式 | 作用 |
| --- | --- |
| `OBSERVE_ONLY` | 查询、预览和 Adapter 空跑 ACK 闭环，不调用 `passorder` |
| `SIM_SIGNAL` | 连接已确认的模拟柜台，可调用 `passorder` |
| `MANUAL_LIVE` | 人工实盘，额外要求逐笔授权，或 1–60 分钟的账户级不限笔数授权；可由高风险 MCP 工具完成 |
| `LIMITED_AUTO` | P1 有限自动交易，额外要求签名策略许可、同日时段和多维额度；无许可或健康异常时失败关闭 |

熔断是独立安全状态，不是第五种模式。Worker、MCP 请求和 QMT Adapter 的模式必须一致。

### 每个账户的参数

| 参数 | 示例默认值 | 要求和作用 |
| --- | --- | --- |
| `alias` | `main_stock` / `main_credit` | Bridge 内部账户别名，只能使用安全的 ASCII 字母、数字、点、下划线和连字符 |
| `account_type` | `STOCK` / `CREDIT` | 普通或信用账户类型 |
| `adapter_instance` | `qmt_stock_01` / `qmt_credit_01` | QMT Adapter 实例名，所有账户之间不得重复 |
| `enabled` | 普通 `true`、信用 `false` | 是否启用账户；信用账户默认不启用 |
| `lot_size` | `100` | 整手单位；数量规则仍由 `order_volume_rules` 单独决定 |
| `odd_lot_sell_allowed` | `true` | 持仓尾数不足最低卖出数量时允许一次全部卖出；不允许 0 股委托 |
| `instrument_allowlist` | `[]` | 空数组表示不额外限制；非空时只允许列出的 6 位证券代码，可带 `.SH`、`.SZ`、`.BJ` |
| `limited_auto_credit_enabled` | `false` | 是否允许为信用账户创建 P1 自动许可；必须完成目标柜台信用 P0 后才可改为 `true` |
| `credit_action_mapping.buy` | `MARGIN_BUY` | 信用通用买入只执行融资买入这一种动作，不做第二次尝试 |
| `credit_action_mapping.sell` | `COLLATERAL_SELL` | 信用通用卖出只执行担保品卖出这一种动作，不做第二次尝试 |

### 下单数量规则

| 适用证券 | 最低买入 | 最低卖出 |
| --- | ---: | ---: |
| 科创板 `688`、`689` 开头 | 200 股 | 200 股 |
| 其他证券（包括创业板） | 100 股 | 100 股 |

数量必须大于 0。卖出时，如果当前可卖持仓本身不足对应最低卖出数量，且 `odd_lot_sell_allowed=true`，允许把该不足部分一次全部卖出。

### 风控参数

| 参数 | 普通账户 | 信用账户 | 范围和作用 |
| --- | ---: | ---: | --- |
| `max_order_notional` | 200000 | 100000 | 单笔最大名义金额，大于 0 |
| `max_order_volume` | 500000 | 500000 | 单笔最大股数，1–2147483647；名义金额上限仍会同时生效 |
| `max_daily_notional` | 2000000 | 1000000 | 单日累计名义金额，大于 0 |
| `min_maintenance_ratio` | 1.5 | 1.5 | 信用维持担保比例下限；普通账户保留默认值但不参与普通交易校验 |
| `max_snapshot_age_seconds` | 90 | 90 | 账户和持仓快照最大允许年龄，1–3600 秒 |
| `max_quote_age_seconds` | 30 | 30 | 行情和价格保护允许的最大行情年龄，1–60 秒；生成 Adapter 时同步为 `price_guard_max_quote_age_seconds` |
| `max_credit_snapshot_age_seconds` | 600 | 600 | 信用专用快照最大允许年龄，1–86400 秒 |
| `preview_ttl_seconds` | 120 | 120 | 预览结果有效期，1–3600 秒 |
| `command_ttl_seconds` | 60 | 60 | 下发命令有效期，1–3600 秒 |
| `credit_query_cooldown_seconds` | 180 | 180 | 信用查询冷却时间，1–86400 秒 |
| `max_auto_authorization_minutes` | 720 | 720 | 自动许可最长申请分钟数；实际仍截断到当天 15:00 |
| `max_auto_session_notional` | 2000000 | 1000000 | 单个自动许可累计名义金额硬上限，同时受 `max_daily_notional` 限制 |
| `max_auto_orders` | 1000 | 500 | 单个许可最多提交订单数 |
| `min_auto_order_interval_seconds` | 1 | 1 | 自动订单最短间隔；许可只能设置得更慢，不能更快 |
| `max_auto_concurrent_orders` | 20 | 10 | 账户活动委托并发硬上限 |
| `max_auto_symbol_position_notional` | 500000 | 250000 | 自动订单允许增加到的单标的持仓市值硬上限 |
| `max_auto_account_drawdown` | 100000 | 50000 | 相对许可创建时账户总资产的最大回撤金额 |
| `auto_heartbeat_max_age_seconds` | 15 | 15 | 自动下单允许的 Adapter 心跳最大年龄 |
| `auto_max_queue_depth` | 20 | 20 | 自动下单允许的待处理命令队列上限；死信始终要求暂停 |

`0.2.1` 适度放宽了五个时效默认值；`0.2.2` 调整百万级账户的通用额度；`0.2.3` 修复快照 UUID 和盯市字段轮换造成的 `SNAPSHOT_CHANGED` 误拒绝；`0.2.4` 增加逐笔 MCP 授权；`0.2.5` 增加 1–60 分钟人工时间授权。`0.3.0` 增加 P1 `LIMITED_AUTO`：许可最长申请 720 分钟但不跨当日收盘，并绑定账户、标的、动作、策略版本、时段、订单/金额/频率/并发/持仓/回撤/连续失败限制。MCP 仍不能切换模式或解除熔断；Profile、价格笼子、资金/可卖量检查和熔断规则没有关闭。

## QMT Adapter 示例

正常情况下不要手工从示例创建 Adapter 配置。运行 setup 后使用：

```text
<runtime>\qmt_ready\<账户别名>\qmt_adapter.json
```

生成文件中的 `data_dir`、`key_file`、`mapping_profile` 都是当前电脑绝对路径；`qmt_account_id` 必须替换为当前 QMT 的完整账户号。`qmt_mode` 初始固定为 `OBSERVE_ONLY`，`p0_probe_enabled` 初始为 `false`。`expected_profile_id`、`expected_qmt_build`、`expected_broker_build` 必须与验证并签名后的 Profile 完全一致。

`adapter_max_volume`、`adapter_max_notional`、`price_guard_max_quote_age_seconds`、信用快照和信用查询参数来自 `bridge.json`。P1 还会同步 `adapter_max_auto_*`、`adapter_min_auto_order_interval_seconds` 和 `limited_auto_credit_enabled`，让 Adapter 独立拒绝超过本地硬上限的签名策略。修改 Bridge 参数后应重新运行 setup 生成 QMT 文件并重新部署，`doctor` 会检查这些同步项。

`order_status_map` 已给出项目默认的 QMT 状态码映射；只有现场版本的状态码不同并经过验证时才修改。`field_map`、`credit_field_map` 和 `eligibility_allowed_values` 只应填入已在当前 QMT/券商组合中完成 P0 验证的结果。

## 未签名 Profile 示例

普通和信用账户必须使用各自的 Profile，且以下绑定字段必须与对应 Adapter 完全一致：

- `account_type`、`strategy_name`；
- `adapter_binding.account_alias`；
- `adapter_binding.account_type`；
- `adapter_binding.adapter_instance`；
- `adapter_binding.qmt_account_id`；
- `adapter_binding.strategy_name`。

示例保留 `verified=false` 和空 `signature`，不能据此开放报单。普通证券账户示例预填了完成 P0 后可采用的单账户、按股数、限价买卖候选映射：买入 `23/1101/11`，卖出 `24/1101/11`；这些值不覆盖市价、信用、基金、期货或其他动作，也不能代替目标 QMT/柜台的现场验收。

完成本机 P0 验证后，填写实际 `profile_id`、`qmt_build`、`broker_build` 和绑定信息，将 `verified` 改为 `true`，把前三个值逐字同步到 Adapter 的三个 `expected_*` 字段，最后使用本机消息密钥签名。签名后再修改任何 Profile 字段都会使签名失效；不要复制其他电脑或其他账户的签名 Profile。

## 修改后检查

保存 JSON 后运行桌面的 `查看QMT桥接状态.cmd`。状态异常时脚本会自动执行诊断；也可以在命令行运行：

```text
workbuddy-qmt doctor --config "<本机实际 runtime>\config\bridge.json"
```

任何未知参数、错误类型、越界数值、非本机监听地址或 Adapter/Profile 不同步都会按失败关闭处理。
