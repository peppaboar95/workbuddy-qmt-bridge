# WorkBuddy MCP 配置示例说明

可直接复制并修改同目录的 `workbuddy.mcp.example.json`。正式配置的默认位置是：

```text
%USERPROFILE%\.workbuddy\mcp.json
```

JSON 不支持注释，所以示例文件使用“必须修改”路径占位符标出必填项。保存前必须替换这些占位符，不能原样使用。

## 必须修改的参数

| JSON 位置 | 是否修改 | 配置要求 |
| --- | --- | --- |
| `mcpServers.qmt-bridge.command` | 必须 | 填写安装了 `workbuddy-qmt-bridge` 的 Python 3.10+ `python.exe` 绝对路径。执行 `python -c "import sys; print(sys.executable)"` 可以查询当前 Python 路径。 |
| `mcpServers.qmt-bridge.args` 中 `--config` 后一项 | 必须 | 填写本机实际 `runtime\config\bridge.json` 的绝对路径。源码目录、盘符和用户名在不同电脑上可以不同。 |
| `mcpServers.qmt-bridge.args` 中 `--endpoint` 后一项 | 通常不改 | 默认是 `http://127.0.0.1:17642`。建议保持 `host=127.0.0.1`；如果只修改了 `bridge.json` 的 `port`，这里必须使用同一端口。绝不能配置成局域网或公网地址。 |
| `mcpServers.qmt-bridge.disabled` | 必须为 `false` | `false` 表示 WorkBuddy 启用这个 MCP 服务。这里是 JSON 布尔值，不能写成字符串 `"false"`。 |

其余内容都是固定启动参数：

- `mcpServers`：WorkBuddy 的 MCP 服务集合，保留原样。
- `qmt-bridge`：本项目的服务名称，保留原样。
- `-m workbuddy_qmt.mcp_server`：启动 MCP 前端模块，保留原样。
- `--config`、`--endpoint`：参数名称，保留原样，只修改它们后面的值。

## 修改示例

假设 Python 实际路径是 `C:\Users\demo\AppData\Local\Programs\Python\Python313\python.exe`，发布包向导使用默认运行目录，配置应为：

```json
{
  "mcpServers": {
    "qmt-bridge": {
      "command": "C:\\Users\\demo\\AppData\\Local\\Programs\\Python\\Python313\\python.exe",
      "args": [
        "-m",
        "workbuddy_qmt.mcp_server",
        "--config",
        "C:\\Users\\demo\\AppData\\Local\\WorkBuddyQMTBridge\\runtime\\config\\bridge.json",
        "--endpoint",
        "http://127.0.0.1:17642"
      ],
      "disabled": false
    }
  }
}
```

Windows 路径中的反斜杠在 JSON 中必须写成双反斜杠 `\\`；也可以使用正斜杠，例如 `C:/Users/demo/AppData/Local/WorkBuddyQMTBridge/runtime/config/bridge.json`。源码用户如果选择了项目内 `runtime`，则改成该电脑源码目录下的实际绝对路径。

## 已有其他 MCP 服务时

不要用整个示例文件覆盖现有 `mcp.json`。只把示例中的 `qmt-bridge` 节点加入现有 `mcpServers`，并保留其他服务。更推荐重新运行 `首次安装与配置.cmd`：向导会先备份原文件，再自动合并或更新 `qmt-bridge`。

修改完成后重启 WorkBuddy，并先启动桌面的 `启动QMT桥接.cmd`。MCP 配置中不需要、也不应该填写 Worker 令牌、QMT 账户号、券商密码、密钥或其他敏感信息。
