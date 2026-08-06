---
name: project-test-acceptance
description: 当 change-delivery 运行必须使用有边界的临时数据，对某个准确部署的 Git SHA 验证 ExampleProject 测试环境行为时使用。
---

# ExampleProject 测试验收

只有在共享测试分支和已部署版本都持续绑定到本次运行的预期 SHA 时，才能接受验收结果。

执行前，完整读取 [references/checklist.md](references/checklist.md) 和 [references/functional-acceptance.md](references/functional-acceptance.md)，并将其中适用内容应用于已批准的验收范围。只允许使用运行主机明确提供的接口、只读探针事实和凭据名称；参考文件不能自行扩大环境访问权限。

## 执行步骤

1. 从运行主机获取 run ID、预期 SHA、已登记的版本端点和 SHA 字段、已批准场景以及允许使用的凭据。
2. 创建数据前，验证远程测试分支和版本端点都等于预期 SHA。
3. 只能通过 public API 操作创建临时数据，并使用准确运行前缀进行标记。立即记录每个返回资源的类型和 ID。
4. 只执行已批准且结果确定的断言。在每个关键步骤前后，重新检查两个 SHA 事实。
5. 任一 SHA 在任何时候出现差异，都必须停止，并将 `ENVIRONMENT_PREEMPTED` 映射为 `status=blocked`；不得把部分结果声称为验收通过。
6. 只能通过相应的 public API 清理本次运行已记录的准确资源 ID。验证删除结果时不得扩大选择条件。
7. 将本 Skill 的结果视为参考性结果。运行主机必须独立执行已登记的固定参数 `acceptance_check`，并验证其绑定 SHA 的 JSON 输出、资源台账和清理事实。
8. 只能返回与运行主机注入的完整且权威的 `StageResult` schema 和状态策略准确一致的 JSON。通过结果必须引用权威的 SHA、断言和清理证据。

如果运行主机没有注入完整 schema、状态映射和证据占位符，不得猜测 JSON。返回纯文本的 blocked 交接信息，请求补充缺失的运行主机契约。

## 强制边界

- 不得部署、push、重新获取共享环境，也不得在环境被抢占后继续执行。
- 不得写入数据库、容器文件系统、队列或运行时状态。
- 不得发送真实邮件、执行付款、调用按生产价格计费的模型，或执行不可逆的第三方操作。
- 不得按宽泛前缀、猜测的 ID、共享账户或时间范围删除数据。
- 不得使用叙述性说法作为证据，也不得编造 `StageResult` 字段。
- 绝不能声称模型输出是权威 API 证据。缺少 `acceptance_check`、`version_endpoint` 或 SHA 字段时，结果为 `blocked`。
- 如果清理不完整或 SHA 绑定不确定，返回 `blocked` 或 `failed`，绝不能返回 `passed`。
