---
name: delivery-pr
description: 当 change-delivery 需要依据批准产物和验证证据生成自定义 Draft PR 内容时使用。
---

# PR 内容

读取 `references/chinese-pr-template.md`，使用运行主机提供的准确事实生成标题与正文。需求、实现、测试、部署注意事项和风险都必须能追溯到权威证据；未执行、失败或待确认事项应明确标注。

保持项目自定义模板的章节与语气，在事实不足时返回缺失项。仅生成 PR 内容，由运行主机负责创建或更新 Draft PR；本 Skill 不调用托管平台、不 push，也不修改仓库。
