# Change Delivery Plugin

面向 Windows 与 Codex CLI 的研发交付编排插件。它把需求确认后的开发、测试环境验收、PR、Review 修复与上线审批连接为可恢复流程，同时把高风险操作保留为明确的人工确认点。

本仓库为私有分发包，许可证标记为 `UNLICENSED`，仅限获授权团队成员安装和使用。

## 环境要求

- Windows PowerShell 5.1 或更高版本。
- Codex CLI 已安装并可执行。
- Python 3.12 或更高版本，仅用于首次安装时创建插件专用虚拟环境。
- Git 与 GitHub CLI；私有仓库访问需先完成 GitHub 认证。

```powershell
gh auth login -h github.com
gh auth status
```

## 安装

当前稳定发布为 `v0.2.0`。克隆固定 tag 后执行共享安装器：

```powershell
git clone --branch v0.2.0 --depth 1 `
  https://github.com/708828375/change-delivery-plugin.git
Set-Location .\change-delivery-plugin
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\install-shared-plugin.ps1
```

安装器会创建以下当前用户专属内容：

- `%USERPROFILE%\plugins\change-delivery-plugin`：受管技能发布副本。
- `%USERPROFILE%\.codex\change-delivery\runtime\venv`：专用 Python 环境。
- `%USERPROFILE%\.codex\change-delivery\host`：与 clone 解耦的运行主机。
- `%USERPROFILE%\.codex\change-delivery\bin\change-delivery.ps1`：稳定启动器。

安装完成后请新建 Codex CLI 会话，再使用 `$change-delivery`。

## 使用与诊断

在项目仓库中新建 Codex CLI 会话，描述需求并调用：

```text
使用 $change-delivery 启动这个需求的交付流程。
```

稳定启动器可以直接查看命令：

```powershell
& "$env:USERPROFILE\.codex\change-delivery\bin\change-delivery.ps1" --help
```

项目注册完成后，可运行只读诊断。`doctor` 会验证项目登记、Git 远端和工具绑定；尚未登记项目时返回受控错误是预期行为。

```powershell
& "$env:USERPROFILE\.codex\change-delivery\bin\change-delivery.ps1" doctor --json
```

项目配置可选择共享测试分支的推送策略：

```yaml
# 默认值；远端分支发生变化时拒绝覆盖
test_push_strategy: force-with-lease

# ExampleProject 当前测试环境流程；只允许用于登记的 test_branch
test_push_strategy: force

# 内部 Review 发现问题后，最多自动修复、重新测试并再次 Review 3 轮
max_internal_review_fix_attempts: 3
```

`force` 由运行主机使用冻结的提交 SHA 执行，并在推送后重新核对远端
测试分支 SHA；它不会放宽功能分支、基线分支或其他分支的强推限制。

如需启用自动 tick，请从已安装 host 执行计划任务安装脚本。推荐使用当前用户的
Codex 认证文件，因为 Task Scheduler 启动的独立进程不会继承安装 PowerShell 的
临时 `OPENAI_API_KEY`：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File "$env:USERPROFILE\.codex\change-delivery\host\scripts\install-scheduled-task.ps1" `
  -AuthMode auth_file `
  -AuthSource "$env:USERPROFILE\.codex\auth.json"
```

计划任务固定执行已安装 host 中经哈希锚定的 `run-tick.ps1`，不会执行 clone 中的脚本。
`api_key` 模式只适用于管理员已经为计划任务进程独立、安全配置环境变量的场景，
不要把 key 写入脚本、仓库或任务参数。

## 升级

获取新的已审核 tag，在新 clone 中重新执行 `install-shared-plugin.ps1`。安装器会先在 staging 中验证新运行时，再替换 host、专用虚拟环境和技能副本；项目与运行数据保持在用户数据目录中。

Marketplace 固定在 `v0.2.0`。发布新版本时必须同步更新 Marketplace ref、插件版本、Python 包版本与 README，再重新安装并新建 Codex CLI 会话。

## 卸载

默认卸载插件注册、计划任务、host、venv 与稳定启动器，但保留 `projects`、`runs`、`logs` 和 `state`：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\uninstall-shared-plugin.ps1
```

只有确认不再需要任何历史数据时才使用 `-PurgeData`。该操作会再次触发 PowerShell 高风险确认：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\uninstall-shared-plugin.ps1 -PurgeData
```

卸载器拒绝删除身份不符、越界或包含链接/reparse point 的目录。
