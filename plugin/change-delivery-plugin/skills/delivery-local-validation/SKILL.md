---
name: delivery-local-validation
description: 当 change-delivery 需要在当前 SHA 上执行项目权威本地验证并形成证据时使用。
---

# 本地验证

从运行主机读取权威命令事实，并结合 `references/project-validation-checklist.md` 判断证据是否覆盖本次风险。只执行已批准的只读验证，不修改源码、测试或配置。

退出判据：记录当前 SHA、逐条命令、退出码和关键输出；所有必需检查通过，或把失败定位为可操作的阻塞项。缺少命令、依赖或环境时如实 blocked，不用临时替代命令把结果包装成通过。
