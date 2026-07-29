from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import hmac
import hashlib
import json
from pathlib import Path
import re
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
    StageResult,
    StepOutcome,
)
from change_delivery.repository import RunRepository
from change_delivery.state_machine import READY_TO_MERGE_EVIDENCE


class GitPort(Protocol):
    def feature_sha(self, branch: str) -> str | None: ...
    def push_feature(self, branch: str, expected_sha: str) -> str: ...


class GitHubPort(Protocol):
    def find_pr(self, branch: str, base: str) -> object | None: ...
    def ensure_draft_pr(
        self, branch: str, base: str, title: str, body: str
    ) -> object: ...


class StageExecutor(Protocol):
    def execute(
        self,
        stage: str,
        run: RunRecord,
        config: ProjectConfig,
        run_root: Path,
    ) -> StageResult: ...


ConfigLoader = Callable[[str], tuple[ProjectConfig, str]]
ApprovalValidator = Callable[[RunRecord, ProjectConfig, Path], bool]


class DependencyUnavailable(RuntimeError):
    pass


class UnavailableGit:
    def __getattr__(self, name: str) -> object:
        raise DependencyUnavailable(f"Git dependency unavailable for {name}")


class UnavailableGitHub:
    def __getattr__(self, name: str) -> object:
        raise DependencyUnavailable(f"GitHub dependency unavailable for {name}")


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
        stage_executor: StageExecutor | None = None,
        test_environment: object | None = None,
        acceptance_executor: object | None = None,
        worktree_port: object | None = None,
        commit_port: object | None = None,
        cleanup_port: object | None = None,
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
        self.stage_executor = stage_executor
        self.test_environment = test_environment
        self.acceptance_executor = acceptance_executor
        self.worktree_port = worktree_port
        self.commit_port = commit_port
        self.cleanup_port = cleanup_port

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
            if run.state is RunState.DRAFT:
                return self._draft_artifacts(run, config)
            if run.state is RunState.AWAITING_DESIGN_APPROVAL:
                return StepOutcome(
                    state=run.state,
                    action="waiting_approval",
                    blocked_reason="explicit design approval is required",
                )
            if run.state is RunState.APPROVED:
                return self._pure_transition(
                    run,
                    RunState.PREPARING_WORKTREE,
                    "approval_scope_accepted",
                )
            if run.state is RunState.PREPARING_WORKTREE:
                return self._run_reconciled_port(
                    run,
                    config,
                    self.worktree_port,
                    "prepare_worktree",
                    RunState.IMPLEMENTING,
                    "worktree_prepared",
                )
            if run.state is RunState.IMPLEMENTING:
                return self._run_stage(
                    run, config, "implementation", RunState.LOCAL_VALIDATION
                )
            if run.state is RunState.LOCAL_VALIDATION:
                return self._run_stage(
                    run, config, "local_validation", RunState.INTERNAL_REVIEW
                )
            if run.state is RunState.INTERNAL_REVIEW:
                return self._run_stage(
                    run, config, "internal_review", RunState.COMMITTING
                )
            if run.state is RunState.COMMITTING:
                return self._commit(run, config)
            if run.state is RunState.DEPLOYING_TEST:
                return self._run_reconciled_port(
                    run,
                    config,
                    self.test_environment,
                    "deploy_test",
                    RunState.ACCEPTANCE_TESTING,
                    "test_deployed",
                )
            if run.state is RunState.ACCEPTANCE_TESTING:
                return self._run_reconciled_port(
                    run,
                    config,
                    self.acceptance_executor,
                    "acceptance",
                    RunState.PR_READY_FOR_REVIEW,
                    "acceptance_passed",
                )
            if run.state is RunState.PR_READY_FOR_REVIEW:
                return self._mark_pr_ready(run, config)
            if run.state is RunState.FIXING_CI_REVIEW:
                return self._run_stage(
                    run, config, "review_fix", RunState.LOCAL_VALIDATION
                )
            if run.state is RunState.READY_TO_MERGE:
                return self._ready_to_merge(run, config)
            if run.state is RunState.MERGED:
                return self._cleanup(run, config)
            if run.state is RunState.CLEANING_LOCAL:
                return self._pure_transition(
                    run,
                    RunState.COMPLETED,
                    "local_cleanup_recorded",
                )
            if run.state is RunState.ENVIRONMENT_PREEMPTED:
                return self._resume_environment(run)
            if run.state is RunState.COMPLETED:
                return StepOutcome(state=run.state, action="already_completed")
            if run.state is RunState.FAILED_TERMINAL:
                return StepOutcome(state=run.state, action="terminal_failure")
            if run.state is RunState.CANCELLED:
                return StepOutcome(state=run.state, action="already_cancelled")
            raise AssertionError(f"unmapped RunState: {run.state.value}")
        except DependencyUnavailable as error:
            try:
                state = self.repository.get_run(run_id).state
            except KeyError:
                state = RunState.BLOCKED
            return StepOutcome(
                state=state,
                action="blocked_dependency",
                blocked_reason=str(error),
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

    def _blocked_dependency(
        self,
        run: RunRecord,
        dependency: str,
    ) -> StepOutcome:
        return StepOutcome(
            state=run.state,
            action="blocked_dependency",
            blocked_reason=f"{dependency} is unavailable",
        )

    def _run_stage(
        self,
        run: RunRecord,
        config: ProjectConfig,
        stage: str,
        target: RunState,
    ) -> StepOutcome:
        if self.stage_executor is None:
            return self._blocked_dependency(run, f"{stage} stage executor")
        reviewed_tree_sha: str | None = None
        if stage == "internal_review":
            if self.commit_port is None:
                return self._blocked_dependency(run, "review tree fact adapter")
            reviewed_tree_sha = self.commit_port.review_tree(  # type: ignore[attr-defined]
                run, config
            )
            if (
                not isinstance(reviewed_tree_sha, str)
                or re.fullmatch(r"[0-9a-f]{40}", reviewed_tree_sha) is None
            ):
                return StepOutcome(
                    state=run.state,
                    action="internal_review_blocked",
                    blocked_reason="review tree SHA is invalid",
                )
        result = self.stage_executor.execute(
            stage,
            run,
            config,
            self._run_root(run.run_id),
        )
        references = self.repository.rebind_primary_stage_result(
            run.run_id,
            result,
            primary=True,
        )
        if result.status != "passed":
            if stage == "internal_review":
                target = (
                    RunState.IMPLEMENTING
                    if result.status == "failed"
                    else RunState.REVIEW_DECISION_REQUIRED
                )
                self.repository.transition_with_evidence(
                    run.run_id,
                    run.state,
                    target,
                    "INTERNAL_REVIEW_NOT_PASSED",
                    references,
                )
                return StepOutcome(
                    state=target,
                    action=(
                        "review_changes_requested"
                        if target is RunState.IMPLEMENTING
                        else "review_decision_required"
                    ),
                    blocked_reason=result.summary,
                    evidence_ids=tuple(
                        reference.evidence_id for reference in references
                    ),
                )
            return StepOutcome(
                state=run.state,
                action=f"{stage}_blocked",
                blocked_reason=result.summary,
            )
        if stage == "local_validation":
            if run.head_sha is None or any(
                not self._evidence_is_current(ref.evidence_id, run.head_sha)
                for ref in references
            ):
                return StepOutcome(
                    state=run.state,
                    action="local_validation_blocked",
                    blocked_reason="validation evidence is not bound to current head",
                )
        transition_references = references
        if stage == "internal_review":
            assert reviewed_tree_sha is not None
            attestation = self.repository.register_evidence(
                run.run_id,
                f"review-attestation:{run.run_id}:{reviewed_tree_sha}",
                "internal_review_attestation",
                {
                    "passed": True,
                    "reviewed_tree_sha": reviewed_tree_sha,
                    "expected_parent_sha": run.head_sha,
                    "source_evidence_ids": [
                        reference.evidence_id for reference in references
                    ],
                },
            )
            transition_references = (*references, attestation)
        self.repository.transition_with_evidence(
            run.run_id,
            run.state,
            target,
            f"{stage.upper()}_PASSED",
            transition_references,
        )
        return StepOutcome(
            state=target,
            action=f"{stage}_passed",
            evidence_ids=tuple(
                ref.evidence_id for ref in transition_references
            ),
        )

    def _evidence_is_current(self, evidence_id: str, head_sha: str) -> bool:
        payload = json.loads(
            self.repository.get_evidence(evidence_id).payload_json
        )
        return (
            type(payload) is dict
            and payload.get("sha") == head_sha
            and payload.get("passed") is True
        )

    def _run_reconciled_port(
        self,
        run: RunRecord,
        config: ProjectConfig,
        port: object | None,
        operation_type: str,
        target: RunState,
        evidence_kind: str,
    ) -> StepOutcome:
        if port is None:
            return self._blocked_dependency(run, f"{operation_type} adapter")
        if run.head_sha is None:
            return StepOutcome(
                state=run.state,
                action=f"{operation_type}_blocked",
                blocked_reason="run head SHA is missing",
            )
        key = f"{operation_type}:{run.run_id}:{run.head_sha}"
        try:
            operation = self.repository.get_remote_operation(key)
        except KeyError:
            operation = None
        observed = port.query(run, config)  # type: ignore[attr-defined]
        if observed is None and operation is not None:
            return StepOutcome(
                state=run.state,
                action=f"{operation_type}_reconciliation_pending",
                blocked_reason="pending operation is not authoritatively observable",
            )
        if observed is None:
            self.repository.begin_remote_operation(
                key,
                run.run_id,
                operation_type,
                f"{run.project_id}:{run.head_sha}",
            )
            try:
                observed = port.execute(run, config)  # type: ignore[attr-defined]
            except Exception as error:
                return StepOutcome(
                    state=run.state,
                    action=f"{operation_type}_reconciliation_pending",
                    blocked_reason=f"operation result is uncertain: {type(error).__name__}",
                )
        payload = observed
        if (
            type(payload) is not dict
            or payload.get("sha") != run.head_sha
            or payload.get("passed") is not True
        ):
            return StepOutcome(
                state=run.state,
                action=f"{operation_type}_blocked",
                blocked_reason="operation evidence is not bound to current head",
            )
        if operation is None:
            try:
                operation = self.repository.get_remote_operation(key)
            except KeyError:
                operation = None
        if operation is not None and operation.status == "PENDING":
            self.repository.confirm_remote_operation(key)
        reference = self.repository.register_evidence(
            run.run_id,
            f"{evidence_kind}:{run.run_id}:{run.head_sha}",
            evidence_kind,
            payload,
        )
        self.repository.transition_with_evidence(
            run.run_id,
            run.state,
            target,
            f"{operation_type.upper()}_PASSED",
            (reference,),
        )
        return StepOutcome(
            state=target,
            action=f"{operation_type}_reconciled",
            evidence_ids=(reference.evidence_id,),
        )

    def _pure_transition(
        self,
        run: RunRecord,
        target: RunState,
        action: str,
    ) -> StepOutcome:
        self.repository.transition_with_evidence(
            run.run_id,
            run.state,
            target,
            action.upper(),
        )
        return StepOutcome(state=target, action=action)

    def _draft_artifacts(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> StepOutcome:
        if self.stage_executor is None:
            return self._blocked_dependency(run, "design stage executor")
        root = self._run_root(run.run_id)
        result = self.stage_executor.execute("design", run, config, root)
        if result.status != "passed":
            return StepOutcome(
                state=run.state,
                action="design_blocked",
                blocked_reason=result.summary,
            )
        references = self.repository.rebind_primary_stage_result(
            run.run_id,
            result,
            primary=True,
        )
        artifacts = {}
        for name in REQUIRED_ARTIFACT_NAMES:
            path = root / name
            contents = path.read_bytes()
            if not contents:
                return StepOutcome(
                    state=run.state,
                    action="design_required",
                    blocked_reason=f"design artifact is empty: {name}",
                )
            artifacts[name] = hashlib.sha256(contents).hexdigest()
        self.repository.transition_with_evidence(
            run.run_id,
            RunState.DRAFT,
            RunState.AWAITING_DESIGN_APPROVAL,
            "DESIGN_ARTIFACTS_READY",
            references,
        )
        return StepOutcome(
            state=RunState.AWAITING_DESIGN_APPROVAL,
            action="design_artifacts_ready",
            evidence_ids=tuple(ref.evidence_id for ref in references),
        )

    def _resume_environment(self, run: RunRecord) -> StepOutcome:
        events = self.repository.list_events(run.run_id)
        entering = next(
            (
                event
                for event in reversed(events)
                if event.to_state is RunState.ENVIRONMENT_PREEMPTED
            ),
            None,
        )
        if entering is None or entering.from_state not in {
            RunState.DEPLOYING_TEST,
            RunState.ACCEPTANCE_TESTING,
        }:
            return StepOutcome(
                state=run.state,
                action="environment_reconciliation_blocked",
                blocked_reason="persisted environment interruption source is missing",
            )
        # Task11 supplies the shared-environment fact query before this resumes.
        return self._blocked_dependency(run, "test environment fact adapter")

    def _ready_to_merge(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> StepOutcome:
        if run.pr_number is None:
            return StepOutcome(
                state=run.state,
                action="merge_fact_blocked",
                blocked_reason="PR number is missing",
            )
        github = self.github_factory(config)
        try:
            merged = github.is_merged(run.pr_number)  # type: ignore[attr-defined]
        except DependencyUnavailable:
            raise
        except AttributeError:
            return self._blocked_dependency(run, "merge fact adapter")
        if merged is not True:
            return StepOutcome(
                state=run.state,
                action="waiting_manual_merge",
                blocked_reason="manual merge has not been observed",
            )
        return self._pure_transition(run, RunState.MERGED, "merge_observed")

    def _mark_pr_ready(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> StepOutcome:
        if run.pr_number is None or run.head_sha is None:
            return StepOutcome(
                state=run.state,
                action="pr_readiness_blocked",
                blocked_reason="PR identity is incomplete",
            )
        github = self.github_factory(config)
        key = f"mark_ready:{run.run_id}:{run.pr_number}:{run.head_sha}"
        try:
            operation = self.repository.get_remote_operation(key)
        except KeyError:
            operation = None
        try:
            current = github.get_pr(run.pr_number)  # type: ignore[attr-defined]
        except AttributeError:
            return self._blocked_dependency(run, "PR readiness adapter")
        if current.head_sha != run.head_sha:
            return StepOutcome(
                state=run.state,
                action="pr_readiness_blocked",
                blocked_reason="PR head SHA changed",
            )
        if current.is_draft:
            if operation is not None and operation.status == "PENDING":
                return StepOutcome(
                    state=run.state,
                    action="mark_ready_reconciliation_pending",
                    blocked_reason="pending ready operation is not observable",
                )
            self.repository.begin_remote_operation(
                key,
                run.run_id,
                "mark_ready",
                f"{config.github_repository}:{run.pr_number}:{run.head_sha}",
            )
            try:
                current = github.mark_ready(run.pr_number)  # type: ignore[attr-defined]
            except Exception as error:
                return StepOutcome(
                    state=run.state,
                    action="mark_ready_reconciliation_pending",
                    blocked_reason=f"ready result is uncertain: {type(error).__name__}",
                )
        if current.is_draft or current.head_sha != run.head_sha:
            return StepOutcome(
                state=run.state,
                action="mark_ready_reconciliation_pending",
                blocked_reason="ready PR fact is not authoritative",
            )
        try:
            operation = self.repository.get_remote_operation(key)
        except KeyError:
            operation = None
        if operation is not None and operation.status == "PENDING":
            self.repository.confirm_remote_operation(key)
        reference = self.repository.register_evidence(
            run.run_id,
            f"pr-ready:{run.run_id}:{run.head_sha}",
            "pr_ready",
            {"number": run.pr_number, "sha": run.head_sha, "passed": True},
        )
        self.repository.transition_with_evidence(
            run.run_id,
            RunState.PR_READY_FOR_REVIEW,
            RunState.MONITORING_CI_REVIEW,
            "PR_READY_RECONCILED",
            (reference,),
        )
        return StepOutcome(
            state=RunState.MONITORING_CI_REVIEW,
            action="pr_ready_reconciled",
            evidence_ids=(reference.evidence_id,),
        )

    def _commit(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> StepOutcome:
        port = self.commit_port
        if port is None:
            return self._blocked_dependency(run, "commit adapter")
        attestation, attestation_payload = self._review_attestation(run)
        reviewed_tree_sha = attestation_payload["reviewed_tree_sha"]
        expected_parent_sha = attestation_payload["expected_parent_sha"]
        if expected_parent_sha != run.head_sha:
            return StepOutcome(
                state=run.state,
                action="commit_review_blocked",
                blocked_reason="review attestation parent no longer matches run head",
            )
        key = (
            f"commit:{run.run_id}:{reviewed_tree_sha}:"
            f"{expected_parent_sha or 'unborn'}"
        )
        try:
            operation = self.repository.get_remote_operation(key)
        except KeyError:
            operation = None
        observed = port.query(run, config)  # type: ignore[attr-defined]
        if observed is None and operation is not None:
            return StepOutcome(
                state=run.state,
                action="commit_reconciliation_pending",
                blocked_reason="pending commit is not authoritatively observable",
            )
        if observed is None:
            self.repository.begin_remote_operation(
                key,
                run.run_id,
                "commit",
                (
                    f"{run.project_id}:{reviewed_tree_sha}:"
                    f"{expected_parent_sha or 'unborn'}"
                ),
            )
            try:
                observed = port.execute(run, config)  # type: ignore[attr-defined]
            except Exception as error:
                return StepOutcome(
                    state=run.state,
                    action="commit_reconciliation_pending",
                    blocked_reason=f"commit result is uncertain: {type(error).__name__}",
                )
        if type(observed) is not dict or observed.get("passed") is not True:
            return StepOutcome(
                state=run.state,
                action="commit_blocked",
                blocked_reason="commit fact is invalid",
            )
        new_sha = observed.get("new_head_sha")
        commit_tree_sha = observed.get("commit_tree_sha")
        parent_sha = observed.get("parent_sha")
        marker_run_id = observed.get("marker_run_id")
        if (
            not isinstance(new_sha, str)
            or re.fullmatch(r"[0-9a-f]{40}", new_sha) is None
            or not isinstance(commit_tree_sha, str)
            or re.fullmatch(r"[0-9a-f]{40}", commit_tree_sha) is None
            or parent_sha != expected_parent_sha
            or marker_run_id != run.run_id
        ):
            return StepOutcome(
                state=run.state,
                action="commit_blocked",
                blocked_reason="commit parent or run marker is invalid",
            )
        if commit_tree_sha != reviewed_tree_sha:
            return StepOutcome(
                state=run.state,
                action="commit_review_tree_mismatch",
                blocked_reason="commit tree differs from reviewed tree",
            )
        source_ids = tuple(attestation_payload["source_evidence_ids"])
        derived = self.repository.complete_commit_with_review(
            run.run_id,
            operation_key=key,
            expected_parent_sha=expected_parent_sha,
            new_head_sha=new_sha,
            commit_tree_sha=commit_tree_sha,
            source_evidence_ids=source_ids,
            attestation_id=attestation.evidence_id,
        )
        return StepOutcome(
            state=RunState.PUSHING_FEATURE,
            action="commit_reconciled",
            evidence_ids=tuple(reference.evidence_id for reference in derived),
        )

    def _review_attestation(
        self,
        run: RunRecord,
    ) -> tuple[object, dict[str, object]]:
        candidates = [
            evidence
            for evidence in self.repository.list_evidence(run.run_id)
            if evidence.kind == "internal_review_attestation"
        ]
        if not candidates:
            raise ValueError("internal review tree attestation is missing")
        attestation = candidates[-1]
        payload = json.loads(attestation.payload_json)
        if (
            type(payload) is not dict
            or payload.get("passed") is not True
            or not isinstance(payload.get("reviewed_tree_sha"), str)
            or re.fullmatch(
                r"[0-9a-f]{40}", payload["reviewed_tree_sha"]
            )
            is None
            or payload.get("expected_parent_sha") != run.head_sha
            or type(payload.get("source_evidence_ids")) is not list
            or not payload["source_evidence_ids"]
            or any(
                not isinstance(evidence_id, str)
                for evidence_id in payload["source_evidence_ids"]
            )
        ):
            raise ValueError("internal review tree attestation is invalid")
        for evidence_id in payload["source_evidence_ids"]:
            source = self.repository.get_evidence(evidence_id)
            if source.run_id != run.run_id:
                raise ValueError("review source evidence belongs to another run")
        return attestation, payload

    def _cleanup(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> StepOutcome:
        port = self.cleanup_port
        if port is None:
            return self._blocked_dependency(run, "worktree cleanup adapter")
        observed = port.query(run, config)  # type: ignore[attr-defined]
        if type(observed) is not dict or observed.get("clean") is not True:
            return StepOutcome(
                state=run.state,
                action="cleanup_blocked",
                blocked_reason="worktree is not clean",
            )
        if observed.get("removed") is not True:
            port.execute(run, config)  # type: ignore[attr-defined]
        return self._pure_transition(
            run,
            RunState.CLEANING_LOCAL,
            "worktree_cleanup_reconciled",
        )

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
        except DependencyUnavailable:
            raise
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
        except DependencyUnavailable:
            raise
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
        github = self.github_factory(config)
        try:
            if existing is not None:
                found = github.find_pr(branch, config.base_branch)
                if found is None:
                    return StepOutcome(
                        state=run.state,
                        action="pr_reconciliation_pending",
                        blocked_reason="pending PR is not authoritatively observable",
                    )
                found_sha = getattr(found, "head_sha", run.head_sha)
                if found_sha != run.head_sha:
                    return StepOutcome(
                        state=run.state,
                        action="pr_reconciliation_pending",
                        blocked_reason="pending PR head SHA mismatch",
                    )
                number = (
                    found if isinstance(found, int) else getattr(found, "number")
                )
            else:
                created = github.ensure_draft_pr(
                    branch,
                    config.base_branch,
                    f"chore: delivery {run.run_id}",
                    f"Automated delivery run: {run.run_id}",
                )
                created_sha = getattr(created, "head_sha", run.head_sha)
                if created_sha != run.head_sha:
                    return StepOutcome(
                        state=run.state,
                        action="pr_reconciliation_pending",
                        blocked_reason="created PR head SHA mismatch",
                    )
                number = (
                    created
                    if isinstance(created, int)
                    else getattr(created, "number")
                )
        except DependencyUnavailable:
            raise
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
        if (
            snapshot.checks_passed is not True
            or snapshot.threads_resolved is not True
            or snapshot.mergeable is not True
            or snapshot.review_decision is not True
            or snapshot.is_draft is not False
        ):
            return StepOutcome(
                state=run.state,
                action="monitoring",
                blocked_reason="remote PR gates are not all strictly satisfied",
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
        by_kind = {}
        for evidence in self.repository.list_evidence(run.run_id):
            payload = json.loads(evidence.payload_json)
            if (
                type(payload) is dict
                and payload.get("sha") == run.head_sha
                and payload.get("passed") is True
            ):
                by_kind[evidence.kind] = evidence
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
