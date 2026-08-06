---
name: delivery-commit
description: 当 change-delivery 需要依据准确 Git 事实生成项目约定的 commit message 时使用。
---

# Commit 内容

以运行主机提供的准确事实、批准范围和已验证 diff 为依据，读取 `references/chinese-commit-convention.md` 后生成 commit 标题与正文。只描述真实完成的行为、原因和必要取舍，不把计划或未验证结果写成事实。

输出供主机创建 commit 的内容；主机负责附加审计 trailer、执行 Git 写入并核对结果。本 Skill 不运行 Git、不修改文件，也不自行推送。
