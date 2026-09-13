# WorkBuddy-QMT Bridge 快速开始

这份说明只帮助你完成第一次只读连接。全过程保持 `OBSERVE_ONLY`，不会向 QMT 实际报单。

## 开始前准备

- Windows 10/11；
- Python 3.10 或更高版本；
- 已安装并可登录的 Tencent WorkBuddy 与大 QMT；
- 需要启用账户的完整 QMT 账户号。

QMT 账户号通常可以在大 QMT 的账户选择、资产查询或策略绑定界面看到。它不是登录密码，也不是证券名称。拿不准时先停止安装并向券商/QMT 运维人员确认，不要猜测。

## 1. 安装

1. 从 [GitHub Releases](https://github.com/peppaboar95/workbuddy-qmt-bridge/releases/latest) 下载 `workbuddy-qmt-bridge-0.3.5.zip`；
2. 使用 Windows“全部解压”放到一个新的独立目录；
3. 双击 `安装、升级或修复.cmd`；
4. 安装器会先检查 Python，再自动校验 wheel 的 SHA-256。校验失败时不要继续安装；
5. 配置向导中不确定的选项可直接按 Enter 使用安全默认值。

向导只会合并 `mcpServers.qmt-bridge`，修改现有 WorkBuddy MCP 配置前会创建备份。重复运行不会重置已有 Profile。

## 2. 部署 QMT Adapter

向导完成后会自动打开 `qmt_ready`。每个已配置账户都有独立目录，并包含：

- `qmt_adapter.py`：复制到大 QMT 策略编辑器的源码；
- `qmt_adapter.json`：本机 Adapter 配置，初始固定为 `OBSERVE_ONLY`；
- `qmt_profile.json`：初始未验证 Profile；
- `部署说明.txt`：针对该账户生成的准确步骤和文件路径。

在大 QMT 中为每个账户创建独立策略实例，将对应 `qmt_adapter.py` 的完整内容复制进去。脚本中的配置路径已经自动写好，不要手工修改。确认策略绑定的是刚才填写的账户，然后手工启动策略。

## 3. 启动并验证

1. 双击桌面的 `启动QMT桥接.cmd`；
2. 模式选择直接按 Enter，使用 `OBSERVE_ONLY`；
3. 保持 Worker 窗口打开；
4. 双击 `验证QMT桥接.cmd`。

完成状态应类似：

```text
[完成] Python 与软件包
[完成] Bridge 配置
[完成] WorkBuddy MCP 配置
[完成] QMT 文件: main_stock
[完成] Worker 正在运行

首次只读连接已完成。当前仍应保持 OBSERVE_ONLY，不会实际报单。
```

最后重启 WorkBuddy，让 MCP 配置重新加载。在 WorkBuddy 中先调用 `qmt_health`，再查询账户和持仓。

## 常用命令

```powershell
workbuddy-qmt --version
workbuddy-qmt verify --human
workbuddy-qmt open qmt-ready --human
workbuddy-qmt open logs --human
workbuddy-qmt account list --human
workbuddy-qmt upgrade-check --human
```

如果命令不在 PATH 中，可使用 `python -m workbuddy_qmt.manager` 替代 `workbuddy-qmt`。

遇到问题请看 [排障与脱敏诊断](TROUBLESHOOTING.zh-CN.md)。
