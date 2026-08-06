---
name: delivery-test-deploy
description: 当 change-delivery 需要为已登记测试环境生成部署预检结论时使用。
---

# 测试部署预检

读取 `references/project-deploy-preflight.md`，核对运行主机给出的 `remote`、`test_branch`、`test_push_strategy`、当前功能 SHA 与预期 SHA。只做只读预检，不直接运行 push 或部署命令。

退出判据：已确认推送目标和策略均来自项目注册事实，基线满足所选策略，且可以让主机执行推送后 SHA 校验；否则返回具体阻塞项。实际测试分支写入只能由运行主机适配器完成。
