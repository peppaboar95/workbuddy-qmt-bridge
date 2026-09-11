# 升级、备份与恢复

## 检查版本

```powershell
workbuddy-qmt --version
workbuddy-qmt upgrade-check --human
```

版本检查只读取 GitHub 最新 Release，不会自动下载、安装或修改配置。

## 标准升级

1. 停止 WorkBuddy 中的新 QMT 请求；
2. 停止 Worker 和所有 QMT 策略；
3. 完整备份 runtime，特别是数据库及同目录 `-wal`/`-shm`、密钥、Profile 和执行日志；
4. 把新 ZIP 完整解压到新的独立目录；
5. 双击 `安装、升级或修复.cmd`；
6. 安装器自动校验 wheel 并覆盖安装软件包；
7. 向导复用已有配置。已有签名 Profile 默认原样保留；
8. 重新启动 QMT、Worker 和 WorkBuddy，在 `OBSERVE_ONLY` 下运行 `verify`。

不要把新旧 wheel 混放在同一目录。不要在 Worker 或 QMT 策略运行时覆盖升级。

## Profile 保护

普通安装、重复运行 `setup` 和 `setup --force` 都不会重置 Profile。只有确实废弃旧 P0 映射时，停止 Worker 与对应 QMT 策略后执行：

```powershell
workbuddy-qmt setup `
  --reset-profile main_stock `
  --confirm-reset-profile RESET-QMT-PROFILE
```

重置前会创建时间戳备份。不要为了普通升级而执行这条命令。

## 失败与回退

- wheel 安装失败：保留旧 runtime，不要反复启动不同版本 Worker；修复 Python/pip 问题后重试；
- 新版本启动失败：保存错误输出，运行 `doctor --human`，不要直接修改数据库；
- 必须回退时：停止全部进程，整体恢复升级前的软件包和 runtime 备份；不能只安装旧 wheel 配合新数据库；
- 存在 `SUBMIT_UNKNOWN` 时：先在券商柜台人工核对，不要通过回退或重新提交来猜测结果。

当前版本的数据库、Profile 和 Adapter 协议变化以对应的 `RELEASE-vX.Y.Z.md` 为准。
