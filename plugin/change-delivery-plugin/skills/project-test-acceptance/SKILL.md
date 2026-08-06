---
name: project-test-acceptance
description: 当 change-delivery 需要对 ExampleProject 已部署测试 SHA 执行功能验收并形成证据链时使用。
---

# ExampleProject 测试验收

先读取 `references/checklist.md`；需要接口、WebSocket、日志或数据库侧的只读探针事实时再读取 `references/functional-acceptance.md`。以运行主机登记的验收命令和预期 SHA 为边界。

验收开始与关键动作前都核对版本端点。远程测试分支不再指向预期 SHA 时返回 `ENVIRONMENT_PREEMPTED`，停止把后续结果归因于本次变更。

按业务结果收集证据，而不是把请求成功、`X-Request-ID` 或任务已入队视为终态成功。为测试账号、临时数据、任务和外部依赖维护资源台账；只有批准的测试资产可以写入，生产环境始终只读。

退出判据：版本一致；批准的场景均有可复现的请求、终态或接收端证据；资源 ID、清理动作与残留项已记录。无法安全准备或清理数据、外部系统不可控、证据无法关联当前 SHA 时返回 blocked。

本 Skill 不执行测试分支推送、生产写入、PR 合并或流程状态写入。
