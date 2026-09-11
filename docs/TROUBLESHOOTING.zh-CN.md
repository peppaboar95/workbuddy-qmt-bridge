# 排障与脱敏诊断

## 先运行统一验证

```powershell
workbuddy-qmt verify --human
```

它会检查 Python/软件包、Bridge 配置、密钥与 Token 文件、WorkBuddy MCP 配置、QMT 文件、Worker 和 Adapter 运行状态。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 找不到 Python | 安装 Python 3.10+，启用 `Add Python to PATH`，关闭窗口后重试 |
| wheel 校验失败 | 删除当前解压目录，从官方 GitHub Release 重新下载完整 ZIP，不要继续安装 |
| MCP JSON 无效 | 修复 `%USERPROFILE%\.workbuddy\mcp.json` 的 JSON 语法；向导不会覆盖无效文件 |
| Worker 未运行 | 双击 `启动QMT桥接.cmd` 并保持窗口打开 |
| 端口冲突 | 关闭旧 Worker或占用 17642 的其他程序，再重新启动 |
| Adapter 离线 | 确认 QMT 已登录、对应策略已启动，并部署了当前 `qmt_ready` 中的脚本 |
| Adapter/Profile 不同步 | 停止 Worker/QMT，运行账户 `configure --force`；已有 Profile 默认保留 |
| WorkBuddy 看不到工具 | 运行 `verify`，确认 MCP 配置通过后完整重启 WorkBuddy |

## 打开相关目录

```powershell
workbuddy-qmt open qmt-ready --human
workbuddy-qmt open logs --human
workbuddy-qmt open config --human
```

## 生成可分享的脱敏诊断包

```powershell
workbuddy-qmt support-bundle --redact --human
```

诊断包只包含版本、脱敏后的配置结构、检查结果和 QMT 文件哈希。默认不包含：

- 日志正文；
- 数据库及 WAL/SHM；
- Worker Token 或消息密钥；
- 完整 QMT 账户号；
- Profile/Adapter 配置正文和签名。

发送给他人前仍应手工检查 ZIP 内容。不要把 runtime、账户号、数据库、Profile、密钥、Token 或原始日志直接上传到 Issue。

对于 `SUBMIT_UNKNOWN`，必须到券商柜台人工核对最终结果；不要自动重发或通过修改数据库改变状态。
