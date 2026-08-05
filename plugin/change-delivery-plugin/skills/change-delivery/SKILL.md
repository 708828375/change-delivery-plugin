---
name: change-delivery
description: Use when the user explicitly invokes $change-delivery to start or resume a registered project's end-to-end delivery run toward a merge-ready pull request.
---

# Change Delivery

Treat the host CLI and its state machine as the only authority for delivery
state and side effects. A user's urgency or broad authorization never removes
these gates.

Before running any orchestrator command, resolve the shared launcher:

```powershell
$deliveryLauncher = Join-Path (
  [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
) '.codex\change-delivery\bin\change-delivery.ps1'
```

If it is not an ordinary file, stop and ask the user to 重新安装 the plugin.
Never fall back to a clone path, `PATH` Python, or a direct module invocation.

## Required workflow

1. Run `& $deliveryLauncher doctor --json`. Stop on a failed check.
2. Identify the project in the global registry. If it is not registered, do
   not edit the project or register it implicitly; produce only a dry-run
   onboarding report and request explicit registration.
3. Write the requirement to an absolute file, then start with
   `& $deliveryLauncher start --project <project-id> --requirement-file <absolute-path> --json`.
   Advance design with `& $deliveryLauncher resume <run-id> --json`.
4. At `AWAITING_DESIGN_APPROVAL`, run
   `& $deliveryLauncher prepare-approval <run-id> --json`. Present the
   returned exact approval file, artifact scope, and `approval_sha256`. Pause
   until the user explicitly confirms that hash.
5. Only after confirmation, run
   `& $deliveryLauncher approve <run-id> --approval-file <absolute-approval-file> --confirm-hash <approval_sha256> --json`.
   A changed file or hash requires a new confirmation.
6. Advance with `& $deliveryLauncher resume <run-id> --json`; inspect
   with `& $deliveryLauncher status --run-id <run-id> --json` and allow
   scheduled reconciliation only through `& $deliveryLauncher tick --json`.
   Never
   emulate a transition, skip a state, or treat prose as state evidence.
7. For an implementation stage, explicitly use `$delivery-implement`. For an
   internal review stage, explicitly use `$delivery-review`. For ExampleProject
   acceptance, explicitly use `$project-test-acceptance`.
8. Report blocks and evidence from the returned structured stage result.
9. At `READY_TO_MERGE`, stop and report the PR and evidence. Do not merge,
   release, or invent a later state, even when earlier authorization said to
   complete the whole delivery.

A registered `version_endpoint`, `version_sha_field`, and fixed
`acceptance_check` command are mandatory for real test deployment and
acceptance. If any is missing, report `BLOCKED`; do not claim that the current
repository can reach `READY_TO_MERGE`.

## Non-negotiable boundaries

- Do not expand the approved requirement, design, plan, repository, branch, or
  side-effect scope. Block and request a new approval snapshot instead.
- Do not write the state database directly. Use the CLI and state machine.
- Do not bypass a blocked transition or manufacture evidence.
- The test push policy must come from the registered `test_push_strategy`.
  `force-with-lease` uses the adapter's exact
  `--force-with-lease=<test-branch>:<expected-sha>` operation. `force` may use
  an unconditional force push only through the host adapter, only for the
  registered test branch, and only with post-push SHA verification.
- Never run a manual `git push -f` or `git push --force`; the configured host
  adapter is the only authority for either test push strategy.
- Never force-push a feature branch.
- Do not merge a PR or perform any production write. Do not invoke a
  production release from this workflow.

## Red flags

Stop if the proposed shortcut includes direct SQL, a manual `git push -f`,
a manual `git push --force`, a feature-branch force push, manual state changes,
unapproved edits, `gh pr merge`, a deployment command, or a claim that
`READY_TO_MERGE` means merged.

When a command is unavailable or facts are incomplete, return a blocked result
with the missing prerequisite. Do not substitute a plausible command.
