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
3. Collect requirement, constraints, success criteria, design, and
   implementation plan. Before explicit approval, make no project writes.
4. Present the exact approval scope. Use the CLI approval command only after
   the user approves that scope; approval does not cover later scope growth.
5. Advance or resume only through `python -m change_delivery ...`. Never
   emulate a transition, skip a state, or treat prose as state evidence.
6. For an implementation stage, explicitly use `$delivery-implement`. For an
   internal review stage, explicitly use `$delivery-review`. For ExampleProject
   acceptance, explicitly use `$project-test-acceptance`.
7. Report blocks and evidence from the returned structured stage result.
8. At `READY_TO_MERGE`, stop and report the PR and evidence. Do not merge,
   release, or invent a later state, even when earlier authorization said to
   complete the whole delivery.

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
