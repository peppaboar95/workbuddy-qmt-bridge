# QMT 内置 Python 的 mmap 传输

`adapter_transport` 默认是 `file`，已有配置、文件队列和部署路径继续可用。选择 `mmap` 后，QMT 策略通过 Windows 命名内存映射与外部 Worker 交换消息；策略不需要 `open` 或 `os` 文件接口。外部 Worker 仍负责配置、Profile、消息密钥、授权和持久执行记录。

目标 QMT 内置运行时为 Python 3.6 或 3.12，外部 Worker 需要 Python 3.10 或更高版本。生成脚本使用 Python 3.6 语法；这不表示已经在目标 QMT 版本或券商柜台完成验证。当前 QMT 同步接口需要现场适配，不能只修改 JSON 后直接启用交易。

能力探针已在一套 64 位 Python 3.12.10 大 QMT 中验证命名 mmap 双向通信及 ctypes/Win32 mutex 的双向互斥；这不等于桥接 Adapter 或柜台交易闭环已验收。其他环境仍应先按 [能力探针说明](QMT-CAPABILITY-PROBE.zh-CN.md) 实测，Python 3.6 的对应能力也需单独确认。

## 配置

以下字段放在 `config/bridge.json` 的对应账户对象中，其他账户、风控和信用配置照常保留：

```json
{
  "alias": "main_stock",
  "account_type": "STOCK",
  "adapter_instance": "qmt_stock_01",
  "enabled": true,
  "adapter_transport": "mmap",
  "mmap_name": "",
  "mmap_size": 4194304
}
```

| 字段 | 默认值 | 约束 |
| --- | --- | --- |
| `adapter_transport` | `file` | 只能是 `file` 或 `mmap` |
| `mmap_name` | 空字符串 | 留空按绝对 `data_dir` 与 `adapter_instance` 的哈希派生 `Local\WBQMT_<hash>`；显式名称须以 `Local\` 开头，后缀为 1–200 个 ASCII 字母、数字、下划线或连字符 |
| `mmap_size` | `4194304` | 65536–16777216 的整数，单位为字节 |

显式名称的 JSON 写法为 `"mmap_name": "Local\\WBQMT_stock_01"`。名称不接受点、路径分隔符或非 ASCII 后缀。已启用 mmap 账户不能使用相同的有效映射名。默认派生名区分运行目录和 Adapter 实例；不要给不同安装手工配置同一个名称。

Worker 和 QMT 须运行在同一台 Windows 机器的同一登录会话。映射用于传输，内容不加密；HMAC 用来校验消息的来源和完整性。不要把映射名当作密钥。

## 通过 CLI 生成部署文件

先停止 Worker 与该账户的 QMT 策略。以下命令使用已初始化的运行目录，账户须已启用：

```powershell
workbuddy-qmt account --config "D:\qmt-runtime\config\bridge.json" configure main_stock --qmt-account-id YOUR_QMT_ACCOUNT --transport mmap --force
```

需要显式名称或大小时：

```powershell
workbuddy-qmt account --config "D:\qmt-runtime\config\bridge.json" configure main_stock --qmt-account-id YOUR_QMT_ACCOUNT --transport mmap --mmap-name 'Local\WBQMT_stock_01' --mmap-size 4194304 --force
```

命令只向 `bridge.json` 写入明确传入的选项。省略这些选项会保留原设置；`--mmap-name ''` 可恢复派生名称。`--force` 对变化的生成文件建立备份后覆盖，已有 `qmt_profile.json` 会按原规则保留，不会因更换传输方式而清空或重签。生成失败时会恢复本次修改前的 Bridge 配置。

生成目录仍是 `qmt_ready/<账户别名>/`，包含策略脚本、外部配置、Profile 和部署说明。`qmt_adapter.json` 保留绝对 `data_dir`、`key_file` 和 `mapping_profile` 路径，供 Worker 使用，并增加当前有效的 `adapter_transport`、`mmap_name`、`mmap_size`。QMT 侧只加载生成脚本；脚本中的 `MMAP_BOOTSTRAP` 已绑定映射名称、大小、账户和实例。改动映射、账户绑定或消息密钥后，要重新生成脚本并在 QMT 中重新加载。

返回文件模式时使用同一命令并指定 `--transport file --force`，再重新加载生成的文件模式策略。

## P0 验证后的重新部署

完成目标环境的 P0 验证并签名 `qmt_profile.json` 后，停止 Worker 和 QMT 策略，在外部 `qmt_adapter.json` 中填写匹配的 `expected_profile_id`、`expected_qmt_build`、`expected_broker_build`，以及经验证的字段映射和信用查询配置。然后重新生成：

```powershell
workbuddy-qmt account --config "D:\qmt-runtime\config\bridge.json" configure main_stock --qmt-account-id YOUR_QMT_ACCOUNT --force
```

此命令沿用账户已有的 mmap 传输设置。原配置的账户别名、账户类型、Adapter 实例和 QMT 账户号均匹配时，会保留三个 Profile 绑定字段、`field_map`、`order_status_map`、信用查询验证标记和其他自定义设置；路径、映射绑定、轮询设置和风控值会重新按 Bridge 配置生成，模式回到 `OBSERVE_ONLY`。已有签名 Profile 保持原始字节。

将新生成的 `qmt_adapter.py` 完整替换到 QMT 策略，重新注入同步接口后启动两端，先验证只读连接。不要只修改外部 JSON 后继续运行旧脚本：mmap 脚本会拒绝与静态启动配置不同的设置；动态模式、签名 Profile 和授权按协议更新。

改变 QMT 账户号或其他账户绑定时，不会借用旧配置中的 Profile 绑定和自定义设置。旧 Profile 文件仍按原规则保留并使不匹配的绑定校验失败，需为新绑定完成 P0 验证。专用 Profile 重置操作也会把三个期望字段恢复为待验证占位符。

## 必需的进程同步接口

QMT 侧必须在策略初始化前提供：

```python
def MMAP_LOCK_FACTORY(name):
    # 打开同名 Win32 命名 mutex，返回支持 with 的锁对象。
    # 将目标环境已验证的 API 适配为上下文管理器；此处不是可运行的实现。
    ...
```

调用名称为有效映射名加 `_lock`。默认外部 Worker 使用 `WindowsNamedMutex`，通过 Win32 `CreateMutexW` 打开该名称的内核 mutex。QMT 工厂必须打开同一个 Win32 命名 mutex，并返回支持 `with lock:`、正确获取和释放所有权的上下文管理器；使用相同字符串命名的信号量或其他锁不能与默认 Worker 互斥。普通线程锁、GIL、`mmap.flush()` 或一次切片赋值也不能替代此同步。缺少 QMT 的 `MMAP_LOCK_FACTORY` 时，初始化会报错并保持不可交易状态。

探针可分别验证 ctypes 和 win32event 后端；已验证环境的 ctypes 后端通过了双向互斥。文中的工厂函数仍是需要部署者接入的注入接口，不是可直接运行的锁实现。完成适配后，按普通方式启动 Worker 和策略，先保持 `OBSERVE_ONLY` 验证连接。通过 `doctor` 检查 mmap 启动绑定与外部配置是否一致，再检查心跳、账户、行情、委托、成交和重启恢复。

## 密钥与执行恢复

生成的 mmap 策略源码包含静态消息签名密钥，须按 `data/secrets/message_keys.json` 同等保护。不要分享、提交到公开仓库、上传到在线编辑器或放进诊断包。脚本不包含 Worker 访问令牌。更换密钥时须停止两端、重新生成并加载脚本。

动态模式、Profile 与授权由 Worker 经已认证的会话发送给策略。连接丢失或控制租约过期后，策略不得继续提交新委托。共享内存不充当持久执行凭据；报单前的持久化授权及重启后的不确定结果由 Worker 处理，未知结果不能自动重发。

本地自动测试覆盖协议与软件行为。目标 QMT 的 Python 3.6/3.12 导入限制、同步实现、策略调度、券商报单/回报、双端重启和连接中断仍需现场验证；完成这些验证前保持 `OBSERVE_ONLY`。
