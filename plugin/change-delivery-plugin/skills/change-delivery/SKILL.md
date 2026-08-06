---
name: change-delivery
description: 当用户显式调用 $change-delivery，为已登记项目启动、恢复并推进端到端交付流程，直至 READY_TO_MERGE 时使用。
---

# 变更交付

运行主机 CLI、状态机及其证据是交付状态与副作用的权威来源。此 Skill 负责驱动主机，不在对话中模拟状态流转。

从 Windows Known Folder 解析共享启动器：

```powershell
$deliveryLauncher = Join-Path (
  [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
) '.codex\change-delivery\bin\change-delivery.ps1'
```

启动器缺失时停止并提示重新安装插件，不从 clone 路径或系统 Python 回退。

## 推进方式

1. 执行 `& $deliveryLauncher doctor --json`，以检查结果决定是否继续。
2. 识别全局注册表中的项目。未登记项目只生成 dry-run 接入报告，等待用户显式登记。
3. 将需求写入绝对路径文件，通过 `start --project <project-id> --requirement-file <absolute-path> --json` 启动；已有 run 使用 `run <run-id> --json` 连续推进，`resume` 只用于单步诊断。
4. 到达 `AWAITING_DESIGN_APPROVAL` 后执行 `prepare-approval`，展示准确的审批文件、产物范围与 `approval_sha256`。只有用户明确确认同一 hash 后才执行 `approve <run-id>`；内容变化需重新确认。
5. 用 `run` 推进到人工关卡、外部等待、阻塞或 `READY_TO_MERGE`。用 `status --run-id <run-id> --json` 查询事实，计划性协调仅使用 `tick --json`。

实现、内部 Review 与 ExampleProject 验收分别显式加载 `$delivery-implement`、`$delivery-review`、`$project-test-acceptance`。各阶段的状态、证据和副作用由主机提供的阶段契约约束。

## 停止边界

- 需求、设计、仓库、分支或副作用超出批准快照时，等待新的审批快照。
- 状态数据库只由 CLI 与状态机写入。
- 测试分支推送只由主机按已登记的 `remote`、`test_branch` 和 `test_push_strategy` 执行并核对 SHA；功能分支不强推。
- 到达 `READY_TO_MERGE` 即停止并报告 PR 与证据。本流程不合并 PR，也不执行生产写入。

命令或事实不完整时返回 blocked，并列出缺失前提。
