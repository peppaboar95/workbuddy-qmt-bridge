# v0.3.5 — MCP 下单延迟与 QMT 对账优化

v0.3.5 缩短 WorkBuddy 通过 MCP 完成交易准备和结果确认时的等待，并减少 QMT Adapter 的周期查询开销。交易确认、硬风控、数据库 schema、Profile 签名和失败关闭边界均未放宽。

## Changes

- 新增 `prepare_trade`：一次完成必要快照刷新、有限等待和安全预览，不会自动提交订单；
- 新增 `wait_trade_intent`：提交后按 `intent_id` 进行一次最长 5 秒的有界等待，超时不会重发；
- QMT Adapter 命令轮询默认从 1 秒缩短为 500ms，Worker 回报摄取轮询从 1 秒缩短为 250ms；
- Adapter 每 5 秒周期任务只刷新账户与持仓；委托和成交以 QMT 实时回调为主，每 30 秒完整对账一次；
- 启动、显式同步和 `prepare_trade` 仍会完整刷新委托与成交；报单结果不确定或 QMT 返回委托错误时，会请求下一个 5 秒守卫周期提前对账；
- Worker 的 MCP 请求线程与后台事件摄取使用同一个进程内写事务锁，降低 SQLite 写锁尾延迟；
- MCP 工具说明补充单账户多标的批量同步、复用新鲜快照和提交后仅做一次有界等待的建议；
- 新增组合准备、等待超时、单写者并发、快速轮询和混合对账回归测试。

## Upgrade notes

- 停止 Worker 和全部 QMT 策略后，完整备份 runtime；
- 把本版 ZIP 解压到新目录，双击 `安装、升级或修复.cmd`；
- 重新运行 setup 生成最新版 `qmt_adapter.py` 与 `qmt_adapter.json`，再按账户目录中的 `部署说明.txt` 更新 QMT 策略；
- 已有签名 Profile 默认原样保留，不需要为了本次升级重置 Profile 或重新完成 P0；
- 升级后保持 `OBSERVE_ONLY`，运行 `verify --human` 并确认 Adapter 心跳、账户、持仓、委托和成交数据正常，再恢复原运行模式。

## Safety

- `prepare_trade` 只生成预览，提交仍需独立确认并调用 `submit_trade_intent`；
- `wait_trade_intent` 只读且绝不重发，超时后必须按原 `intent_id` 查询；
- 30 秒对账只是实时回调之外的兜底，启动、显式同步和异常路径仍保留完整对账；
- 本版本不改变 Profile、授权、硬风控、双端复核或信用自动交易默认关闭策略；
- 软件测试通过不代表目标 QMT、券商和真实资金账户已经完成现场验收。

## Assets

- `workbuddy_qmt_bridge-0.3.5-py3-none-any.whl`
- `workbuddy-qmt-bridge-0.3.5.zip`
- `workbuddy-qmt-bridge-0.3.5.zip.sha256`
- `SHA256SUMS.txt`

四个资产必须来自同一次最终构建。
