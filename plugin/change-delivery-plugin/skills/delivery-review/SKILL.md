---
name: delivery-review
description: 当 change-delivery 需要对准确代码树执行只读内部 Review，并输出可验证 findings 时使用。
---

# 内部 Review

以运行主机提供的准确树、批准产物、diff 与验证证据为审查边界。先完整发现问题，再分级为 `critical`、`important`、`minor`，避免看到首个问题便提前结束。

始终读取 `references/team-review-checklist.md`。变更涉及 Celery、Outbox、Watchdog、WebSocket、断点续跑、自动恢复、积分扣减或大表扫描时，再读取 `references/project-async-review.md`；未触发这些条件时不加载专项规则。

每条 finding 应包含代码证据、触发条件、实际影响和建议方向；无法从当前路径证明的问题不作为缺陷。Review 保持只读，不修代码、不改变 Git 或运行状态。

退出判据：已覆盖本次变更的高风险路径；不存在尚未记录的可操作问题；只有在没有 `critical` 或 `important` finding 时才通过。任何修复都交给后续 `$delivery-review-fix`。
