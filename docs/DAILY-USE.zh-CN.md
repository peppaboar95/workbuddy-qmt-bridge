# 日常使用

## 推荐启动顺序

1. 打开并登录大 QMT；
2. 启动每个已启用账户对应的 QMT 策略实例；
3. 双击桌面的 `启动QMT桥接.cmd`；
4. 不确定时直接按 Enter 使用 `OBSERVE_ONLY`；
5. 保持 Worker 窗口打开；
6. 打开或重启 WorkBuddy；
7. 先查询 `qmt_health`、账户、持仓和行情，再进行其他操作。

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
