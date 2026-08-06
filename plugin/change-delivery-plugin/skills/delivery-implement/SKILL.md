---
name: delivery-implement
description: 当 change-delivery 进入已批准实现阶段，需要在当前 worktree 内完成代码、测试和开发记录时使用。
---

# 开发实现

你是当前 worktree 的唯一写入者。以批准的 requirement、design 和 implementation plan 为范围，以仓库现有约定和测试为事实来源。

先阅读 `references/team-development-rules.md`。已批准计划包含测试账号、第三方依赖、临时数据或专用测试资产时，再读取 `references/test-assets.md`。

按风险选择实现路径：能稳定隔离的行为变更应先建立失败回归，再做最小实现；文档、配置或机械变更可直接修改，但仍需提供与风险匹配的验证。避免为了满足计划文字而扩大公共接口、数据迁移或依赖范围。

退出判据：批准范围内的行为已经实现；相关回归证据可复现；变更文件与当前 SHA 可识别；遗留风险、未执行验证和外部阻塞均已明确记录。之后将控制权交回运行主机，不自行 commit、push、部署或改变流程状态。
