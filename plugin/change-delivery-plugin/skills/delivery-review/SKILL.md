---
name: delivery-review
description: 当需要依据已批准的需求、设计、计划和验证证据，对准确的待处理 change-delivery 树进行内部 review 时使用。
---

# 交付 Review

对运行主机提供的准确待处理树执行只读 review。review 权限不包含实现权限。

完整读取 [references/team-review-checklist.md](references/team-review-checklist.md)。当变更涉及 Celery、Outbox、Watchdog、WebSocket、异步恢复、积分扣费、大表扫描或索引时，再完整读取 [references/project-async-review.md](references/project-async-review.md)。未触发专项条件时，不得把 ExampleProject 专项要求强加给其他项目。

## Review 步骤

1. 将 review 绑定到提供的树标识和预期父节点。如果树发生变化，立即停止；替换后的树必须重新 review。
2. 将准确待处理树与已批准的需求、设计和实施计划进行比较。
3. 检查正确性、回归、安全性、并发、错误处理、测试和范围合规性。区分可执行发现和不可执行观察。
4. 按严重程度划分可执行发现。存在 `critical` 或 `important` 发现时，结果不能为 `passed`。
5. 只能返回与运行主机注入的完整且权威的 `StageResult` schema 和状态策略准确一致的 JSON。顶层字段只能是 `status, summary, evidence, findings, next_action`；观察项应表示为 finding，不得增加字段。

## 强制边界

- 不得修改任何文件，即使只是评论、拼写、格式或所谓无害的修复也不允许。
- 不得 stage、commit、push、部署、修改 PR、合并或写入运行时状态。
- 不得运行会改变 worktree 的命令。只能执行只读检查，并使用运行主机已提供的证据。
- 不得编造证据，也不得把叙述性说法转为 `EvidenceRef`。如果缺少完整 schema、状态映射或证据占位符，不得猜测 JSON。返回纯文本的 blocked 交接信息，请求补充缺失的运行主机契约。
- 不得将用户接受问题或时间紧迫视为可执行发现已解决。实现阶段修复后，必须 review 新的准确树。
- 只要仍有任何可执行的 `critical` 或 `important` 发现，就不得返回 `passed`。返回 `failed` 或 `blocked`，并给出准确的下一步操作。
