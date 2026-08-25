# WorkBuddy-QMT Bridge 0.3.0 P1 验证记录

## 软件范围

0.3.0 将 `LIMITED_AUTO` 从失败关闭占位模式推进为 P1 软件控制面：

- SQLite schema 2 的结构化策略许可和原子额度使用记录；
- WorkBuddy/MCP readiness、授权、状态、恢复和撤销工具；
- 同一交易日最长 720 分钟、默认覆盖完整盘中的交易窗口；
- 账户、证券、动作、sizing 类型、source、规则集和规则版本绑定；
- 单笔金额/数量、累计金额、订单数、频率、并发、单标的持仓金额、账户净值回撤和连续失败限制；
- Worker 提交事务内额度预占，以及 Adapter 对同一签名策略哈希和授权代次的独立校验；
- Adapter 在不确定提交或自动命令拒绝时本地暂停；
- 模式切换、熔断、跨日、撤销、故障暂停和恢复代次轮换；
- `qmt_health` 和许可状态中的额度、暂停与健康信息。

## 已执行验证

发布构建执行了以下验证：

1. 全包 Python 语法编译；
2. 0.3.0 wheel 隔离安装和版本/MCP 工具数检查；
3. 从已安装 wheel 运行 7 项 P1 增量测试；
4. Bridge、普通账户 Adapter、信用账户 Adapter 示例 JSON 解析；
5. `bridge.example.json` 严格配置加载和 P1 范围校验；
6. 安装向导生成 QMT bundle、P1 Adapter 硬上限字段和 GBK 脚本读取冒烟测试；
7. wheel 内容、版本、RECORD 和发布 ZIP 完整性检查。

测试命令：

```powershell
python -m pip install --no-deps --target <临时目录> workbuddy_qmt_bridge-0.3.0-py3-none-any.whl
$env:PYTHONPATH='<临时目录>'
python -m unittest discover -s tests -v
```

增量测试覆盖 schema 迁移、结构化许可、预览/提交额度占用、策略版本不匹配、死信自动暂停、全局熔断撤销、恢复代次轮换，以及 Adapter 本地暂停和策略绑定防篡改。

## 仍需目标环境验收

软件完成不等于目标真实资金柜台已经验收。首次实际使用前仍必须在目标电脑、目标 QMT 构建、目标账户和目标券商柜台完成：

- Profile 枚举、账户/持仓/委托/成交/错误字段和备注关联 P0；
- 模拟柜台的超额、超频、并发、回撤、暂停、恢复、撤销、死信和 `SUBMIT_UNKNOWN` 故障演练；
- 小额、受监督、单标的普通股票限价实盘验收；
- Windows ACL、备份和整体回退演练。

信用账户 `limited_auto_credit_enabled` 默认保持 `false`。未完成信用资格、额度、负债、维持担保比例和六类动作的现场验证前，不得开启；融资买入和融券卖出还必须在许可中显式允许新增负债。

当前每个账户同一时间只允许一个活动 P1 策略许可。发布包不会自动启动 QMT、Worker，不会自动切换模式，也不会自动创建或恢复任何交易许可。
