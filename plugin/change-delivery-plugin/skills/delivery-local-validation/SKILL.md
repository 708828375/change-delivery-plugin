---
name: delivery-local-validation
description: 当 change-delivery 已由运行主机执行全部 required_pre_pr_checks，需要依据准确 SHA 和脱敏后的权威命令事实应用项目补充验证要求时使用。
---

# 交付本地验证补充检查

只读检查运行主机提供的准确 SHA、tested tree 和权威命令事实。用户可以在此 Skill 中补充项目特有的验证要求，但不能替代或放宽 `required_pre_pr_checks`。

完整读取 [references/project-validation-checklist.md](references/project-validation-checklist.md)，依据变更类型选择适用检查项。只能评价运行主机提供的事实和只读可见内容，不得把参考清单描述成已经执行的命令。

## 输出要求

1. 核对输入绑定的准确 SHA 和权威命令事实。
2. 只依据已提供事实和只读检查判断补充要求是否满足。
3. 返回完整且权威的 `StageResult` JSON；缺少 Schema 或绑定事实时返回 `blocked`。

## 强制边界

- 不得改写命令退出码，不得把失败的权威检查改写为 `passed`。
- 不得修改文件、commit、push、部署、创建 PR 或写入数据库。
- 不得扩大已批准范围，不得声称未提供的测试已经通过。
- 不得执行未经运行主机批准的写操作。
