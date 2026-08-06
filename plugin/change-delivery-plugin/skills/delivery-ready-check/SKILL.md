---
name: delivery-ready-check
description: 当 change-delivery 需要汇总当前 SHA 的证据并判断是否满足合并门禁时使用。
---

# 合并就绪检查

读取 `references/merge-readiness.md`，围绕当前 SHA 建立证据矩阵，核对批准范围、本地验证、内部 Review、测试部署、环境验收、Draft PR 与外部检查是否属于同一提交链。

退出判据：所有必需合并门禁都有可追溯证据，且不存在未处理的 `critical`、`important`、环境抢占或 SHA 漂移。Skill 只返回检查结论；`READY_TO_MERGE` 只能由运行主机状态机确认，不代表 PR 已合并。
