# 第一个演示：离线验证交付门禁

这个演示直接调用仓库中的状态迁移与证据函数，用合成数据检查四种情况：

1. 批准引用与当前快照的哈希一致时，可以进入 `APPROVED`。
2. 内容改变后，原批准引用不能通过新快照的检查。
3. 缺少 `acceptance_passed` 时，不能进入 `READY_TO_MERGE`。
4. 提供全部必需的证据类型时，可以通过 `READY_TO_MERGE` 的状态门禁。

演示沿用仓库现有的私有分发与 `UNLICENSED` 使用约定。它面向已有使用授权的读者，不改变项目许可。

## 准备

需要 Python 3.12 或更高版本，以及仓库声明的 `pydantic>=2.11,<3`。已有符合要求的 Python 环境时，无须安装 Codex 插件或 Windows 运行主机。

如果需要准备独立环境，可以在仓库根目录执行以下 PowerShell 命令。安装依赖这一步通常需要访问包源；下节演示本身不访问网络。

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install "pydantic>=2.11,<3"
```

## 运行

在仓库根目录，使用已经具备上述依赖的 Python 执行：

```powershell
python .\examples\verify_delivery_gates.py
```

如果使用了上面的独立环境，执行：

```powershell
& .\.venv\Scripts\python.exe .\examples\verify_delivery_gates.py
```

预期输出：

```text
[PASS] Matching approval snapshot accepted
[PASS] Changed approval snapshot rejected
[PASS] READY_TO_MERGE rejected when acceptance_passed is missing
[PASS] READY_TO_MERGE accepted with all 6 required evidence kinds
Offline checks passed (4/4). No delivery actions were invoked.
```

脚本会检查预期的拒绝原因；如果应当拒绝的迁移被接受，或返回了其他错误，会以非零状态退出。

## 为什么会得到这个结果

[`build_evidence_ref`](../src/change_delivery/evidence.py) 对合成内容计算 SHA-256，并生成 `explicit_approval` 类型的证据引用。进入 `APPROVED` 时，[`assert_transition`](../src/change_delivery/state_machine.py) 要求该引用的哈希与调用者提供的批准快照哈希匹配。

进入 `READY_TO_MERGE` 时，同一个函数要求调用者提供以下六种证据类型：

| 类型 | 对应条件 |
| --- | --- |
| `ci_passed` | CI 通过 |
| `acceptance_passed` | 验收通过 |
| `internal_review_passed` | 内部 Review 通过 |
| `threads_resolved` | Review 讨论已解决 |
| `mergeable` | 满足可合并条件 |
| `head_sha_verified` | 当前提交 SHA 已核对 |

演示删除其中的 `acceptance_passed`，确认门禁拒绝迁移；随后提供完整集合，确认门禁接受迁移。

## 验证范围

这里验证的是纯函数对状态迁移、快照哈希和证据类型的检查。数据全部在内存中合成，脚本不调用模型、Git、GitHub、浏览器或部署服务，也不创建真实运行记录。

传入完整的证据类型集合，不代表真实 CI、验收或 Review 已经完成。这个演示不验证外部证据的真实性、用户身份、持久化恢复、安装器或完整交付流程，也不生成真实批准。

完整插件仍面向 Windows 与 Codex CLI。README 中描述的当前 MVP 停在 `READY_TO_MERGE`，不会自动合并或上线生产；本演示只调用状态检查函数，不执行这些动作。

返回 [README](../README.md)。
