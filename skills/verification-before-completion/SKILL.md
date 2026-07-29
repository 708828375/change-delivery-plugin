---
name: verification-before-completion
description: Use when a change-delivery worktree must be checked against its approved scope before review; authoritative command execution remains controlled by the host orchestrator.
---

# Delivery Verification

Inspect the pending worktree and approved artifacts. Do not claim success from
memory or old output. Treat only evidence supplied for this execution and tree
as current.

Do not edit files, commit, push, deploy, merge, or manufacture evidence. The
host orchestrator runs required commands and binds their identities, nonce, and
tree. Report discrepancies as structured findings and return the required
structured stage result.
