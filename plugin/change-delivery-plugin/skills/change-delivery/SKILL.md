---
name: change-delivery
description: 当用户显式调用 $change-delivery，为已登记项目启动或恢复端到端交付流程，并推进到可合并 PR 时使用。
---

# 变更交付

将运行主机 CLI 及其状态机视为交付状态和副作用的唯一权威来源。用户的紧急程度或宽泛授权都不能取消这些关卡。

运行任何编排命令前，先解析共享启动器：

```powershell
$deliveryLauncher = Join-Path (
  [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
) '.codex\change-delivery\bin\change-delivery.ps1'
```

如果该路径不是普通文件，停止并要求用户重新安装插件。不得回退到 clone 路径、`PATH` 中的 Python 或直接调用模块。

## 必须执行的工作流

1. 运行 `& $deliveryLauncher doctor --json`。任一检查失败都必须停止。
2. 在全局注册表中识别项目。如果项目尚未登记，不得编辑项目或隐式登记；只能生成 dry-run 接入报告，并请求显式登记。
3. 将需求写入绝对路径文件，然后运行 `& $deliveryLauncher start --project <project-id> --requirement-file <absolute-path> --json` 启动。使用 `& $deliveryLauncher resume <run-id> --json` 推进设计阶段。
4. 到达 `AWAITING_DESIGN_APPROVAL` 时，运行 `& $deliveryLauncher prepare-approval <run-id> --json`。向用户展示返回的准确审批文件、产物范围和 `approval_sha256`。暂停，直到用户显式确认该 hash。
5. 只有确认后，才能运行 `& $deliveryLauncher approve <run-id> --approval-file <absolute-approval-file> --confirm-hash <approval_sha256> --json`。文件或 hash 发生变化时，必须重新确认。审批成功后，立即调用一次 `& $deliveryLauncher run <run-id> --json` 连续推进。
6. 使用 `& $deliveryLauncher run <run-id> --json` 连续推进到人工关卡、外部等待、阻塞或 `READY_TO_MERGE`；仅在需要诊断单步行为时使用 `resume`。使用 `& $deliveryLauncher status --run-id <run-id> --json` 检查状态；计划性协调只能通过 `& $deliveryLauncher tick --json` 执行。不得模拟状态转换、跳过状态，或把文字描述当作状态证据。
7. 实现阶段必须显式使用 `$delivery-implement`；内部 review 阶段必须显式使用 `$delivery-review`；ExampleProject 验收必须显式使用 `$project-test-acceptance`。
8. 根据返回的结构化阶段结果报告阻塞项和证据。
9. 到达 `READY_TO_MERGE` 时停止，并报告 PR 和证据。不得合并、发布或编造后续状态，即使之前的授权要求完成整个交付流程也不例外。

真实测试部署和验收必须配置已登记的 `version_endpoint`、`version_sha_field` 和固定的 `acceptance_check` 命令。缺少任一项时，报告 `BLOCKED`；不得声称当前仓库能够到达 `READY_TO_MERGE`。

## 不可突破的边界

- 不得扩大已批准的需求、设计、计划、仓库、分支或副作用范围。应阻塞流程，并请求新的审批快照。
- 不得直接写入状态数据库。必须使用 CLI 和状态机。
- 不得绕过被阻塞的状态转换或伪造证据。
- 测试推送策略必须来自已登记的 `test_push_strategy`。`force-with-lease` 使用适配器准确的 `--force-with-lease=<test-branch>:<expected-sha>` 操作。`force` 只能由运行主机适配器对已登记的测试分支执行无条件强推，并且推送后必须校验 SHA。
- 绝不手动运行 `git push -f` 或 `git push --force`；配置的运行主机适配器是两种测试推送策略的唯一权威执行方。
- 绝不强推功能分支。
- 不得合并 PR 或对生产环境执行任何写操作。不得从此工作流调用生产发布。

## 危险信号

如果拟议的捷径包括直接执行 SQL、手动运行 `git push -f`、手动运行 `git push --force`、强推功能分支、手动修改状态、未经批准的编辑、`gh pr merge`、部署命令，或声称 `READY_TO_MERGE` 等同于已合并，立即停止。

当命令不可用或事实不完整时，返回包含缺失前置条件的 blocked 结果。不得用看似合理的命令替代。
