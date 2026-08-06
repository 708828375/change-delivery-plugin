---
name: production-release
description: 当用户显式调用 $production-release，检查已合并的 change-delivery 运行是否满足未来生产发布的条件时使用。
---

# 生产发布

版本 1 仅执行只读预检。它不包含生产写入路径，并且必须始终拒绝此类操作。

从当前 Windows Known Folder 解析共享启动器：

```powershell
$deliveryLauncher = Join-Path (
  [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
) '.codex\change-delivery\bin\change-delivery.ps1'
```

如果该路径不是普通文件，停止并要求用户重新安装插件。不得回退到 clone 路径或系统 Python。

## 预检

1. 要求显式提供 run ID，并显式调用此 Skill。
2. 运行 `& $deliveryLauncher release <run-id> --json`。
3. 报告返回的合并标识、测试验收 SHA 关系、配置关卡、缺失的前置条件和经过脱敏的证据。
4. 如果预检通过，只能报告本次运行符合未来单独实现的发布路径的条件，并说明生产写操作已被拒绝。

## 强制边界

- 不得部署、回滚、创建 tag、发布产物、修改工作流、修改服务器或写入生产数据。
- 不得对生产环境运行冒烟测试；第一版范围只到本地只读预检为止。
- 不得将本次运行标记为已发布、已部署、已完成或在生产环境中成功。
- 不得将 `production_enabled=true`、通过预检、凭据、紧急程度或书面授权解释为生产写入权限。
- 不得编造部署命令，也不得调用其他 Skill 绕过此拒绝。

即使每项检查都通过，并且用户反复授权，也必须始终拒绝生产写操作，只返回预检结果以及缺失的实现/授权边界。
