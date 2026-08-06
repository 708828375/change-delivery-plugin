---
name: delivery-planning
description: 当 change-delivery 需要把批准的 requirement.md 转换为 design.md 和 implementation-plan.md 时使用。
---

# 方案设计

目标是在批准范围内产出可实施、可验证、可审查的 `design.md` 与 `implementation-plan.md`。先阅读 `references/project-analysis-and-test-strategy.md`，再按当前仓库事实选择分析深度。

完成判据：方案明确行为边界、关键取舍、受影响接口或数据、验证策略、实施顺序与风险；计划中的每项任务都有可观察结果。未知决策如实列为待确认项。

本阶段只写入上述设计产物，不修改产品代码。产物写入后返回路径与摘要，交由运行主机生成审批快照；只有用户对准确的 `approval_sha256` 显式批准后才能进入开发。
