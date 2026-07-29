# ExampleProject Acceptance Checklist

Use this checklist for the current run only.

## Preconditions

- Confirm the project registration, approved scenarios, run ID, and exact run
  prefix `codex-<run-id>`.
- Record the expected 40-character Git SHA.
- Read the remote test branch through the registered Git adapter.
- Read the registered version endpoint and configured SHA field.
- Require both facts to equal the expected SHA before any temporary write.
- Confirm every planned write uses a public API and is inside approved scope.

## Temporary data ledger

- Attach the exact run prefix to each created object.
- After every successful create response, record the resource type, exact
  resource ID, exact returned marker, parent ID, and creation result.
- Never infer created resource IDs from names, list position, timestamps, or a
  database query.
- Permit cleanup only when the returned marker equals the exact run prefix or
  uses its unambiguous `--` child suffix.

## Assertions and continuity

- Recheck the remote test branch and version endpoint before and after each
  consequential scenario.
- Stop with `ENVIRONMENT_PREEMPTED` when either no longer equals the expected
  SHA. The other source still matching does not preserve acceptance.
- Record deterministic API or WebSocket assertions and bounded polling results.
- Treat logs and database results as read-only corroboration, not as a route
  for setup or mutation.
- Prohibit real email, real payment, costly production-model calls, and
  irreversible third-party actions.

## Cleanup

- Delete children before parents using public API operations.
- Address only created resource IDs from this run's ledger.
- Before each delete, verify the fetched resource still carries the exact run
  prefix; stop on an ownership mismatch.
- Record the delete result for each ID and verify each exact ID is absent.
- Never use `startsWith`, wildcard, time-window, account-wide, or global cleanup.
- Prohibit direct database writes, SSH deletion, container mutation, and queue
  purges.
- Report incomplete cleanup as blocked or failed; preserve the remaining exact
  IDs for authorized follow-up.

## Evidence

- Include the expected SHA and start/end facts for both SHA sources.
- Include assertion evidence and the temporary-data ledger.
- Include cleanup requests and exact-ID absence checks.
- Redact credentials, tokens, cookies, message bodies, and personal data.
