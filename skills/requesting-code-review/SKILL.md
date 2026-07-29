---
name: requesting-code-review
description: Use when a validated change-delivery tree needs a read-only internal review for correctness, security, tests, and approved-scope compliance before commit.
---

# Internal Code Review

Review the exact pending tree against requirement, design, and implementation
plan. Inspect correctness, regressions, security, concurrency, error handling,
tests, and scope compliance.

Do not modify files, stage changes, commit, push, deploy, merge, or dismiss an
actionable concern to obtain a pass. Mark critical or important actionable
findings explicitly. Return `passed` only when none remain and provide the
required structured stage result.
