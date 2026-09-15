# P0/P1 高级配置

这部分面向已经完成 `OBSERVE_ONLY` 只读连接的操作者。首次安装不需要执行这里的步骤。

## P0：目标柜台映射验证

开放任何可能调用 `passorder` 的模式前，必须在目标 QMT 版本、券商构建和模拟柜台中验证：

- 账户、资产、持仓、委托、成交和行情字段；
- 普通/信用账户类型与动作映射；
- 最小数量、价格类型、状态码和撤单回报；
- 重启、超时、重复请求及 `SUBMIT_UNKNOWN` 恢复；
- Profile 中的 QMT 构建、券商构建、账户和策略绑定。

完成验证后才可修改 Profile 的验证字段和映射，并在目标电脑本机签名：

```powershell
python -m workbuddy_qmt.console `
  --config "<runtime>\config\bridge.json" `
  sign-qmt-profile "<runtime>\qmt_ready\main_stock\qmt_profile.json" `
  --confirm VERIFIED-PROFILE
```

签名后修改任何 Profile 字段都会使签名失效。其他电脑、账户、QMT 构建或券商构建的签名 Profile 不可复用。

## 从模拟到人工实盘

1. 在已验收模拟柜台使用 `SIM_SIGNAL` 完成最小数量、报单、成交、撤单和恢复验证；
2. 真实资金场景先使用 `MANUAL_LIVE` 小额验收；
3. 每笔仍执行“同步 → 预览 → 最新风控 → 提交”；
4. 逐笔授权或限时授权只开放授权条件，不会绕过 Profile、价格笼子、资金、持仓和熔断。

## P1：有限自动交易

只有 readiness 无阻断、策略范围明确且目标环境已验收时，才使用 `LIMITED_AUTO`：

1. 启动桥接时选择 `LIMITED_AUTO` 并完成本机确认，Worker 和新版 Adapter 模式会自动同步；
2. 调用 `check_limited_auto_readiness`；
3. 向操作者展示账户、标的、动作、策略版本、时段和全部额度；
4. 明确确认后调用 `authorize_limited_auto`；
5. 使用 `get_limited_auto_status` 观察剩余时间、订单数、金额和暂停原因；
6. 健康异常后排除原因、重新同步、通过 readiness，再显式恢复；
7. 结束时撤销许可。撤销不会自动撤销券商侧已有委托。

完整字段、确认词、状态机和错误码见[在线 API 参考](https://peppaboar95.github.io/workbuddy-qmt-bridge/)；v0.3.0 的软件测试范围见 [P1 软件验证记录](P1-VALIDATION.zh-CN.md)。
