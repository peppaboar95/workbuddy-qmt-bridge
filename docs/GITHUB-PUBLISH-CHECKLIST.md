# GitHub 发布清单

目标仓库：https://github.com/peppaboar95/workbuddy-qmt-bridge

## 创建仓库前

- [ ] 确认 LICENSE 为 MIT，版权主体为 peppaboar95；
- [ ] 检查 Git 历史中不存在密钥、令牌、账户号、数据库和未脱敏材料；
- [ ] 从干净源码运行测试和 tools/build_release.ps1；
- [ ] 验证 wheel 隔离安装、版本号、入口点和 ZIP 内容；
- [ ] 核对 SHA256SUMS.txt 覆盖全部 Release 二进制资产；
- [ ] 最后审阅 README、SECURITY 和当前版本 Release 文案。

## GitHub 仓库设置

- [ ] 仓库所有者为 peppaboar95，名称为 workbuddy-qmt-bridge；
- [ ] 初次推送后先检查文件列表，再将可见性设为 Public；
- [ ] 在 Settings → Security → Advanced Security 中启用 Private vulnerability reporting；
- [ ] 确认 Security → Advisories 页面显示 Report a vulnerability；
- [ ] 为 main 启用分支保护，要求 test 工作流通过；
- [ ] 禁止强制推送和删除 main；
- [ ] 确认 Actions 权限保持最小化，默认 GITHUB_TOKEN 仅 contents: read。

## v0.3.2 Release

- [ ] 创建带注释 Tag v0.3.2；
- [ ] 先创建 Draft Release，不立即发布；
- [ ] 上传 wheel、安装 ZIP、ZIP 单独哈希和 SHA256SUMS.txt；
- [ ] 从 Draft Release 下载资产并在新的临时目录复核哈希；
- [ ] 确认 Release 明确说明软件 P1 不等于目标账户现场验收；
- [ ] 确认信用自动交易默认关闭；
- [ ] 确认发布资产中不存在旧 wheel 或同名错误 ZIP；
- [ ] 维护者最终确认后再点击 Publish release。

## 发布后

- [ ] 从公开 Release 执行一次全新安装；
- [ ] 检查 README、文档相对链接、Issue 模板和安全报告入口；
- [ ] 保持真实部署处于 OBSERVE_ONLY，不把公开发布视为实盘授权；
- [ ] 记录发布提交、Tag、资产 SHA256 和验证时间。
