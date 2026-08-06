---
name: delivery-test-deploy
description: 当 change-delivery 即将把准确 SHA 推送到已登记测试分支，需要对固定 remote、test_branch 和 test_push_strategy 执行只读预检时使用。
---

# 测试环境部署预检

仅检查运行主机提供的 `remote`、`test_branch`、`test_push_strategy` 和预期 SHA 是否符合项目要求。此 Skill 不拥有推送权限，也不能替换任何已登记值。

完整读取 [references/project-deploy-preflight.md](references/project-deploy-preflight.md)，应用其中不产生副作用的部署前检查。环境连接信息和凭据只能来自运行主机登记事实，不能使用个人机器路径或猜测值。

## 输出要求

1. 核对预期 SHA 以及已登记测试部署事实。
2. 发现项目约束不满足时返回 `failed` 或 `blocked`，并说明需要运行主机处理的事实。
3. 返回完整且权威的 `StageResult` JSON；缺少 Schema 或绑定事实时返回 `blocked`。

## 强制边界

- 不得更改 remote、test_branch、test_push_strategy 或预期 SHA。
- 不得手动执行 `git push -f`、`git push --force` 或任何 push 命令。
- 不得修改文件、commit、创建 PR、部署生产环境、合并或写入数据库。
- Skill 通过只代表只读预检通过；实际测试分支推送仍由运行主机执行并校验 SHA。
