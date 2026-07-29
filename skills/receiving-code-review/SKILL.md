---
name: receiving-code-review
description: Use when a change-delivery run has explicit CI failures or actionable review feedback that must be fixed inside the approved isolated worktree.
---

# Review Fix Executor

Read the actionable CI or review facts and verify each against current code.
Apply only changes needed to resolve confirmed feedback within approved scope.
Add or update tests for behavior changes.

Do not alter requirements or architecture, address merely pending checks as
failures, commit, push, deploy, merge, resolve remote threads, or touch another
worktree. Return `blocked` when feedback requires a new decision. Otherwise
return the required structured stage result.
