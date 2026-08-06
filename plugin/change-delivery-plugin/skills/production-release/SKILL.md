---
name: production-release
description: 当用户显式调用 $production-release，检查已合并运行是否满足未来生产发布条件时使用。
---

# 生产发布预检

版本 1 仅提供只读预检，不包含任何生产写入路径。

```powershell
$deliveryLauncher = Join-Path (
  [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
) '.codex\change-delivery\bin\change-delivery.ps1'
```

启动器缺失时停止并提示重新安装插件。要求用户提供 run ID，然后执行 `& $deliveryLauncher release <run-id> --json`。

报告合并标识、测试验收 SHA 关系、配置门禁、缺失前提和脱敏证据。退出判据是形成可审计的预检结论：通过只表示满足未来独立发布能力的前提，不代表已部署或已上线。

生产写入是不可跨越的权限边界：本 Skill 不部署、回滚、创建 tag、发布产物、修改服务器或生产数据，也不把任何授权解释为这些权限。
