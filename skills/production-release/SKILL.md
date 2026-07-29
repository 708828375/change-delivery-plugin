---
name: production-release
description: Use when the user explicitly invokes $production-release to inspect whether a merged change-delivery run is eligible for a future production release.
---

# Production Release

Version 1 is a read-only preflight. It contains no production-write path and
must always refuse one.

## Preflight

1. Require an explicit run ID and explicit invocation of this Skill.
2. Run `python -m change_delivery release <run-id> --json`.
3. Report the returned merge identity, test-acceptance SHA relationship,
   configuration gate, missing prerequisites, and sanitized evidence.
4. If preflight passes, report only that the run is eligible for a future
   separately implemented release path. State that the production write was
   refused.

## Hard boundaries

- Do not deploy, roll back, tag, publish an artifact, change a workflow,
  modify a server, or write production data.
- Do not run smoke tests against production; first-version scope ends at
  local read-only preflight.
- Do not mark the run released, deployed, complete, or successful in
  production.
- Do not translate `production_enabled=true`, a passed preflight, credentials,
  urgency, or written authorization into permission to write.
- Do not invent a deployment command or call another Skill to bypass this
  refusal.

Even when every check passes and the user repeats authorization, always refuse
the production write and return the preflight result plus the missing
implementation/authorization boundary.
