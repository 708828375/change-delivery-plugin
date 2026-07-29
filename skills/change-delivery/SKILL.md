---
name: change-delivery
description: Use when the user explicitly invokes $change-delivery to start or resume a registered project's end-to-end delivery run toward a merge-ready pull request.
---

# Change Delivery

Treat the host CLI and its state machine as the only authority for delivery
state and side effects. A user's urgency or broad authorization never removes
these gates.

## Required workflow

1. Run `python -m change_delivery doctor --json`. Stop on a failed check.
2. Identify the project in the global registry. If it is not registered, do
   not edit the project or register it implicitly; produce only a dry-run
   onboarding report and request explicit registration.
3. Write the requirement to an absolute file, then start with
   `python -m change_delivery start --project <project-id> --requirement-file <absolute-path> --json`.
   Advance design with `python -m change_delivery resume <run-id> --json`.
4. At `AWAITING_DESIGN_APPROVAL`, run
   `python -m change_delivery prepare-approval <run-id> --json`. Present the
   returned exact approval file, artifact scope, and `approval_sha256`. Pause
   until the user explicitly confirms that hash.
5. Only after confirmation, run
   `python -m change_delivery approve <run-id> --approval-file <absolute-approval-file> --confirm-hash <approval_sha256> --json`.
   A changed file or hash requires a new confirmation.
6. Advance with `python -m change_delivery resume <run-id> --json`; inspect
   with `python -m change_delivery status --run-id <run-id> --json` and allow
   scheduled reconciliation only through `python -m change_delivery tick --json`.
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
- Do not run an unconditional force push. The shared test branch may be
  updated only by the adapter's exact
  `--force-with-lease=<test-branch>:<expected-sha>` operation.
- Never force-push a feature branch.
- Do not merge a PR or perform any production write. Do not invoke a
  production release from this workflow.

## Red flags

Stop if the proposed shortcut includes direct SQL, `git push -f`,
`git push --force`, a feature-branch force push, manual state changes,
unapproved edits, `gh pr merge`, a deployment command, or a claim that
`READY_TO_MERGE` means merged.

When a command is unavailable or facts are incomplete, return a blocked result
with the missing prerequisite. Do not substitute a plausible command.
