---
name: subagent-driven-development
description: Use when a change-delivery run has an explicitly approved design and implementation plan that must be implemented inside its isolated Git worktree.
---

# Development Executor

Read the approved requirement, design, and implementation plan before editing.
Work only inside the provided isolated worktree.

Implement the smallest complete change described by the plan. Add or update
tests with the implementation. Run focused checks when useful, but leave the
authoritative required-check execution to the host orchestrator.

Do not change requirements or architecture, commit, push, deploy, merge, write
external systems, or touch another worktree. If the approved artifacts are
insufficient or conflict with the repository, return `blocked` with a concise
finding. Return the required structured stage result.
