# v0.3.6 — 启动模式自动同步与使用体验整理

v0.3.6 在启动桥接时自动同步 Worker 与已启用账户的 Adapter 运行模式，省去逐次手工修改 `qmt_adapter.json` 的步骤，并整理桌面入口、安装包目录和文档导航。交易授权、硬风控、数据库 schema 和 Profile 签名边界保持不变。

## Changes

- 启动时将所选模式写入已配置账户的 `qmt_mode`；保留账户号、字段映射、自定义配置与已有 Profile；
- 多账户先完成全部预检，再备份并原子写入配置；预检失败停止启动，写入失败尝试恢复原配置；
- 新版 Adapter 在命令轮询前读取模式变化，默认约 500ms，兼容先启动 QMT 或先启动桥接两种顺序；
- Adapter 配置无效或同时修改了模式以外的字段时，停止处理命令并报告 `ADAPTER_MODE_SYNC_FAILED`；其他配置变化仍需重启 QMT 策略；
- 桥接处于熔断状态时，只同步 Adapter 为 `OBSERVE_ONLY`，保留熔断；
- Release 内部安装文件归入 `installer/`，详细使用资料归入 `docs/`；
- 桌面保留启动与状态两个脚本，状态入口先运行连接验证再显示状态，并安全清理本项目生成的旧入口；
- 修复部分 Windows 代码页下中文安装入口闪退和 editable 安装路径末尾反斜杠导致的参数问题；
- 新增源码文档导航，修正发布包上手步骤顺序，并明确 v0.3.0 P1 验证记录的历史范围。

## Upgrade notes

1. 停止 Worker 和全部 QMT 策略，完整备份 runtime；
2. 将 `workbuddy-qmt-bridge-0.3.6.zip` 完整解压到新的独立目录，双击 `安装、升级或修复.cmd`；
3. 重新生成 QMT 文件，按每个账户目录中的 `部署说明.txt` 将新版 `qmt_adapter.py` 部署到对应策略，保留已验证的账户配置与 Profile 绑定；
4. 已有签名 Profile 默认保留，不需要仅为本次升级重置或重新签名；
5. 在 `OBSERVE_ONLY` 下启动桥接和 QMT 策略，运行 `verify --human` 并调用 `qmt_health` 确认连接；
6. 此后通过启动桥接选择模式，支持热加载的 Adapter 会自动读取，无需逐次修改 JSON 或重启策略。

旧 QMT 策略副本不会自动升级。重新生成配置后，请核对已有 `expected_*` 与目标 Profile 的绑定；模式同步不会替你填写或验证 P0 映射。

## Safety

- 非观察模式仍需本机确认，并在同步前校验账户绑定、Profile 验证状态、映射和签名；
- 模式同步不会创建、延长或恢复 MANUAL_LIVE 授权或 LIMITED_AUTO 策略许可；
- 信用自动交易默认关闭；未知提交结果仍不得自动重发；
- 软件测试通过不表示目标 QMT、券商和真实资金账户已经完成现场验收。

## Validation

- 46 项软件回归测试通过，包括 18 项启动同步与运行中 Adapter 模式读取测试；
- 覆盖多账户预检、失败回滚、配置与 Profile 保留、熔断、重复启动和热加载异常恢复；
- Adapter 保持 Python 3.6 语法与 GBK 编码兼容；
- wheel 隔离安装、Release ZIP 安装校验、首次 setup、重复 setup 与脱敏诊断冒烟验证。

## Assets

- `workbuddy_qmt_bridge-0.3.6-py3-none-any.whl`
- `workbuddy-qmt-bridge-0.3.6.zip`
- `workbuddy-qmt-bridge-0.3.6.zip.sha256`
- `SHA256SUMS.txt`

四个资产来自同一次构建。下载后应核对 SHA-256。
