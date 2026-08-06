---
name: delivery-review-fix
description: 当内部 Review 产生可操作 findings，需要在原 worktree 修复并重新验证时使用。
---

# Review 修复

逐条读取 finding 与 `references/review-feedback-triage.md`，先验证问题是否存在、影响是否准确、是否落在批准范围内。证据不足或建议本身有风险时记录异议，不机械照做。

对确认的问题实施最小修复，并补充能复现原问题的回归证据。修复后重新验证；任何代码变化都必须进入重新 Review，不能沿用旧 SHA 的通过结论。

退出判据：已确认 findings 均有处理结论；修复与验证对应当前 SHA；范围外意见已明确阻塞或等待新审批。仅修改当前 worktree，不 commit、push、部署或自行关闭流程关卡。
