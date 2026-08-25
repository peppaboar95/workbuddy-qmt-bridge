# Security Policy

## 支持版本

| 版本 | 安全更新 |
| --- | --- |
| 0.3.x | 支持 |
| 0.2.x 及更早版本 | 不支持 |

## 报告漏洞

请不要通过公开 GitHub Issue 报告可能导致真实资金委托、授权绕过、密钥泄漏、消息伪造、重复报单或账户数据泄漏的问题。

请在仓库的 Security → Advisories 页面选择 Report a vulnerability，向维护者提交私密报告：

https://github.com/peppaboar95/workbuddy-qmt-bridge/security/advisories

维护者应在 3 个工作日内确认收到报告。仓库所有者必须在首次公开发布时启用 GitHub Private vulnerability reporting；如果页面没有私密报告入口，请不要在公开 Issue 中披露细节。

报告内容请尽量包括版本、运行模式、最小复现步骤、预期与实际结果，以及已经脱敏的日志。不要提供券商密码、真实账户号、密钥、Worker 令牌、完整数据库或可识别个人身份的信息。

## 高优先级问题

- 未授权地进入 MANUAL_LIVE 或 LIMITED_AUTO；
- 绕过逐笔审批、时间授权或结构化策略许可；
- 绕过账户、证券、动作、策略版本、金额、订单数、回撤或并发限制；
- PRE_SUBMIT 或 SUBMIT_CALLED 后重复调用 passorder；
- 篡改签名命令、Profile 或 Adapter 配置后仍能报单；
- 熔断、暂停或撤销后仍能创建新委托；
- 通过 MCP 参数、日志、诊断包或仓库文件泄漏密钥与账户数据。

## 运行方责任

本项目不能替代券商权限控制、QMT 柜台隔离或人工监控。运行方必须：

- 默认保持 OBSERVE_ONLY；
- 先在目标模拟柜台完成字段和报单闭环；
- 使用 Windows ACL 保护密钥、令牌、数据库和执行日志；
- 对真实资金部署执行最小权限、备份和恢复演练；
- 监控死信、SUBMIT_UNKNOWN、熔断、回撤和许可剩余预算；
- 发生不确定结果时停止自动化并人工核对券商侧状态。
