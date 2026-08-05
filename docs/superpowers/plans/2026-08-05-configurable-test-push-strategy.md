# 可配置测试分支推送策略实现计划

> **面向 AI 代理的工作者：** 按 TDD 红绿循环逐项实现，并在完成前运行完整回归。

**目标：** 让 ExampleProject 使用受约束的无条件测试分支强推，同时保留共享默认的精确 lease 策略。

**架构：** `ProjectConfig` 提供策略，`RuntimeTestEnvironmentPort` 负责选择，`GitAdapter` 负责构造受约束 argv 和推送后校验。配置哈希、证据与 Skill 文档同步表达该选择。

**技术栈：** Python 3.12、Pydantic、Git、pytest、PowerShell 安装器。

---

### 任务 1：配置契约

- [x] 为默认值、`force`、非法值和哈希差异编写失败测试。
- [x] 在 `ProjectConfig` 增加 `test_push_strategy` Literal 字段。
- [x] 重新生成 `schemas/project.schema.json`。
- [x] 运行配置和领域测试。

### 任务 2：Git 推送实现

- [x] 编写无条件强推、远端覆盖和分支边界的失败集成测试。
- [x] 新增策略化 `push_test`，保留 lease 兼容入口。
- [x] 两种策略均执行推送后远端 SHA 校验。
- [x] 运行 Git 工作流集成测试。

### 任务 3：运行时与证据

- [x] 编写运行时按 `force` 分发并记录策略的失败测试。
- [x] 让测试环境端口从配置选择推送策略。
- [x] 更新测试替身并运行编排恢复测试。

### 任务 4：产品配置和 Skill

- [x] 更新 ExampleProject 配置示例为 `force`。
- [x] 更新 Skill 红线，使配置驱动的测试分支 `force` 成为唯一例外。
- [x] 更新插件 Skill 测试与 README 配置说明。

### 任务 5：验证

- [x] 运行相关测试。
- [x] 运行完整 pytest。
- [ ] 清理源码目录产生的 Python bytecode 后执行共享安装器烟雾测试。
- [x] 检查 `git diff --check` 和最终差异。
