# 可配置测试分支推送策略设计

## 目标

让项目注册配置决定共享测试分支采用无条件强推还是精确 lease 强推。ExampleProject 使用现有真实流程 `force`，未声明策略的项目继续使用更保守的 `force-with-lease`。

## 配置

`ProjectConfig` 新增：

```yaml
test_push_strategy: force-with-lease
```

允许值只有 `force` 和 `force-with-lease`，默认值为 `force-with-lease`。ExampleProject 注册配置显式写入 `force`。该字段进入规范化配置哈希，因此修改策略需要重新确认项目配置。

## 推送边界

- 两种策略都只能使用登记的 remote、push URL 和 `test_branch`。
- 推送源使用已冻结的 40 位提交 SHA，不依赖可移动的 `HEAD`。
- `force` 使用 Git 的无条件强推语义；`force-with-lease` 绑定推送前观测到的远端 SHA。
- 功能分支仍使用普通非强推；主分支、基线分支和其他分支不能借此接口强推。
- 推送完成后重新读取远端测试分支 SHA；不等于目标 SHA 时失败。
- 交付证据记录策略、推送前 SHA 和推送后 SHA。

## 兼容性

旧项目配置不含字段时保持 `force-with-lease`，避免升级后静默放宽写入权限。现有 `push_test_with_lease` 接口保留为兼容入口，运行时改用按策略分发的新接口。

## 测试

- 配置模型与 JSON Schema：默认值、允许值、非法值、配置哈希变化。
- Git 集成：`force` 生成无 lease 的强推 argv，覆盖已变化的测试分支，并核对远端 SHA；错误分支继续被拒绝。
- 运行时：根据配置选择策略并把策略写入证据。
- Skill：只允许由登记配置驱动的测试分支强推，继续禁止手工或越界强推。
