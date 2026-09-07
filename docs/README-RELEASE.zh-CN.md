# WorkBuddy-QMT Bridge 发布包使用方法和运行过程

本文面向从发布 ZIP 安装的 Windows 用户。源码开发、接口字段和完整风控设计请查看项目 `README.md`。

当前发布版本为 `0.3.3`。这是 Profile 升级保护与非交互 Windows 输出兼容修复版本，不改变交易、风控、数据库 schema、配置或 Adapter 协议。

## 一、安装前准备

需要提前安装：

- Windows 10/11；
- Python 3.10 或更高版本，并在安装 Python 时启用 `Add Python to PATH`；
- Tencent WorkBuddy；
- 大 QMT 客户端。Worker 和大 QMT 默认在同一台电脑、同一个 Windows 用户下运行。

还需要准备启用账户的完整 QMT 账户号。新环境默认只启用普通证券账户，信用账户默认关闭。

发布包不包含 Python、QMT、账户号、密钥、令牌、数据库或任何既有 `runtime`。不要从其他电脑复制密钥、令牌或签名 Profile。

## 二、发布包包含什么

解压 ZIP 后可以看到：

- `首次安装与配置.cmd`：首次安装和配置入口；
- `workbuddy_qmt_bridge-<版本>-py3-none-any.whl`：Python 安装包；
- `RELEASE-v<版本>.md`：当前版本变更和升级说明；
- `README-RELEASE.zh-CN.md`：本文；
- `P1-VALIDATION.zh-CN.md`：0.3.0 软件验证范围、测试结果和仍需现场验收的边界；
- `SHA256SUMS.txt`：发布 ZIP 内 wheel 的 SHA-256；ZIP 自身的校验值位于同目录 `.zip.sha256` 文件；
- `examples\`：Bridge、普通/信用 QMT Adapter、未签名 Profile 和 MCP 的完整示例及参数说明。

必须先完整解压 ZIP，再运行安装脚本。不要直接在压缩软件预览窗口中双击脚本，也不要只复制其中一个文件。

## 三、首次安装与配置

双击 `首次安装与配置.cmd`。脚本会依次完成：

1. 检查 Python 3.10+；
2. 使用当前用户权限安装同目录 wheel；
3. 启动五步配置向导；
4. 显示仍需手工完成的 QMT 和 WorkBuddy 操作。

五步向导中的用户操作如下：

### 1. 运行数据目录

直接按 Enter 使用默认目录：

```text
%LOCALAPPDATA%\WorkBuddyQMTBridge\runtime
```

这里保存 `bridge.json`、本机密钥、Worker 令牌、数据库、日志、消息队列和 QMT 生成文件。路径可以与开发电脑不同，生成文件会写入当前电脑的绝对路径。路径必须能用 GBK 表示，不能包含双引号。

已有运行目录会被复用，不会静默覆盖账户、风险参数、密钥或数据库。

### 2. WorkBuddy MCP 配置

直接按 Enter 使用：

```text
%USERPROFILE%\.workbuddy\mcp.json
```

向导只合并或更新 `mcpServers.qmt-bridge`，保留其他 MCP 服务；修改前会创建带时间戳的备份。如果原文件不是有效 JSON，向导会停止而不会覆盖。

### 3. 账户和 QMT 文件

- 普通证券账户默认启用；
- 信用账户默认不启用；
- 输入 `y`/`n` 选择，或直接按 Enter 接受括号中的默认值；
- 为每个启用账户输入完整 QMT 账户号；如果暂时留空，该账户不会生成 QMT 文件，可稍后重新运行向导。

成功后会生成：

```text
<runtime>\qmt_ready\<账户别名>\qmt_adapter.py
<runtime>\qmt_ready\<账户别名>\qmt_adapter.json
<runtime>\qmt_ready\<账户别名>\qmt_profile.json
```

初始状态固定为：

- Worker 和 QMT Adapter：`OBSERVE_ONLY`；
- Profile：`verified=false`、没有可执行数字映射。

### 已有 Profile 的升级保护

从 0.3.2 开始，重复运行安装向导、`setup` 或 `setup --force` 都会原样保留已有 `qmt_profile.json`。通用的“备份后更新”确认只适用于变化的 Adapter/配置文件，不能再重置 Profile。

只有确实需要废弃现有 P0 映射时，才可在停止 Worker 和对应 QMT 策略后运行以下命令。账户别名必须明确指定；重置前会创建带时间戳的 Profile 备份：

```powershell
workbuddy-qmt setup `
  --reset-profile main_stock `
  --confirm-reset-profile RESET-QMT-PROFILE
```

`--reset-profile` 可重复指定多个已启用账户。缺少专用确认词、确认词不匹配、账户未知或账户未启用时，命令都会在修改 Profile 前失败。

### 4. MCP 和启动器配置

向导备份并合并 WorkBuddy MCP 配置，并把非敏感启动信息保存到：

```text
%LOCALAPPDATA%\WorkBuddyQMTBridge\launcher.json
```

该文件不保存 QMT 账户号、消息密钥或 Worker 令牌。

### 5. 桌面入口

建议接受默认选择，创建：

- `启动QMT桥接.cmd`；
- `查看QMT桥接状态.cmd`。

状态脚本在状态正常时直接给出结论；状态异常时自动继续检查 Python、配置、MCP、端口、QMT 文件和 Adapter/Profile 同步情况。

## 四、首次配置后必须手工完成

安装脚本不会启动 QMT、Worker 或 WorkBuddy，也不会签名 Profile 或开放交易。

1. 重启 WorkBuddy，让新的 `qmt-bridge` MCP 配置生效；
2. 打开 `<runtime>\qmt_ready\main_stock\`；
3. 在大 QMT 模型交易中创建一个独立策略实例；
4. 用生成的 `qmt_adapter.py` 作为策略源码；
5. 确认策略绑定的是刚才填写的账户；
6. 保持 `qmt_adapter.json` 的 `qmt_mode=OBSERVE_ONLY`；
7. 手工启动 QMT 策略实例；
8. 双击桌面的 `启动QMT桥接.cmd`，首次选择 `OBSERVE_ONLY`；
9. 双击 `查看QMT桥接状态.cmd`，确认 Worker 和启用账户的 Adapter 都为正常/就绪。

大 QMT 控制台默认每 30 秒打印一行 `[WorkBuddy-QMT][HEARTBEAT]`。能看到心跳只表示 Adapter 正常运行，不代表交易模式已经开放。

## 五、日常运行方法

推荐启动顺序：

1. 打开并登录大 QMT；
2. 启动对应的 QMT 策略实例；
3. 双击桌面的 `启动QMT桥接.cmd`；
4. 选择本次统一运行模式；不确定时直接按 Enter 使用 `OBSERVE_ONLY`；
5. 保持 Worker 控制台窗口打开；
6. 打开或重启 WorkBuddy；
7. 在 WorkBuddy 中先查询 `qmt_health`、账户和持仓，再进行后续操作。

关闭 Worker 控制台或按 `Ctrl+C` 会安全停止 Worker。停止 Worker 不会自动关闭 QMT 或 WorkBuddy，也不会自动撤销已经提交给柜台的委托。

Worker 日志写入：

```text
<runtime>\data\logs\worker-YYYY-MM-DD.log
```

## 六、启动时的模式选择

| 模式 | 含义 | 是否可能调用 `passorder` |
| --- | --- | --- |
| `OBSERVE_ONLY` | 查询、预览和空跑 ACK 闭环，不实际报单 | 否 |
| `SIM_SIGNAL` | 仅用于已验收的模拟账户/模拟柜台 | 是 |
| `MANUAL_LIVE` | 人工实盘模式，支持逐笔审批或限时不限笔数授权；均可由高风险 MCP 工具完成 | 是 |
| `LIMITED_AUTO` | P1 有限自动交易；还必须有签名、限时、限范围的本机策略许可 | 是；无许可或健康异常时失败关闭 |

`SIM_SIGNAL`、`MANUAL_LIVE` 和 `LIMITED_AUTO` 都需要再次输入完整模式名确认。Worker、MCP 请求和 Adapter 必须使用相同模式名称。模式名称不能识别当前连接的是模拟柜台还是真实资金柜台，操作者必须在 QMT 中自行核对账户和柜台。

`0.3.0` 已提供 `LIMITED_AUTO` 的 P1 软件控制面。它不会随模式切换自动开放：仍须先完成目标柜台 P0、同步 Worker/Adapter 模式，通过健康与对账检查，再由 WorkBuddy/MCP 创建明确的策略许可。信用账户自动交易仍默认关闭；没有完成目标 QMT/券商现场验收时不得开启。

### 已运行环境如何切换模式

不要在 Worker 和 QMT Adapter 正在处理请求时热切换模式。推荐按以下顺序操作：

1. 停止在 WorkBuddy 中发起新的 QMT 请求，并确认没有正在提交的交易意图；
2. 在 Worker 控制台按 `Ctrl+C`，等待 Worker 正常停止；
3. 停止所有已启用账户对应的 QMT 策略实例；
4. 分别打开每个启用账户目录中的 `<runtime>\qmt_ready\<账户别名>\qmt_adapter.json`，把 `qmt_mode` 改为目标模式；
5. 确认所有启用账户使用同一个模式，然后重新启动对应的 QMT 策略实例；
6. 双击桌面的 `启动QMT桥接.cmd`，选择与 Adapter 完全一致的模式；
7. `SIM_SIGNAL`、`MANUAL_LIVE` 或 `LIMITED_AUTO` 还需要按提示再次输入完整模式名确认；
8. 双击 `查看QMT桥接状态.cmd`，确认 Worker 与所有启用账户的 Adapter 均为正常/就绪且模式一致；
9. 回到 WorkBuddy，先调用 `qmt_health` 并重新同步账户、持仓和行情，再继续后续操作。

启动菜单中的选择为：

```text
1 或直接按 Enter = OBSERVE_ONLY
2                  = SIM_SIGNAL
3                  = MANUAL_LIVE
4                  = LIMITED_AUTO
```

如果 Worker 已经在运行，再次双击启动脚本只会报告已有 Worker，不会替正在运行的 Worker 改模式。必须先停止旧 Worker，再重新启动并选择模式。

`bridge.json` 中的 `default_mode` 只在数据库首次创建时设置初始模式，不是当前运行模式；修改它不会切换已有环境的模式。当前 Worker 模式保存在本机状态数据库中，启动菜单会将本次选择写入该状态。

也可以在停止 Worker 后使用本机控制台修改 Worker 模式：

```powershell
# 切回观察模式，不需要额外确认
python -m workbuddy_qmt.console --config "<runtime>\config\bridge.json" `
  set-mode OBSERVE_ONLY

# 切换到已验收的模拟柜台；模式名和确认文字必须完全一致
python -m workbuddy_qmt.console --config "<runtime>\config\bridge.json" `
  set-mode SIM_SIGNAL --confirm SIM_SIGNAL

# 切换到人工实盘模式
python -m workbuddy_qmt.console --config "<runtime>\config\bridge.json" `
  set-mode MANUAL_LIVE --confirm MANUAL_LIVE

# 切换到 P1 有限自动模式；这里只切模式，不会创建策略许可
python -m workbuddy_qmt.console --config "<runtime>\config\bridge.json" `
  set-mode LIMITED_AUTO --confirm LIMITED_AUTO
```

控制台命令只修改 Worker 的本机状态，不会自动修改 `qmt_adapter.json`，也不会重启 QMT 策略。仍须按上述步骤同步 Adapter 并完成状态检查。

如果桥接处于熔断状态，系统会拒绝直接改模式。确认熔断原因已经排除后，可以执行：

```powershell
python -m workbuddy_qmt.console --config "<runtime>\config\bridge.json" `
  clear-halt --reason "已排除的具体原因" --confirm CLEAR-HALT
```

解除熔断后模式固定回到 `OBSERVE_ONLY`。先让 Worker 和所有 Adapter 在 `OBSERVE_ONLY` 下恢复正常，再考虑切换到其他模式。

切换到非观察模式还必须满足各自的额外条件：

- `SIM_SIGNAL`：只用于已经完成 P0 验收的模拟账户/模拟柜台；Profile 必须验证、绑定并使用本机密钥签名；
- `MANUAL_LIVE`：除已验证 Profile 外，还需要逐笔审批或限时不限笔数授权；0.2.4 的 `authorize_manual_trade` 提供逐笔授权，0.2.5 的 `authorize_manual_session` 提供账户级时间授权，但 Worker 模式仍必须先在本机切换；
- `LIMITED_AUTO`：还需要 `authorize_limited_auto` 创建的 P1 签名策略许可；许可绑定交易日、时段、账户、证券、动作、策略版本和全部自动额度，健康异常时自动暂停；
- 任一条件缺失、Worker/Adapter/MCP 请求模式不一致或 Profile 不匹配时，系统都会失败关闭，不应通过修改数据库、队列或签名文件绕过检查。

### WorkBuddy/MCP 调用较慢时的时效参数

从 `0.2.1` 起，新安装环境使用一组更适合 WorkBuddy 连续调用 MCP 和其他工具的默认时效参数：

| 参数 | 0.2.0 默认值 | 0.2.1 默认值 | 作用 |
| --- | ---: | ---: | --- |
| `max_snapshot_age_seconds` | 30 | 90 | 账户与持仓快照允许的最大年龄 |
| `max_quote_age_seconds` | 10 | 30 | 预览、提交和 Adapter 最终价格保护允许的最大行情年龄 |
| `max_credit_snapshot_age_seconds` | 300 | 600 | 信用资产、资格、负债和额度快照允许的最大年龄 |
| `preview_ttl_seconds` | 30 | 120 | 交易预览可被审批和提交的时间 |
| `command_ttl_seconds` | 30 | 60 | Worker 已签名命令到达 Adapter 前的有效时间 |

`0.2.1` 的这次时效调整只减少多工具调用造成的 `*_STALE` 和 `PREVIEW_EXPIRED`；它没有关闭 Profile、模式一致性、价格笼子、LIVE 授权、逐笔/限时授权边界、本地熔断或防重复提交。QMT Adapter 在调用 `passorder` 前仍会重新读取行情并复核账户、持仓、价格和数量。

已有 runtime 不会被升级脚本静默改写。需要采用新值时，先停止 Worker 和 QMT 策略，备份 `bridge.json` 与 QMT bundle，再修改每个账户的 `risk_limits`；同时把对应 `qmt_adapter.json` 的 `price_guard_max_quote_age_seconds` 改为 `30`，信用账户还要把 `credit_snapshot_max_age_seconds` 改为 `600`。保存后重新部署 `qmt_adapter.py`、重启 QMT 策略并运行 `doctor`。只修改这些时效字段不会使已经签名的 Profile 失效；不要顺手修改 Profile 内容。

即使使用放宽后的值，执行交易前仍应先用 `request_sync` 刷新账户、持仓和目标标的 `QUOTE`，再进行 `preview_trade` 和 `submit_trade_intent`。如果行情源中断或 Adapter 报行情过期，应恢复行情并重新同步，不要继续增大阈值或重复提交旧意图。

### 0.2.3 的预览提交一致性修复

`0.2.3` 将提交一致性从“所有快照 UUID 必须完全不变”调整为“风险决策必须保持一致”。提交时仍会使用最新账户、持仓、行情、活动委托和信用数据重新执行完整硬风控，并比较最终动作、价格、数量、可用资金、目标证券持仓、价格保护结果、活动委托、日内额度及信用风险字段。仅账户/持仓/行情快照重新编号，或行情波动只改变市值、总资产、持仓市值和现价而没有改变最终委托及风险输入时，不再返回 `SNAPSHOT_CHANGED`。

这不是关闭二阶段校验：可用资金、目标证券总量/可用量/冻结量、最终限价或数量、活动委托、日内已用额度、信用维护担保比例/资格/额度/负债任一实质变化，仍会要求重新预览；过期快照、过期行情和任何最新硬风控失败仍直接拒绝。升级前创建但尚未提交的旧预览使用旧指纹格式，升级后会安全失效，应重新同步和预览一次。

### 0.2.4 的 WorkBuddy/MCP 人工实盘授权

`0.2.4` 新增高风险副作用工具 `authorize_manual_trade`。在操作者已经从本机把 Worker 和 QMT Adapter 切到 `MANUAL_LIVE` 后，WorkBuddy 可以针对一个准确、未过期且最新硬风控仍通过的 `preview_id`，同时开启短时 LIVE、创建一次性审批，并取得可直接交给 `submit_trade_intent` 的 `approval_context`：

```json
{
  "preview_id": "preview_xxx",
  "live_minutes": 10,
  "approval_ttl_seconds": 30,
  "reason": "用户已在 WorkBuddy 核对账户、证券、数量和限价",
  "confirm": "AUTHORIZE-MANUAL-TRADE"
}
```

该工具被标记为 `destructiveHint=true`，能够为真实 QMT 报单打开授权条件。调用前必须向用户展示预览的账户别名、证券、方向、最终数量、最终限价、名义金额、价格保护调整和风险结果，并取得用户对这一笔预览的明确确认。MCP 仍不能切换 Worker/Adapter 模式、解除熔断、修改 Profile 或绕过最新风控；确认词错误、非 `MANUAL_LIVE` 模式、预览过期/已消费、风险输入变化都会拒绝。传统本机 `enable-live` 与 `approve-preview` 命令继续可用。

### 0.2.5 的限时不限笔数授权

`0.2.5` 新增高风险副作用工具 `authorize_manual_session`。操作者明确确认账户与授权时长后，WorkBuddy 可以为一个已经处于 `MANUAL_LIVE` 的账户开启 1–60 分钟的时间授权：

```json
{
  "account_alias": "main_stock",
  "minutes": 10,
  "reason": "用户确认未来 10 分钟允许 WorkBuddy 按策略连续下单",
  "confirm": "AUTHORIZE-TIMED-MANUAL-TRADING"
}
```

时间授权有效时，订单笔数不设上限，`submit_trade_intent` 不再要求逐笔传入 `approval_context`。这不等于绕过交易流程：每笔订单仍必须分别执行“同步账户/持仓/行情 → `preview_trade` → `submit_trade_intent`”，提交时仍复算最新硬风控并比较风险决策指纹；预览过期、资金/持仓/价格/活动委托等风险输入变化、额度超限、模式变化或熔断都会拒绝。

WorkBuddy 可用 `get_manual_authorization_status` 显示剩余秒数，并可随时撤销：

```json
{
  "account_alias": "main_stock",
  "reason": "用户要求停止连续下单",
  "confirm": "REVOKE-TIMED-MANUAL-TRADING"
}
```

撤销会同时终止 LIVE 窗口并使该账户尚未使用的逐笔审批过期。切换运行模式或调用 `halt_trading` 也会立即使所有时间授权失效；解除熔断后不会自动恢复。时间授权工具被标记为 `destructiveHint=true`，调用前必须让用户看到准确账户别名、持续时间以及“期间订单笔数不设上限”的提示并取得明确确认。

### 0.3.0 的 P1 有限自动交易

`0.3.0` 新增 5 个 `LIMITED_AUTO` 工具：

- `check_limited_auto_readiness`：刷新队列并检查模式、Adapter 心跳、快照、死信和 `SUBMIT_UNKNOWN`；
- `authorize_limited_auto`：创建同一交易日内有效的签名策略许可；
- `get_limited_auto_status`：查询策略、剩余秒数、剩余订单数、剩余金额和暂停原因；
- `resume_limited_auto`：排除故障后显式恢复暂停许可，同时轮换授权代次，使暂停前的旧命令继续无效；
- `revoke_limited_auto`：立即撤销许可；不会自动撤销已经提交给柜台的委托。

`minutes` 允许 1–720 分钟，默认 480 分钟，但实际到期时间会截断到当天 15:00，不能跨交易日。默认交易窗口为 `09:30–11:30` 和 `13:00–15:00`。因此盘中监控可以一次覆盖完整交易日，不需要每 60 分钟重新授权；午休、收盘、周末和许可外时段仍不能产生新自动订单。

授权示例：

```json
{
  "account_alias": "main_stock",
  "symbols": ["600000.SH", "000001.SZ"],
  "actions": ["BUY", "SELL"],
  "source_type": "intraday_monitor",
  "rule_set_id": "breakout-monitor",
  "rule_version": "2026-08-22-v1",
  "minutes": 480,
  "max_order_notional": 50000,
  "max_order_volume": 5000,
  "max_session_notional": 300000,
  "max_orders": 30,
  "min_order_interval_seconds": 10,
  "max_concurrent_orders": 2,
  "max_symbol_position_notional": 200000,
  "max_account_drawdown": 20000,
  "max_consecutive_failures": 2,
  "trading_windows": [
    {"start": "09:30", "end": "11:30"},
    {"start": "13:00", "end": "15:00"}
  ],
  "allowed_sizing_types": ["FIXED_VOLUME", "FIXED_NOTIONAL"],
  "allow_credit_new_debt": false,
  "reason": "用户确认当日盘中监控策略及全部额度",
  "confirm": "AUTHORIZE-LIMITED-AUTO-P1"
}
```

调用前必须向用户展示上述完整范围和上限。授权成功后，每笔订单仍必须使用完全相同的 `source.type`、`rule_set_id`、`rule_version`，并逐笔执行“同步 → `preview_trade` → `submit_trade_intent`”。Worker 会在提交事务中原子占用订单数和累计金额；Adapter 会独立复核签名策略哈希、授权代次、时段、标的、动作、策略版本、额度、频率、并发、持仓敞口和账户净值回撤。

出现 Adapter 离线/模式漂移、本地暂停、快照陈旧、死信、队列积压、`SUBMIT_UNKNOWN`、账户回撤超限或连续失败超限时，系统停止新自动订单并暂停许可。排除原因、重新同步并运行 `check_limited_auto_readiness` 后，必须使用 `RESUME-LIMITED-AUTO-P1` 明确恢复；不会静默自动恢复。连续失败达到上限时不能直接恢复，必须修正规则、更新 `rule_version` 并创建新许可。切换模式、熔断、撤销和跨日都会使许可失效。

当前每个账户同一时间只允许一个活动策略许可；创建新许可会撤销旧许可。需要并行多策略时应使用不同的、独立验收的账户别名和 Adapter 实例，不能共享额度。普通股票限价买卖是默认 P1 路径。信用账户必须在 Bridge 与 Adapter 同时设置 `limited_auto_credit_enabled=true`，完成信用字段/枚举/额度/负债最终校验后才能创建许可；`MARGIN_BUY` 和 `SHORT_SELL` 还必须显式设置 `allow_credit_new_debt=true`。

### 百万级账户的默认交易额度

从 `0.2.2` 起，新安装环境按照百万级账户规模同时放宽普通账户和信用账户的固定额度上限：

| 账户类型 | 单笔名义金额 `max_order_notional` | 单笔股数 `max_order_volume` | 单日累计金额 `max_daily_notional` |
| --- | ---: | ---: | ---: |
| 普通证券账户 | 200000 元 | 500000 股 | 2000000 元 |
| 信用账户 | 100000 元 | 500000 股 | 1000000 元 |

信用账户的金额上限继续低于普通账户，用于覆盖融资融券带来的额外杠杆和负债风险。`max_order_volume` 与 `max_order_notional` 会同时校验：即使股数未达到 50 万股，只要名义金额超过对应单笔上限仍会拒绝。单日上限统计当日成交、活动委托和新交易意图，不代表允许忽略可用资金、可卖数量或信用额度。

这些值是发布包的通用硬上限，不是按账户资产比例自动计算的仓位建议。资金规模、策略换手率或风险承受能力较低的用户应主动调低；需要更高额度时也应先完成独立风控评估。价格笼子、涨跌停、行情时效、账户资金、可卖量、信用维持担保比例、Profile、审批和熔断仍会继续生效。

已有 runtime 不会被升级程序静默覆盖。采用新额度时，应在停止 Worker 和 QMT 策略后修改每个账户 `bridge.json` 中的三个字段，并同步修改对应 `qmt_adapter.json` 的 `adapter_max_notional` 和 `adapter_max_volume`。普通账户同步为 `200000` 和 `500000`；信用账户同步为 `100000` 和 `500000`。这些额度字段不属于 Profile 签名内容，单独调整无需重签 Profile；保存后仍须重启 QMT 策略并运行 `doctor`。

## 七、从自然语言到 QMT 的运行过程

查询链路：

```text
WorkBuddy
  -> stdio MCP 前端
  -> 127.0.0.1 Worker（本机令牌验证）
  -> SQLite/本地签名队列
  -> QMT Embedded Adapter
  -> 大 QMT 账户和行情接口
  -> Adapter 回传签名事件
  -> Worker 入库
  -> WorkBuddy 获得结构化结果
```

交易链路：

1. WorkBuddy 先请求 QMT 同步账户、持仓和目标标的行情；
2. QMT Adapter 返回最新价、盘口、涨跌停、最小价位和动态价格笼子；
3. `preview_trade` 把自然语言意图转换为结构化动作、数量和限价；
4. Worker 检查零股、最小数量、资金/可卖量、单笔/单日限额、行情年龄、价格笼子及活动委托冲突；
5. `MANUAL_LIVE` 可使用 `authorize_manual_trade` 逐笔审批，或先用 `authorize_manual_session` 开启 1–60 分钟的账户级不限笔数授权；
6. `LIMITED_AUTO` 必须先通过 P1 readiness，再创建绑定当日时段、标的、动作、策略版本和额度的签名许可；
7. `submit_trade_intent` 再次比较风险决策和授权状态，并在同一数据库事务中占用自动许可额度；
8. 通过后，Worker 把带 HMAC 签名和 TTL 的唯一命令写入目标账户队列；
9. Adapter 重新检查账户、数量、资金、实时行情、涨跌停、价格笼子、本地授权及 P1 策略哈希；
10. Adapter 校验签名 Profile 与 QMT 构建、柜台构建、账户、策略和 Adapter 实例完全一致；
11. Adapter 先写入 `PRE_SUBMIT` 防重复日志，再调用一次 `passorder`；
12. 委托、成交或错误回调写回 Worker，WorkBuddy 通过查询工具取得最终状态。

一笔意图只生成一张委托。系统不自动拆单、不追单、不尝试第二价格，也不会在 `SUBMIT_UNKNOWN` 时自动重发。

## 八、开放模拟报单前的 P0 要求

`OBSERVE_ONLY` 正常不等于可以下单。切换 `SIM_SIGNAL` 前必须在目标 QMT 构建和目标模拟柜台完成 P0：

### 可以使用 AI Agent 更新、绑定和签名 Profile

每台新电脑生成的 `qmt_profile.json` 都故意保持未验证和未签名，不能直接运行 `SIM_SIGNAL` 或 `MANUAL_LIVE`。完成该电脑、该账户和该柜台的 P0 后，可以让具备本机文件与命令行权限的 AI Agent（例如 Codex）完成以下机械操作：

1. 读取 `<runtime>\config\bridge.json`、`qmt_profile.json` 和 `qmt_adapter.json`；
2. 根据已经确认的 P0 结果填写 `profile_id`、`qmt_build`、`broker_build`、`mappings` 和 `verified=true`；
3. 保留并核对 `adapter_binding` 中的账户别名、账户类型、Adapter 实例、完整 QMT 账户号和策略名；
4. 将 Profile 的三个标识逐字同步到 Adapter 的三个 `expected_*` 字段；
5. 使用本机 `workbuddy_qmt.console sign-qmt-profile` 命令和本机消息密钥签名；
6. 运行 `doctor`，确认 Profile 签名、Adapter 绑定和配置同步全部通过。

可直接向 AI Agent 提交类似下面的任务：

```text
P0 已在当前电脑、当前 QMT 构建和目标柜台闭环。
请读取本机 bridge.json、qmt_profile.json 和 qmt_adapter.json，
按照以下已确认的 profile_id、qmt_build、broker_build 和 mappings 更新 Profile，
同步 Adapter 的 expected_* 绑定，使用本机密钥执行 sign-qmt-profile，
最后运行 doctor。不要输出或复制账户号、worker.token、message_keys.json 或签名内容。
```

AI Agent 只能依据操作者提供或本机可验证的 P0 结果执行更新，不能自行猜测柜台构建、信用字段或交易枚举。不要把 `worker.token`、`message_keys.json`、完整账户号或已签名 Profile 上传给远程服务。签名必须在目标电脑本机完成；从另一台电脑复制来的签名 Profile 无效，也不应尝试复用。

1. 校验账户、持仓、委托、成交和错误回调字段；
2. 对每个允许动作验证 `opType/orderType/prType`；
3. 填写 `qmt_profile.json` 的 `profile_id`、`qmt_build`、`broker_build`、`mappings` 和 `verified=true`；
4. 把前三个值同步到 `qmt_adapter.json` 的 `expected_profile_id`、`expected_qmt_build`、`expected_broker_build`；
5. 保持生成的 `adapter_binding` 与当前账户、QMT 账号、Adapter 实例和策略名一致；
6. 使用本机配置和本机密钥签名 Profile；
7. 将 Adapter 改为 `SIM_SIGNAL`，重新部署脚本并重启 QMT 策略；
8. 最后才在 Worker 启动时选择 `SIM_SIGNAL`。

普通证券账户、单账户、按股数、限价买卖在完成目标柜台 P0 后，Profile 的映射结构如下：

```json
{
  "mappings": {
    "STOCK:STOCK:BUY:LIMIT": {
      "op_type": 23,
      "order_type": 1101,
      "price_type": 11
    },
    "STOCK:STOCK:SELL:LIMIT": {
      "op_type": 24,
      "order_type": 1101,
      "price_type": 11
    }
  }
}
```

这些数字只覆盖普通股票限价买卖，不代表市价、信用、基金、期货或其他动作已经开放。发布包中的未签名普通账户示例预填了这两个候选映射，但仍保持 `verified=false` 和空签名；只有操作者确认当前 QMT 构建、当前柜台及买卖回调已经完成 P0 后，才可将它们写入本机 Profile 并设置 `verified=true`。

`profile_id` 应是本账户和本次 P0 的唯一标识；`qmt_build` 与 `broker_build` 必须记录现场实际构建，且三个值必须逐字同步到 Adapter 的 `expected_profile_id`、`expected_qmt_build` 和 `expected_broker_build`。Adapter 在策略初始化时读取配置，因此修改三个绑定字段或 `qmt_mode` 后必须重启对应 QMT 策略实例。

签名命令示例（`python` 也可以替换为安装时使用的 `py -3`）：

```powershell
python -m workbuddy_qmt.console --config "<runtime>\config\bridge.json" `
  sign-qmt-profile "<runtime>\qmt_ready\main_stock\qmt_profile.json" `
  --confirm VERIFIED-PROFILE
```

签名必须是最后一步。签名后再修改 Profile 中的任何字段都会使签名失效，必须重新核对绑定并重新签名。已验证的普通账户 Profile 可以被 `SIM_SIGNAL` 和 `MANUAL_LIVE` 使用，但它本身不会跳过模式一致性、逐笔/限时授权、风险限制或本地熔断检查。

旧电脑、旧账户或旧构建的签名 Profile 不能直接复用。

## 九、常见问题

### 找不到 Python

安装 Python 3.10+，启用 `Add Python to PATH`，关闭安装窗口后重新双击脚本。

### 找不到 wheel

说明没有完整解压发布 ZIP，或安装脚本和 wheel 不在同一个目录。重新完整解压。

### MCP JSON 无效

向导不会覆盖无效文件。先修复 `%USERPROFILE%\.workbuddy\mcp.json` 的 JSON 语法，再重新运行安装脚本。

### Worker 端口冲突

运行桌面的 `查看QMT桥接状态.cmd`。关闭占用 `17642` 的旧 Worker 或其他程序后再启动。

### Adapter 显示离线

确认大 QMT 已登录、策略实例已启动、部署的是当前 `qmt_ready` 中的最新脚本，并检查大 QMT 控制台心跳。

### Adapter/Profile 不同步

重新运行首次配置可更新当前路径和风险参数对应的 Adapter/配置，同时保留现有 Profile。如果账户、构建或三个 `expected_*` 绑定确实发生变化，系统会失败关闭；此时应先核对变化原因。只有决定废弃旧 P0 映射时，才使用专用 Profile 重置命令，然后重新完成 P0、同步绑定并使用本机密钥签名。

### 路径不能用 GBK 表示

把运行目录改到仅包含 ASCII 或常用中文字符的路径，再重新配置。

## 十、升级与备份

### 什么情况下需要重新安装

这里的“安装”通常是覆盖升级 Python wheel，不是删除并重建整个系统。`首次安装与配置.cmd` 虽然名称中有“首次安装”，也可用于后续版本升级：它会执行用户级 `pip install --upgrade`，然后启动配置向导；向导会复用已有 Bridge 设置，生成文件发生变化时要求操作者确认，不会静默覆盖交易配置。

| 更新内容 | 是否运行安装脚本 | 更新后还要做什么 |
| --- | --- | --- |
| 只修改 `README`、说明、示例或发布 ZIP 包装 | 否 | 替换文档即可，Worker 和 WorkBuddy 无需重启 |
| wheel、Worker 或 MCP 工具发生变化 | 是 | 停止旧 Worker，运行新版安装脚本，然后重启 Worker 和 WorkBuddy |
| `qmt_adapter.py` 或 Adapter 协议发生变化 | 是 | 另外重新部署 Adapter，并重启对应 QMT 策略实例 |
| `bridge.json`、风险参数或示例默认值变化 | 依发布说明 | 既有 runtime 通常不会被静默改写；按说明人工核对 Bridge 与 Adapter 两侧参数 |
| Profile 字段、绑定或签名协议变化 | 是 | 重新核对账户/构建/策略绑定，并按说明重新签名 Profile |
| 数据库 schema 变化 | 是 | 先完整备份数据库及 WAL/SHM，再按该版本迁移说明操作 |

判断依据是新版发布说明和 wheel 版本，不是 ZIP 文件时间。若只是同一版本的文档补充，wheel 内容及版本未变化，不需要重复安装。

### 标准覆盖升级步骤

1. 阅读新版“升级到 x.y.z”说明，确认是否涉及 Adapter、配置、Profile 或数据库迁移；校验发布 ZIP/wheel 的 SHA-256（如果发布方提供）。
2. 停止在 WorkBuddy 中发起新订单，确认没有正在提交的交易意图或需要人工处理的 `SUBMIT_UNKNOWN`。如果存在限时授权，先调用 `revoke_manual_session`，或在本机把 Worker 安全切回观察模式：

   ```powershell
   python -m workbuddy_qmt.console --config "<runtime>\config\bridge.json" `
     set-mode OBSERVE_ONLY
   ```

   切回 `OBSERVE_ONLY` 会使限时授权和未使用的逐笔审批失效；升级完成后不会自动恢复实盘授权。
3. 在 Worker 控制台按 `Ctrl+C` 并等待进程退出。只有发布说明要求更新 Adapter、Profile 或对应配置时，才需要同时停止相关 QMT 策略实例；涉及实盘安全边界时，宁可一并停止。
4. 备份整个 `<runtime>`。至少保留：

   - `config/bridge.json`；
   - `data/secrets/`；
   - `data/state/bridge.db`；
   - 与数据库同目录的 `bridge.db-wal`、`bridge.db-shm`（存在时）；
   - `data/qmt_runtime/*/execution_journal/`；
   - 已验证的 QMT Profile 和 P0 现场证据。

5. 把新版 ZIP **完整解压到新的独立目录**。不要只复制 wheel，也不要把新旧多个 `workbuddy_qmt_bridge-*.whl` 混放在同一发布目录；安装脚本只会选择找到的一个 wheel，混放可能安装错误版本。
6. 在新版解压目录中运行 `首次安装与配置.cmd`。脚本会检查 Python 3.10+、执行用户级 wheel 覆盖升级，并进入配置向导。使用已有 runtime 路径，认真核对向导显示的复用/备份/文件变更提示。
7. 如果本版本修改了 Adapter 或 Profile 协议，按版本说明重新部署 `qmt_adapter.py`、同步 `qmt_adapter.json`、核对三个 `expected_*` 绑定字段，并在需要时重新签名 Profile；然后重启 QMT 策略。仅 Worker/MCP wheel 更新时不要无故改动这些文件。
8. 确认实际安装版本：

   ```powershell
   python -c "import workbuddy_qmt; print(workbuddy_qmt.__version__)"
   ```

   如果日常启动使用的不是这里的 `python`，应改用安装脚本显示的同一个 Python 启动器，例如 `py -3`。
9. 启动 Worker，重启 WorkBuddy 以重新加载 MCP 工具。在 `OBSERVE_ONLY` 下检查 `qmt_health`、账户、持仓和行情同步；涉及交易链路的版本至少在模拟柜台验证一次“同步 → 预览 → 提交/撤销”。
10. 所有检查通过后，再按本机流程恢复已经验收的模式。不要恢复升级前尚未消费的旧预览、逐笔审批或限时授权；重新同步、重新预览并重新授权。

安装脚本不会自动启动 Worker、QMT 或 WorkBuddy。脚本完成不代表新代码已经被正在运行的旧进程加载，必须按上述步骤重启相关进程。

### 升级失败或需要回退

- wheel 安装失败时，旧 Worker 保持停止，先保存完整错误输出并修复 Python/pip 权限或路径问题；不要一边运行旧 Worker 一边重复覆盖安装。
- 只有当目标版本说明数据库、配置和 Adapter 协议向后兼容时，才可直接安装上一版本 wheel 回退。
- 如果升级修改了数据库 schema、配置、Adapter 或 Profile，应停止 Worker/QMT 后整体恢复升级前备份，不能只回退 wheel。
- 回退后固定从 `OBSERVE_ONLY` 启动，重新检查数据库、队列、Adapter、Profile 和签名，不复用升级期间产生的 LIVE 授权。

### 当前 0.3.3 的升级方法

从 0.3.0、0.3.1 或 0.3.2 升级到 0.3.3，只需停止旧 Worker，运行新版 `首次安装与配置.cmd` 覆盖安装 wheel，然后重启 Worker 和 WorkBuddy。新版向导会保留已有 Profile，即使确认更新 Adapter/配置或使用 `setup --force` 也不会清空 Profile。数据库 schema、`bridge.json`、Profile schema 和 Adapter 协议均未变化，不需要仅为本次升级重新签名。

从 0.2.5 或更早版本直接升级到 0.3.3 时，还必须执行下面列出的 0.3.0 协议与数据库迁移步骤。

### 0.3.0 的特殊升级要求

从 0.2.5 或更早版本升级到 0.3.0，必须停止 Worker 和所有 QMT Adapter，撤销现有人工时间授权，完整备份 runtime（特别是数据库、WAL/SHM、密钥、Profile 和执行日志），再运行新版 `首次安装与配置.cmd`。首次启动 0.3.0 时数据库 schema 会从 1 自动迁移到 2，新增结构化自动许可和原子额度使用表；需要回退时应整体恢复升级前备份，不能只安装旧 wheel。

0.3.0 修改了交易命令和 Adapter 授权协议，必须把新生成的 `qmt_adapter.py` 重新部署到每个 QMT 策略实例，并同步 `qmt_adapter.json` 中以下 P1 硬上限后重启策略：

- `adapter_max_auto_session_notional`；
- `adapter_max_auto_orders`；
- `adapter_min_auto_order_interval_seconds`；
- `adapter_max_auto_concurrent_orders`；
- `adapter_max_auto_symbol_position_notional`；
- `adapter_max_auto_account_drawdown`；
- `limited_auto_credit_enabled`。

现有 `bridge.json` 缺少新字段时会使用 0.3.0 的安全默认值，不会自动覆盖文件；建议参考 `examples\bridge.example.json` 明确写出并按账户规模调低。Profile schema 没有变化，只要没有修改 Profile 内容和三个 `expected_*` 绑定，不需要重新签名。

升级后应看到 29 个 MCP 工具，并确认存在 `check_limited_auto_readiness`、`authorize_limited_auto`、`get_limited_auto_status`、`resume_limited_auto` 和 `revoke_limited_auto`。先在 `OBSERVE_ONLY` 运行 `doctor`，再在目标模拟柜台完整验证策略版本不匹配、超额、超频、暂停、恢复、撤销、旧授权代次、死信和 `SUBMIT_UNKNOWN` 均会失败关闭；通过后才考虑小额受监督实盘。

### 历史版本的特殊升级要求

升级到 0.2.0 时必须把最新 `qmt_adapter.py` 重新放入 QMT 策略并重启策略。向导会把旧配置中的模式名安全迁移为统一名称；数据库中的旧只读/空跑模式迁移为 `OBSERVE_ONLY`，旧模拟信号模式迁移为 `SIM_SIGNAL`，旧熔断模式迁移为独立熔断状态。升级后先选择 `OBSERVE_ONLY` 完成状态检查，不要直接恢复交易模式。

升级到 0.2.1 时，既有 runtime 的时效参数不会被静默覆盖。按“WorkBuddy/MCP 调用较慢时的时效参数”一节手工同步 Bridge 与 Adapter，并把 `0.2.1` 中的最新 `qmt_adapter.py` 重新部署到 QMT。只调整时效参数无需重签 Profile；如果重新运行 setup 覆盖了 Profile 或修改了 Profile 内容，则必须重新核对 P0 绑定并使用本机密钥签名。升级后先在 `OBSERVE_ONLY` 下运行 `doctor` 和同步查询，再恢复已验收的模式。

升级到 0.2.2 时，既有 runtime 的单笔金额、单笔股数和单日金额也不会被静默覆盖。按“百万级账户的默认交易额度”一节同步普通/信用账户的 Bridge 与 Adapter 上限。单独调整这些额度不需要重签 Profile；升级后仍应先在 `OBSERVE_ONLY` 下检查账户、持仓、行情和 `doctor`，再恢复已验收模式。

升级到 0.2.3 不需要修改 `bridge.json`、数据库 schema、QMT Adapter 或已签名 Profile；重新运行安装脚本更新 Worker/MCP wheel 即可。升级前未消费的预览需要重新创建。升级后先在 `OBSERVE_ONLY` 下运行 `doctor`，并至少验证一次“同步行情 → 预览 → 提交”的模拟流程，再恢复已验收模式。

升级到 0.2.4 不需要修改数据库 schema、QMT Adapter、`bridge.json` 或已签名 Profile；重新运行安装脚本更新 Worker/MCP wheel 并重启 WorkBuddy 和 Worker 即可看到 `authorize_manual_trade`。该工具扩大了 MCP 的实盘授权能力，首次使用前应在模拟柜台完整验证“预览 → 明确确认 → MCP 授权 → 携带返回的 `approval_context` 提交”流程。

升级到 0.2.5 不修改数据库 schema、QMT Adapter、`bridge.json` 或已签名 Profile。升级后有 24 个 MCP 工具，并增加 `authorize_manual_session`、`get_manual_authorization_status` 和 `revoke_manual_session`。

不要把 runtime、账户号、密钥、令牌或数据库发送给他人，也不要把它们放进公开代码仓库。
