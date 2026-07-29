---
name: project-test-acceptance
description: Use when a change-delivery run must verify ExampleProject test-environment behavior for one exact deployed Git SHA using bounded temporary data.
---

# ExampleProject Test Acceptance

Accept behavior only while both the shared test branch and deployed version
remain bound to the run's expected SHA.

Before acting, read [references/checklist.md](references/checklist.md) completely
and apply it to the approved acceptance scope.

## Execute

1. Obtain the run ID, expected SHA, registered version endpoint and SHA field,
   approved scenarios, and allowed credentials from the host.
2. Verify the remote test branch and version endpoint both equal the expected
   SHA before creating data.
3. Create temporary data only through public API operations, tagged with the
   exact run prefix. Record every returned resource type and ID immediately.
4. Execute only approved deterministic assertions. Recheck both SHA facts
   before and after consequential steps.
5. If either SHA differs at any time, stop and map
   `ENVIRONMENT_PREEMPTED` to `status=blocked`; do not claim partial results
   as acceptance.
6. Clean up only the exact resource IDs recorded by this run, through their
   public API. Verify deletion without widening the selector.
7. Return only JSON conforming exactly to the complete authoritative
   `StageResult` schema and status policy injected by the host. A pass must
   reference authoritative SHA, assertion, and cleanup evidence.

If the host did not inject the complete schema, status mapping, and evidence
placeholders, do not guess JSON. Return a plain-text blocked handoff requesting
the missing host contract.

## Hard boundaries

- Do not deploy, push, reacquire the shared environment, or continue after
  preemption.
- Do not write the database, container filesystem, queue, or runtime state.
- Do not send real email, make payment, call production-priced models, or
  perform irreversible third-party actions.
- Do not delete by a broad prefix, guessed ID, shared account, or time range.
- Do not use narrative claims as evidence or invent `StageResult` fields.
- If cleanup is incomplete or the SHA binding is uncertain, return `blocked`
  or `failed`, never `passed`.
