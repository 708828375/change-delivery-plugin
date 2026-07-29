from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import hmac
import json
from pathlib import Path
from typing import Protocol

from change_delivery.approval import (
    ApprovalSnapshot,
    REQUIRED_ARTIFACT_NAMES,
    load_approval,
    verify_approval,
)
from change_delivery.domain import (
    ProjectConfig,
    RunRecord,
    RunState,
    StepOutcome,
)
from change_delivery.repository import RunRepository
from change_delivery.state_machine import READY_TO_MERGE_EVIDENCE


class GitPort(Protocol):
    def feature_sha(self, branch: str) -> str | None: ...
    def push_feature(self, branch: str, expected_sha: str) -> str: ...


class GitHubPort(Protocol):
    def ensure_draft_pr(self, branch: str, base: str, head_sha: str) -> int: ...


ConfigLoader = Callable[[str], tuple[ProjectConfig, str]]
ApprovalValidator = Callable[[RunRecord, ProjectConfig, Path], bool]


def _default_approval_validator(
    run: RunRecord,
    config: ProjectConfig,
    run_root: Path,
) -> bool:
    approval_path = run_root / "approval.json"
    try:
        approval = load_approval(approval_path)
    except (OSError, ValueError):
        return False
    if (
        approval.run_id != run.run_id
        or approval.project_id != run.project_id
        or approval.base_branch != config.base_branch
        or approval.test_branch != config.test_branch
    ):
        return False
    files = tuple(run_root / name for name in REQUIRED_ARTIFACT_NAMES)
    return verify_approval(approval, files, run.config_hash)


class Orchestrator:
    """A single-step, crash-recoverable delivery state driver."""

    def __init__(
        self,
        *,
        repository: RunRepository,
        plugin_root: Path,
        config_loader: ConfigLoader,
        git_factory: Callable[[ProjectConfig], GitPort],
        github_factory: Callable[[ProjectConfig], GitHubPort],
        clock: Callable[[], datetime] | None = None,
        approval_validator: ApprovalValidator | None = None,
    ) -> None:
        self.repository = repository
        self.plugin_root = plugin_root.resolve()
        self.config_loader = config_loader
        self.git_factory = git_factory
        self.github_factory = github_factory
        self.clock = clock or (lambda: datetime.now(UTC))
        self.approval_validator = (
            approval_validator or _default_approval_validator
        )

    def _run_root(self, run_id: str) -> Path:
        candidate = self.plugin_root / "runs" / run_id
        resolved_parent = candidate.parent.resolve(strict=True)
        expected = (self.plugin_root / "runs").resolve(strict=True)
        if resolved_parent != expected or candidate.is_symlink():
            raise ValueError("run path escapes plugin root")
        return candidate.resolve(strict=True)

    def _context(self, run_id: str) -> tuple[RunRecord, ProjectConfig, Path]:
        run = self.repository.get_run(run_id)
        config, digest = self.config_loader(run.project_id)
        if not hmac.compare_digest(digest, run.config_hash):
            raise ValueError("project configuration hash changed")
        root = self._run_root(run_id)
        if run.state not in {
            RunState.DRAFT,
            RunState.AWAITING_DESIGN_APPROVAL,
            RunState.BLOCKED,
            RunState.REVIEW_DECISION_REQUIRED,
            RunState.CANCELLED,
        } and not self.approval_validator(run, config, root):
            raise ValueError("approval snapshot is missing or changed")
        if run.state in {
            RunState.BLOCKED,
            RunState.REVIEW_DECISION_REQUIRED,
        }:
            events = self.repository.list_events(run.run_id)
            entering = next(
                (
                    event
                    for event in reversed(events)
                    if event.to_state is run.state
                ),
                None,
            )
            if (
                entering is not None
                and entering.from_state
                not in {RunState.DRAFT, RunState.AWAITING_DESIGN_APPROVAL}
                and not self.approval_validator(run, config, root)
            ):
                raise ValueError("approval snapshot is missing or changed")
        return run, config, root

    def step(self, run_id: str) -> StepOutcome:
        try:
            run, config, _ = self._context(run_id)
            if run.state in {
                RunState.BLOCKED,
                RunState.REVIEW_DECISION_REQUIRED,
            }:
                return self._resume_interrupted(run)
            if run.state is RunState.PUSHING_FEATURE:
                return self._push_feature(run, config)
            if run.state is RunState.PR_DRAFT:
                return self._draft_pr(run, config)
            if run.state is RunState.MONITORING_CI_REVIEW:
                return self._monitor(run, config)
            return StepOutcome(
                state=run.state,
                action="noop_blocked",
                blocked_reason=f"handler not implemented for {run.state.value}",
            )
        except Exception as error:
            try:
                state = self.repository.get_run(run_id).state
            except KeyError:
                state = RunState.BLOCKED
            return StepOutcome(
                state=state,
                action="blocked",
                blocked_reason=str(error),
            )

    resume = step

    def _resume_interrupted(self, run: RunRecord) -> StepOutcome:
        events = self.repository.list_events(run.run_id)
        entering = next(
            (
                event
                for event in reversed(events)
                if event.to_state is run.state
            ),
            None,
        )
        if entering is None:
            return StepOutcome(
                state=run.state,
                action="blocked",
                blocked_reason="persisted interruption source is missing",
            )
        self.repository.transition_with_evidence(
            run.run_id,
            run.state,
            entering.from_state,
            "RUN_RESUMED",
            interrupted_from=entering.from_state,
        )
        return StepOutcome(
            state=entering.from_state,
            action="resumed_interrupted_state",
        )

    @staticmethod
    def _feature_branch(run: RunRecord) -> str:
        return f"feature/{run.run_id}"

    def _push_feature(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> StepOutcome:
        if run.head_sha is None:
            return StepOutcome(
                state=run.state,
                action="blocked",
                blocked_reason="run head SHA is missing",
            )
        branch = self._feature_branch(run)
        identity = f"{branch}:{run.head_sha}"
        key = f"push_feature:{run.run_id}:{identity}"
        git = self.git_factory(config)
        try:
            remote_sha = git.feature_sha(branch)
        except Exception as error:
            return StepOutcome(
                state=run.state,
                action="push_reconciliation_pending",
                blocked_reason=f"remote feature query failed: {type(error).__name__}",
            )
        try:
            operation = self.repository.get_remote_operation(key)
        except KeyError:
            operation = None

        if remote_sha == run.head_sha:
            if operation is not None and operation.status == "PENDING":
                self.repository.confirm_remote_operation(key)
            return self._finish_push(run, branch, "feature_already_pushed")
        if operation is not None and operation.status == "PENDING":
            return StepOutcome(
                state=run.state,
                action="push_reconciliation_pending",
                blocked_reason="pending push has no authoritative remote result",
            )
        if operation is not None:
            return StepOutcome(
                state=run.state,
                action="blocked",
                blocked_reason="prior push operation is terminal without matching SHA",
            )

        self.repository.begin_remote_operation(
            key,
            run.run_id,
            "push_feature",
            identity,
        )
        try:
            pushed = git.push_feature(branch, run.head_sha)
        except Exception as error:
            return StepOutcome(
                state=run.state,
                action="push_reconciliation_pending",
                blocked_reason=f"push result is uncertain: {type(error).__name__}",
            )
        if pushed != run.head_sha:
            return StepOutcome(
                state=run.state,
                action="push_reconciliation_pending",
                blocked_reason="push result SHA mismatch",
            )
        self.repository.confirm_remote_operation(key)
        return self._finish_push(run, branch, "feature_pushed")

    def _finish_push(
        self,
        run: RunRecord,
        branch: str,
        action: str,
    ) -> StepOutcome:
        payload = {"branch": branch, "sha": run.head_sha}
        reference = self.repository.register_evidence(
            run.run_id,
            f"feature-head:{run.run_id}:{run.head_sha}",
            "head_sha_verified",
            payload,
        )
        self.repository.transition_with_evidence(
            run.run_id,
            RunState.PUSHING_FEATURE,
            RunState.PR_DRAFT,
            "FEATURE_PUSHED",
            (reference,),
        )
        return StepOutcome(
            state=RunState.PR_DRAFT,
            action=action,
            evidence_ids=(reference.evidence_id,),
        )

    def _draft_pr(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> StepOutcome:
        if run.head_sha is None:
            return StepOutcome(
                state=run.state,
                action="blocked",
                blocked_reason="run head SHA is missing",
            )
        branch = self._feature_branch(run)
        identity = f"{config.github_repository}:{branch}:{run.head_sha}"
        key = f"draft_pr:{run.run_id}:{identity}"
        try:
            existing = self.repository.get_remote_operation(key)
        except KeyError:
            self.repository.begin_remote_operation(
                key, run.run_id, "draft_pr", identity
            )
            existing = None
        if existing is not None and existing.status != "PENDING":
            return StepOutcome(
                state=run.state,
                action="blocked",
                blocked_reason="draft PR operation is terminal without state transition",
            )
        try:
            number = self.github_factory(config).ensure_draft_pr(
                branch,
                config.base_branch,
                run.head_sha,
            )
        except Exception as error:
            return StepOutcome(
                state=run.state,
                action="pr_reconciliation_pending",
                blocked_reason=f"PR result is uncertain: {type(error).__name__}",
            )
        self.repository.confirm_remote_operation(key)
        reference = self.repository.register_evidence(
            run.run_id,
            f"draft-pr:{run.run_id}:{run.head_sha}",
            "draft_pr",
            {"number": number, "head_sha": run.head_sha},
        )
        self.repository.transition_with_evidence(
            run.run_id,
            RunState.PR_DRAFT,
            RunState.DEPLOYING_TEST,
            "DRAFT_PR_RECONCILED",
            (reference,),
            run_updates={"pr_number": number},
        )
        return StepOutcome(
            state=RunState.DEPLOYING_TEST,
            action="draft_pr_reconciled",
            evidence_ids=(reference.evidence_id,),
        )

    def _monitor(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> StepOutcome:
        if run.head_sha is None or run.pr_number is None:
            return StepOutcome(
                state=run.state,
                action="blocked",
                blocked_reason="PR identity is incomplete",
            )
        github = self.github_factory(config)
        snapshot = github.snapshot(run.pr_number)  # type: ignore[attr-defined]
        if snapshot.head_sha != run.head_sha:
            return StepOutcome(
                state=run.state,
                action="blocked",
                blocked_reason="PR head SHA changed",
            )
        facts = {
            "ci_passed": snapshot.checks_passed,
            "threads_resolved": snapshot.threads_resolved,
            "mergeable": snapshot.mergeable,
            "head_sha_verified": True,
        }
        refs = []
        for kind, passed in facts.items():
            if passed:
                refs.append(
                    self.repository.register_evidence(
                        run.run_id,
                        f"{kind}:{run.run_id}:{run.head_sha}",
                        kind,
                        {"sha": run.head_sha, "passed": True},
                    )
                )
        by_kind = {
            evidence.kind: evidence
            for evidence in self.repository.list_evidence(run.run_id)
            if json.loads(evidence.payload_json).get("sha") == run.head_sha
        }
        if not READY_TO_MERGE_EVIDENCE <= by_kind.keys():
            return StepOutcome(
                state=run.state,
                action="monitoring",
                blocked_reason="current head SHA does not satisfy all merge gates",
                evidence_ids=tuple(reference.evidence_id for reference in refs),
            )
        gate_refs = tuple(
            by_kind[kind].reference for kind in sorted(READY_TO_MERGE_EVIDENCE)
        )
        self.repository.transition_with_evidence(
            run.run_id,
            RunState.MONITORING_CI_REVIEW,
            RunState.READY_TO_MERGE,
            "MERGE_GATES_PASSED",
            gate_refs,
        )
        return StepOutcome(
            state=RunState.READY_TO_MERGE,
            action="ready_to_merge",
            evidence_ids=tuple(ref.evidence_id for ref in gate_refs),
        )

    def cancel(self, run_id: str) -> StepOutcome:
        run = self.repository.get_run(run_id)
        if run.state is RunState.CANCELLED:
            return StepOutcome(state=RunState.CANCELLED, action="already_cancelled")
        if run.state in {
            RunState.COMPLETED,
            RunState.FAILED_TERMINAL,
        }:
            raise ValueError("terminal run cannot be cancelled")
        self.repository.transition_with_evidence(
            run_id,
            run.state,
            RunState.CANCELLED,
            "RUN_CANCELLED",
        )
        return StepOutcome(state=RunState.CANCELLED, action="cancelled")
