---
name: delivery-ready-check
description: 当 change-delivery 已收集当前准确 SHA 的全部权威合并门禁，需要生成只读汇总并应用项目合并前补充要求时使用。
---

# 可合并前补充检查

只读检查当前准确 SHA 的合并门禁证据，并生成补充风险汇总。此 Skill 只能收紧流程；最终 `READY_TO_MERGE` 仍由运行主机重新读取全部权威证据后判断。

完整读取 [references/merge-readiness.md](references/merge-readiness.md)，核对开发记录、验证、Review、测试环境验收、资源清理和残留风险是否已被当前 SHA 的证据覆盖。

## 输出要求

1. 核对每项合并门禁是否绑定同一准确 SHA。
2. 根据项目要求指出剩余风险，不得伪造或替换任何权威证据。
3. 返回完整且权威的 `StageResult` JSON；缺少 Schema 或门禁事实时返回 `blocked`。

## 强制边界

- 不得 merge，不得调用合并命令，也不得声称 PR 已合并。
- 不得修改 PR、Review、CI 状态、分支、标签或保护规则。
- 不得修改文件、push、部署或写入运行时状态。
- Skill 返回 `passed` 不等于 `READY_TO_MERGE`；运行主机必须重新验证全部合并门禁。
