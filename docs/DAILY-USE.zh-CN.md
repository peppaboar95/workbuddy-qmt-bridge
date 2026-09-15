# 日常使用

## 推荐启动顺序

1. 打开并登录大 QMT；
2. 启动每个已启用账户对应的 QMT 策略实例；
3. 双击桌面的 `启动QMT桥接.cmd`；
4. 不确定时直接按 Enter 使用 `OBSERVE_ONLY`；
5. 保持 Worker 窗口打开；
6. 打开或重启 WorkBuddy；
7. 先查询 `qmt_health`、账户、持仓和行情，再进行其他操作。

启动桥接时，所选模式会自动同步到每个已启用账户的 `qmt_adapter.json`，无需手工修改 `qmt_mode`。新版 Adapter 会在下一次命令轮询前读取模式变化，默认约 500ms；账户绑定、字段映射和 Profile 保持原样。其他配置变化仍需重启 QMT 策略。

首次使用这项功能时，需要更新软件并重新生成、部署新版 `qmt_adapter.py`。旧策略副本不会自动升级，更新后再次切换模式无需重启策略。同步失败会停止本次 Worker 启动；熔断状态下只同步为 `OBSERVE_ONLY`，不会解除熔断或创建交易授权。

关闭 Worker 窗口或按 `Ctrl+C` 会安全停止 Worker，但不会自动关闭 QMT，也不会撤销已经提交给券商柜台的委托。

## 四种模式

| 模式 | 用途 | 是否可能实际报单 |
| --- | --- | --- |
| `OBSERVE_ONLY` | 查询、预览和空跑 ACK 闭环 | 否 |
| `SIM_SIGNAL` | 已完成现场验证的模拟柜台 | 是 |
| `MANUAL_LIVE` | 人工实盘，仍需逐笔或限时授权 | 是 |
| `LIMITED_AUTO` | 有签名策略许可的有限自动交易 | 是 |

运行模式不能识别当前 QMT 连接的是模拟柜台还是真实柜台。任何非观察模式都必须由操作者自行核对账户和柜台。

## 状态与目录

```powershell
workbuddy-qmt status --human
workbuddy-qmt verify --human
workbuddy-qmt open logs --human
workbuddy-qmt open qmt-ready --human
```

`status` 适合日常查看；`verify` 会同时检查安装、MCP、Worker、QMT 文件和 Adapter 运行状态。

## 增加或停用账户

修改账户前先停止 Worker 和所有 QMT 策略。

```powershell
# 查看当前账户
workbuddy-qmt account list --human

# 启用信用账户并生成本机 QMT 文件
workbuddy-qmt account enable main_credit --qmt-account-id "<完整QMT账户号>" --human

# 保留 Profile，重新生成变化的 Adapter/配置
workbuddy-qmt account configure main_credit --qmt-account-id "<完整QMT账户号>" --force --human

# 停用但不删除文件或 Profile
workbuddy-qmt account disable main_credit --confirm DISABLE-ACCOUNT --human
```

账户变化后保持 `OBSERVE_ONLY`，重新部署/启动对应 QMT 策略，再运行 `verify`。

## 更快的下单调用方式

单笔交易使用以下流程：

1. 调用 `prepare_trade`，由桥接一次完成账户、持仓、委托、成交和目标行情同步，并返回预览；
2. 展示预览并完成当前模式要求的确认或授权；
3. 调用 `submit_trade_intent`；
4. 返回 `QUEUED` 后调用一次 `wait_trade_intent`，建议等待 2.5 秒；
5. 如果超时，保留 `intent_id`，稍后查询同一意图。不要重新提交预览。

同一账户同时处理多个标的时，不要为每个标的单独同步。先调用一次 `request_sync`，在 `symbols` 中放入全部目标标的，然后复用新鲜账户/持仓数据逐笔 `preview_trade`。

新的 QMT Adapter 默认每 500ms 取一次命令，Worker 每 250ms 摄取一次回报。旧的 QMT 策略副本不会自动变化；升级后需要重新生成并部署 Adapter，Profile 默认保留。

Adapter 每 5 秒刷新账户和持仓；委托与成交以 QMT 实时回调为主，并每 30 秒完整对账兜底。策略启动、`prepare_trade` 和包含 `ORDER`/`DEAL` 的显式同步仍会立即完整对账；异常报单会请求提前对账。
