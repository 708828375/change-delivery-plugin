# Change Delivery Plugin

面向 Windows 与 Codex CLI 的研发交付编排插件。它把需求确认后的开发、测试环境验收、PR、Review 修复与上线审批连接为可恢复流程，同时把高风险操作保留为明确的人工确认点。

本仓库为私有分发包，许可证标记为 `UNLICENSED`，仅限获授权团队成员安装和使用。

## 先了解当前版本

当前 MVP 面向 Windows 与 Codex CLI，推进到 `READY_TO_MERGE` 后停止，不自动合并或上线生产环境。

想先理解核心机制，可以运行 [离线交付门禁演示](docs/first-demo.md)：使用合成数据验证批准快照不匹配、缺失验收证据和证据齐全时的状态检查结果。演示不需要安装 Windows 运行主机或连接模型与 GitHub。

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

当前稳定发布为 `v0.2.2`。克隆固定 tag 后执行共享安装器：

```powershell
git clone --branch v0.2.2 --depth 1 `
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

### 连续推进

设计方案获得明确批准后，使用 `run` 让固定状态机连续推进，无需人工逐段调用 `resume`：

```powershell
& "$env:USERPROFILE\.codex\change-delivery\bin\change-delivery.ps1" `
  run <run-id> --json
```

一次 `run` 最多处理 32 步，并在以下情况自动暂停：等待设计批准、需要人工决策、依赖或阶段阻塞、等待外部 CI/Review、状态未变化、到达终态或 `READY_TO_MERGE`。返回结果包含 `steps_processed` 和 `step_actions`，便于确认本轮实际推进了哪些阶段。`resume` 仍保留用于单步诊断。

内部 Review 发现可执行问题时，会进入固定闭环：修复 → 本地验证 → 重新 Review。默认最多自动修复 3 轮，达到上限后转为人工决策。PR 标题和正文通过契约校验后，由运行主机自动创建 Draft PR。当前 MVP 始终停在 `READY_TO_MERGE`，不会自动合并或上线生产环境。

### 阶段 Skill 定制

业务阶段与内置 Skill 的对应关系如下：

| 阶段 | Skill | 可定制内容 |
| --- | --- | --- |
| 需求方案与计划 | `delivery-planning` | 设计和实施计划要求 |
| 开发实现 | `delivery-implement` | TDD、编码和项目约束 |
| 本地验证补充检查 | `delivery-local-validation` | 权威命令通过后的项目补充要求 |
| 内部 Review | `delivery-review` | Review 重点和严重级别 |
| Review 修复 | `delivery-review-fix` | 本轮可执行问题的修复要求 |
| Commit 内容 | `delivery-commit` | commit message 格式、中文标题和正文 |
| PR 内容 | `delivery-pr` | PR 标题、正文、章节和检查清单 |
| 测试部署预检 | `delivery-test-deploy` | 测试分支推送前的只读项目检查 |
| 测试环境验收 | `project-test-acceptance` | 测试场景、资源和清理要求 |
| 合并前检查 | `delivery-ready-check` | 合并门禁汇总和补充风险检查 |

当前版本每个阶段仍只调用一个固定 Skill，不包含追加 Skill 调度。可以直接修改仓库中对应 Skill 的中文提示词来保留个人或团队习惯；复杂规则优先放入该 Skill 自己的 `references/`，再由 `SKILL.md` 明确说明始终读取或按变更类型读取。这样不会增加阶段调用次数，也不会形成多个实现写入者。

内置 `references/` 已按阶段整理团队开发、接口测试、中文 commit/PR、ExampleProject 异步 Review、测试部署和功能验收规则。项目专项规则必须带明确触发条件，例如只有改动涉及 Celery、Outbox 或 Watchdog 时才读取 ExampleProject 异步 Review 清单，不能把专项要求无条件应用到其他项目。

阶段 `SKILL.md` 应优先描述目标、完成/退出判据、所需证据和不可逆权限边界，不重复运行主机已经注入的 JSON Schema、状态映射和动作白名单。模型能够从当前仓库直接判断的通用开发步骤不必逐条罗列；只有团队约定、项目陷阱和验收口径进入 `references/`，并通过明确条件按需加载。

Skill 及其 `references/` 只能定制阶段内容或收紧检查，不能改变固定状态、remote、分支、push 策略、主机权限和副作用边界，也不能直接执行 commit、push、创建 PR 或 merge。个人 SSH 路径、密钥、密码和服务器连接信息不得写入引用文件，应由项目配置或运行主机批准的凭据名称提供。

修改 Skill 后需要重新运行 `install-shared-plugin.ps1`，并新建 Codex CLI 会话；旧会话和未重新安装的发布副本不会自动获得修改。

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

Marketplace 固定在 `v0.2.2`。发布新版本时必须同步更新 Marketplace ref、插件版本、Python 包版本与 README，再重新安装并新建 Codex CLI 会话。

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
