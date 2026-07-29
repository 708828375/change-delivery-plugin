---
name: delivery-review
description: Use when an exact pending change-delivery tree needs internal review against its approved requirement, design, plan, and validation evidence.
---

# Delivery Review

Perform a read-only review of the exact pending tree supplied by the host.
Review authority does not include implementation authority.

## Review

1. Bind the review to the supplied tree identity and expected parent. If the
   tree changes, stop; the replacement requires a fresh review.
2. Compare the exact pending tree with the approved requirement, design, and
   implementation plan.
3. Inspect correctness, regressions, security, concurrency, error handling,
   tests, and scope compliance. Distinguish actionable findings from
   non-actionable observations.
4. Classify actionable findings by severity. A `critical` or `important`
   finding prevents `passed`.
5. Return only JSON conforming exactly to the complete authoritative
   `StageResult` schema and status policy injected by the host. Its only top-level fields
   are `status, summary, evidence, findings, next_action`; represent
   observations as findings rather than adding a field.

## Hard boundaries

- Do not modify any file, even for a comment, spelling, formatting, or
  supposedly harmless fix.
- Do not stage, commit, push, deploy, change a PR, merge, or write runtime
  state.
- Do not run commands that mutate the worktree. Use only read-only inspection
  and evidence already supplied by the host.
- Do not invent evidence or turn narrative claims into an `EvidenceRef`. If
  the complete schema, status mapping, or evidence placeholders are absent,
  do not guess JSON. Return a plain-text blocked handoff requesting the
  missing host contract.
- Do not mark an actionable finding resolved because the user accepts it or
  time is short. Review a new exact tree after the implementation stage fixes
  it.
- Do not return `passed` while any actionable `critical` or `important`
  finding remains. Return `failed` or `blocked` with a precise next action.
