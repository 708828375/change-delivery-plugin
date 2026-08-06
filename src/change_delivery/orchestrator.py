from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import hmac
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Protocol
from change_delivery.artifacts import ArtifactStore

from change_delivery.approval import (
    ApprovalSnapshot,
    REQUIRED_ARTIFACT_NAMES,
    load_approval,
    verify_approval,
)
from change_delivery.adapters.github import (
    PullRequestRef,
    RequestedReviewFact,
    ReviewThread,
)
from change_delivery.domain import (
    ProjectConfig,
    RunRecord,
    RunState,
    StageResult,
    StepOutcome,
)
from change_delivery.evidence import sha256_payload
from change_delivery.repository import RunRepository
from change_delivery.process import ProcessTimeoutError
from change_delivery.state_machine import READY_TO_MERGE_EVIDENCE


_SHA = re.compile(r"^[0-9a-f]{40}$")


class GitPort(Protocol):
    def feature_sha(self, branch: str) -> str | None: ...
    def push_feature(self, branch: str, expected_sha: str) -> str: ...


class GitHubPort(Protocol):
    def find_pr(self, branch: str, base: str) -> object | None: ...
    def ensure_draft_pr(
        self, branch: str, base: str, title: str, body: str
    ) -> object: ...
    def list_actionable_reviews(
        self, pr_number: int
    ) -> tuple[ReviewThread, ...]: ...
    def reply_to_thread(
        self, pr_number: int, thread_id: str, body: str
    ) -> None: ...
    def thread_contains_comment(
        self, pr_number: int, thread_id: str, body: str
    ) -> bool: ...


class StageExecutor(Protocol):
    def execute(
        self,
        stage: str,
        run: RunRecord,
        config: ProjectConfig,
        run_root: Path,
        *,
        operation_identity: str | None = None,
        tested_tree_sha: str | None = None,
        checks_identity: str | None = None,
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
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self.repository = repository
        self.plugin_root = plugin_root.resolve()
        self.config_loader = config_loader
        self.git_factory = git_factory
        self.github_factory = github_factory
        self.clock = clock or (lambda: datetime.now(UTC))
        self.approval_validator = approval_validator or self._validate_approval
        self.stage_executor = stage_executor
        self.test_environment = test_environment
        self.acceptance_executor = acceptance_executor
        self.worktree_port = worktree_port
        self.commit_port = commit_port
        self.cleanup_port = cleanup_port
        self.artifact_store = artifact_store or ArtifactStore(self.plugin_root)

    def _run_root(self, run_id: str) -> Path:
        candidate = self.plugin_root / "runs" / run_id
        expected = self.plugin_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(expected)
        except ValueError as error:
            raise ValueError("run path escapes plugin root") from error
        current = self.plugin_root / "runs"
        for component in (current, candidate):
            metadata = component.lstat()
            if component.is_symlink() or (
                getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                raise ValueError("run path contains a link or reparse point")
        if resolved.parent != current.resolve(strict=True):
            raise ValueError("run path escapes plugin root")
        return resolved

    def _validate_approval(
        self,
        run: RunRecord,
        config: ProjectConfig,
        run_root: Path,
    ) -> bool:
        try:
            approval_path = run_root / "approval.json"
            metadata = approval_path.lstat()
            if approval_path.is_symlink() or (
                getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                return False
            descriptor = os.open(
                approval_path,
                os.O_RDONLY | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            with os.fdopen(descriptor, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino) != (
                    metadata.st_dev,
                    metadata.st_ino,
                ):
                    return False
                contents = handle.read()
                after = os.fstat(handle.fileno())
            if (
                opened.st_size,
                opened.st_mtime_ns,
            ) != (after.st_size, after.st_mtime_ns):
                return False
            payload = json.loads(contents)
            approval = ApprovalSnapshot.model_validate(payload)
            stored_json, evidence = self.repository.get_approval_binding(
                run.run_id
            )
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            file_sha = hashlib.sha256(contents).hexdigest()
            if (
                canonical != stored_json
                or evidence.payload_json != stored_json
                or not hmac.compare_digest(evidence.sha256, file_sha)
            ):
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
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return False

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
            if run.state is RunState.FIXING_INTERNAL_REVIEW:
                return self._run_stage(
                    run,
                    config,
                    "internal_review_fix",
                    RunState.LOCAL_VALIDATION,
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
        except ProcessTimeoutError as error:
            try:
                timed_out_run = self.repository.get_run(run_id)
                state = timed_out_run.state
            except KeyError:
                state = RunState.BLOCKED
            return StepOutcome(
                state=state,
                action="process_timeout",
                blocked_reason="configured process timeout exceeded",
                configured_timeout_seconds=error.timeout_seconds,
                operation_hash=hashlib.sha256(
                    f"{run_id}:{state.value}".encode("utf-8")
                ).hexdigest(),
                command_hash=hashlib.sha256(
                    error.command_id.encode("ascii")
                ).hexdigest(),
            )
        except DependencyUnavailable as error:
            try:
                state = self.repository.get_run(run_id).state
            except KeyError:
                state = RunState.BLOCKED
            return StepOutcome(
                state=state,
                action="blocked_dependency",
                blocked_reason=f"{type(error).__name__}: dependency unavailable",
            )
        except Exception as error:
            try:
                state = self.repository.get_run(run_id).state
            except KeyError:
                state = RunState.BLOCKED
            return StepOutcome(
                state=state,
                action="blocked",
                blocked_reason=f"{type(error).__name__}: operation blocked",
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
        tested_tree_sha: str | None = None
        validation_attestation_id: str | None = None
        validation_attestation: object | None = None
        validation_operation_identity: str | None = None
        checks_identity: str | None = None
        if stage == "local_validation":
            if self.commit_port is None:
                return self._blocked_dependency(run, "validation tree fact adapter")
            tested_tree_sha = self.commit_port.validation_tree(  # type: ignore[attr-defined]
                run, config
            )
            if (
                not isinstance(tested_tree_sha, str)
                or re.fullmatch(r"[0-9a-f]{40}", tested_tree_sha) is None
            ):
                return StepOutcome(
                    state=run.state,
                    action="local_validation_blocked",
                    blocked_reason="validation tree SHA is invalid",
                )
            validation_operation_identity = (
                f"local-validation:{run.run_id}:{secrets.token_hex(16)}"
            )
            checks_identity = self._checks_identity(config)
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
            validation_attestation = self._local_validation_attestation(run)
            validation_payload = json.loads(
                validation_attestation.payload_json
            )
            if validation_payload["tested_tree_sha"] != reviewed_tree_sha:
                return StepOutcome(
                    state=run.state,
                    action="internal_review_blocked",
                    blocked_reason="worktree changed after local validation",
                )
            validation_attestation_id = validation_attestation.evidence_id
        if stage == "local_validation":
            result = self.stage_executor.execute(
                stage,
                run,
                config,
                self._run_root(run.run_id),
                operation_identity=validation_operation_identity,
                tested_tree_sha=tested_tree_sha,
                checks_identity=checks_identity,
            )
        else:
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
        if stage == "local_validation":
            after_tree = self.commit_port.validation_tree(  # type: ignore[attr-defined]
                run, config
            )
            if after_tree != tested_tree_sha:
                return StepOutcome(
                    state=run.state,
                    action="local_validation_blocked",
                    blocked_reason="worktree changed during local validation",
                )
            if result.status == "passed" and not self._validation_sources_match(
                run,
                references,
                tested_tree_sha,
                validation_operation_identity,
                checks_identity,
            ):
                return StepOutcome(
                    state=run.state,
                    action="local_validation_blocked",
                    blocked_reason=(
                        "local validation source evidence does not match "
                        "this execution"
                    ),
                )
        if result.status != "passed":
            if stage == "local_validation":
                self.repository.transition_with_evidence(
                    run.run_id,
                    run.state,
                    RunState.IMPLEMENTING,
                    "LOCAL_VALIDATION_NOT_PASSED",
                    references,
                )
                return StepOutcome(
                    state=RunState.IMPLEMENTING,
                    action="validation_changes_required",
                    blocked_reason="local validation did not pass",
                    evidence_ids=tuple(
                        reference.evidence_id for reference in references
                    ),
                )
            if stage == "internal_review":
                if result.status == "failed":
                    return self._request_internal_review_fix(
                        run,
                        config,
                        result,
                        references,
                        reviewed_tree_sha,
                    )
                target = RunState.REVIEW_DECISION_REQUIRED
                self.repository.transition_with_evidence(
                    run.run_id,
                    run.state,
                    target,
                    "INTERNAL_REVIEW_NOT_PASSED",
                    references,
                )
                return StepOutcome(
                    state=target,
                    action="review_decision_required",
                    blocked_reason="internal review did not pass",
                    evidence_ids=tuple(
                        reference.evidence_id for reference in references
                    ),
                )
            return StepOutcome(
                state=run.state,
                action=f"{stage}_blocked",
                blocked_reason=f"{stage} did not pass",
            )
        transition_references = references
        if stage == "local_validation":
            assert tested_tree_sha is not None
            assert validation_operation_identity is not None
            assert checks_identity is not None
            nonce = validation_operation_identity.rsplit(":", 1)[-1]
            validation_attestation = self.repository.register_evidence(
                run.run_id,
                f"validation-attestation:{run.run_id}:{tested_tree_sha}:{nonce}",
                "local_validation_attestation",
                {
                    "passed": True,
                    "tested_tree_sha": tested_tree_sha,
                    "expected_parent_sha": run.head_sha,
                    "operation_identity": validation_operation_identity,
                    "checks_identity": checks_identity,
                    "source_evidence_ids": [
                        reference.evidence_id for reference in references
                    ],
                },
            )
            transition_references = (*references, validation_attestation)
        if stage == "internal_review":
            assert reviewed_tree_sha is not None
            assert validation_attestation is not None
            attestation = self.repository.register_evidence(
                run.run_id,
                f"review-attestation:{run.run_id}:{reviewed_tree_sha}",
                "internal_review_attestation",
                {
                    "passed": True,
                    "reviewed_tree_sha": reviewed_tree_sha,
                    "expected_parent_sha": run.head_sha,
                    "validation_attestation_id": validation_attestation_id,
                    "source_evidence_ids": [
                        reference.evidence_id for reference in references
                    ],
                },
            )
            transition_references = (
                *references,
                validation_attestation.reference,  # type: ignore[attr-defined]
                attestation,
            )
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

    def _request_internal_review_fix(
        self,
        run: RunRecord,
        config: ProjectConfig,
        result: StageResult,
        references: tuple[object, ...],
        reviewed_tree_sha: str | None,
    ) -> StepOutcome:
        if reviewed_tree_sha is None:
            raise ValueError("internal review tree identity is missing")
        actionable = tuple(
            finding for finding in result.findings if finding.actionable
        )
        if not actionable:
            self.repository.transition_with_evidence(
                run.run_id,
                RunState.INTERNAL_REVIEW,
                RunState.REVIEW_DECISION_REQUIRED,
                "INTERNAL_REVIEW_ACTION_REQUIRED",
                references,
            )
            return StepOutcome(
                state=RunState.REVIEW_DECISION_REQUIRED,
                action="review_decision_required",
                blocked_reason="internal review failed without actionable findings",
                evidence_ids=tuple(
                    reference.evidence_id for reference in references
                ),
            )
        attempts: list[int] = []
        for evidence in self.repository.list_evidence(run.run_id):
            if evidence.kind != "internal_review_fix_request":
                continue
            payload = json.loads(evidence.payload_json)
            if not hmac.compare_digest(
                evidence.sha256,
                sha256_payload(payload),
            ):
                raise ValueError("internal review fix request digest mismatch")
            if (
                type(payload) is not dict
                or set(payload) != {
                    "attempt",
                    "expected_parent_sha",
                    "findings",
                    "reviewed_tree_sha",
                }
                or type(payload["attempt"]) is not int
                or payload["attempt"] < 1
                or payload["expected_parent_sha"] != run.head_sha
                or not isinstance(payload["reviewed_tree_sha"], str)
                or type(payload["findings"]) is not list
                or not payload["findings"]
            ):
                raise ValueError("internal review fix request is invalid")
            attempts.append(payload["attempt"])
        previous_attempt = max(attempts, default=0)
        attempt = previous_attempt + 1
        if attempt > config.max_internal_review_fix_attempts:
            limit = self.repository.register_evidence(
                run.run_id,
                f"internal-review-fix-limit:{run.run_id}:{run.head_sha}",
                "internal_review_fix_limit_reached",
                {
                    "attempts": previous_attempt,
                    "expected_parent_sha": run.head_sha,
                    "reviewed_tree_sha": reviewed_tree_sha,
                },
            )
            transition_evidence = (*references, limit)
            self.repository.transition_with_evidence(
                run.run_id,
                RunState.INTERNAL_REVIEW,
                RunState.REVIEW_DECISION_REQUIRED,
                "INTERNAL_REVIEW_FIX_LIMIT_REACHED",
                transition_evidence,
            )
            return StepOutcome(
                state=RunState.REVIEW_DECISION_REQUIRED,
                action="internal_review_fix_limit_reached",
                blocked_reason=(
                    "internal review remained unresolved after the configured "
                    "automatic fix limit"
                ),
                evidence_ids=tuple(
                    reference.evidence_id
                    for reference in transition_evidence
                ),
            )
        request = self.repository.register_evidence(
            run.run_id,
            f"internal-review-fix-request:{run.run_id}:{attempt}",
            "internal_review_fix_request",
            {
                "attempt": attempt,
                "expected_parent_sha": run.head_sha,
                "findings": [
                    finding.model_dump(mode="json")
                    for finding in actionable
                ],
                "reviewed_tree_sha": reviewed_tree_sha,
            },
        )
        transition_evidence = (*references, request)
        self.repository.transition_with_evidence(
            run.run_id,
            RunState.INTERNAL_REVIEW,
            RunState.FIXING_INTERNAL_REVIEW,
            "INTERNAL_REVIEW_CHANGES_REQUIRED",
            transition_evidence,
        )
        return StepOutcome(
            state=RunState.FIXING_INTERNAL_REVIEW,
            action="internal_review_changes_required",
            blocked_reason="internal review requires bounded changes",
            evidence_ids=tuple(
                reference.evidence_id for reference in transition_evidence
            ),
        )

    @staticmethod
    def _checks_identity(config: ProjectConfig) -> str:
        payload = [
            check.model_dump(mode="json")
            for check in config.required_pre_pr_checks
        ]
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _validation_sources_match(
        self,
        run: RunRecord,
        references: tuple[object, ...],
        tested_tree_sha: str | None,
        operation_identity: str | None,
        checks_identity: str | None,
    ) -> bool:
        if (
            tested_tree_sha is None
            or operation_identity is None
            or checks_identity is None
            or not references
        ):
            return False
        for reference in references:
            try:
                evidence = self.repository.get_evidence(  # type: ignore[attr-defined]
                    reference.evidence_id  # type: ignore[attr-defined]
                )
                payload = json.loads(evidence.payload_json)
            except (AttributeError, KeyError, TypeError, json.JSONDecodeError):
                return False
            if (
                evidence.run_id != run.run_id
                or evidence.kind != "local_validation_passed"
                or type(payload) is not dict
                or payload.get("passed") is not True
                or payload.get("sha") != run.head_sha
                or payload.get("tested_tree_sha") != tested_tree_sha
                or payload.get("operation_identity") != operation_identity
                or payload.get("checks_identity") != checks_identity
            ):
                return False
        return True

    def _local_validation_attestation(self, run: RunRecord) -> object:
        candidates = [
            evidence
            for evidence in self.repository.list_evidence(run.run_id)
            if evidence.kind == "local_validation_attestation"
        ]
        if not candidates:
            raise ValueError("local validation tree attestation is missing")
        evidence = candidates[-1]
        payload = json.loads(evidence.payload_json)
        if (
            type(payload) is not dict
            or payload.get("passed") is not True
            or payload.get("expected_parent_sha") != run.head_sha
            or not isinstance(payload.get("operation_identity"), str)
            or not payload["operation_identity"].startswith(
                f"local-validation:{run.run_id}:"
            )
            or not isinstance(payload.get("checks_identity"), str)
            or re.fullmatch(r"[0-9a-f]{64}", payload["checks_identity"])
            is None
            or not isinstance(payload.get("tested_tree_sha"), str)
            or re.fullmatch(r"[0-9a-f]{40}", payload["tested_tree_sha"])
            is None
            or type(payload.get("source_evidence_ids")) is not list
            or not payload["source_evidence_ids"]
            or any(
                not isinstance(evidence_id, str)
                for evidence_id in payload["source_evidence_ids"]
            )
        ):
            raise ValueError("local validation tree attestation is invalid")
        return evidence

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
        if type(payload) is not dict or payload.get("sha") != run.head_sha:
            return StepOutcome(
                state=run.state,
                action=f"{operation_type}_blocked",
                blocked_reason="operation evidence is not bound to current head",
            )
        if payload.get("passed") is not True:
            reasons = {
                "ENVIRONMENT_PREEMPTED": "shared test environment was preempted",
                "MISSING_ACCEPTANCE_CHECK": (
                    "registered host acceptance command is missing"
                ),
                "ACCEPTANCE_ADVISORY_BLOCKED": (
                    "acceptance advisory stage did not pass"
                ),
                "VERSION_NOT_READY": (
                    "test deployment version did not converge before timeout"
                ),
            }
            reason = payload.get("reason")
            return StepOutcome(
                state=run.state,
                action=f"{operation_type}_blocked",
                blocked_reason=reasons.get(
                    reason,
                    "operation authority check did not pass",
                ),
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
                blocked_reason="design stage did not pass",
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
        del config
        return StepOutcome(
            state=run.state,
            action="ready_to_merge",
            blocked_reason="automation stops before merge",
        )

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
            or not isinstance(payload.get("validation_attestation_id"), str)
            or type(payload.get("source_evidence_ids")) is not list
            or not payload["source_evidence_ids"]
            or any(
                not isinstance(evidence_id, str)
                for evidence_id in payload["source_evidence_ids"]
            )
        ):
            raise ValueError("internal review tree attestation is invalid")
        validation = self.repository.get_evidence(
            payload["validation_attestation_id"]
        )
        validation_payload = json.loads(validation.payload_json)
        if (
            validation.run_id != run.run_id
            or validation.kind != "local_validation_attestation"
            or type(validation_payload) is not dict
            or validation_payload.get("tested_tree_sha")
            != payload["reviewed_tree_sha"]
        ):
            raise ValueError("review is not bound to tested tree")
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
            return self._finish_push(
                run, config, branch, "feature_already_pushed"
            )
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
        return self._finish_push(run, config, branch, "feature_pushed")

    def _finish_push(
        self,
        run: RunRecord,
        config: ProjectConfig,
        branch: str,
        action: str,
    ) -> StepOutcome:
        cycle = self._complete_review_fix_cycle(run, config)
        if isinstance(cycle, StepOutcome):
            return cycle
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
            (reference, *cycle),
        )
        return StepOutcome(
            state=RunState.PR_DRAFT,
            action=action,
            evidence_ids=(
                reference.evidence_id,
                *(item.evidence_id for item in cycle),
            ),
        )

    def _complete_review_fix_cycle(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> tuple[object, ...] | StepOutcome:
        request = next(
            (
                self.repository.get_evidence(evidence_id)
                for event in reversed(self.repository.list_events(run.run_id))
                if event.to_state is RunState.FIXING_CI_REVIEW
                for evidence_id in event.evidence_ids
                if self.repository.get_evidence(evidence_id).kind
                == "ci_review_fix_request"
            ),
            None,
        )
        if request is None:
            return ()
        try:
            payload = self._validated_review_fix_request(request, run)
        except (TypeError, ValueError, json.JSONDecodeError):
            return StepOutcome(
                state=RunState.PUSHING_FEATURE,
                action="review_fix_cycle_blocked",
                blocked_reason="review fix request or fix head is invalid",
            )
        github = self.github_factory(config)
        references = []
        for issue in payload["issues"]:
            identity = json.loads(issue["issue_identity"])
            if identity[0] != "review_thread":
                continue
            thread_id = identity[1]
            marker_identity = hashlib.sha256(
                (
                    f"{run.run_id}:{issue['issue_fingerprint']}:"
                    f"{request.evidence_id}:{payload['head_sha']}:"
                    f"{run.head_sha}:{thread_id}"
                ).encode("utf-8")
            ).hexdigest()
            body = (
                "Applied an automated fix for this review thread.\n\n"
                f"<!-- change-delivery-reply:{marker_identity} -->"
            )
            key = (
                f"review_reply:{run.run_id}:"
                f"{issue['issue_fingerprint']}:{run.head_sha}"
            )
            try:
                operation = self.repository.get_remote_operation(key)
            except KeyError:
                operation = None
            observed = github.thread_contains_comment(  # type: ignore[attr-defined]
                run.pr_number, thread_id, body
            )
            if not observed:
                if operation is not None:
                    return StepOutcome(
                        state=RunState.PUSHING_FEATURE,
                        action="review_reply_reconciliation_pending",
                        blocked_reason="review reply is not yet observable",
                    )
                self.repository.begin_remote_operation(
                    key,
                    run.run_id,
                    "review_reply",
                    (
                        f"{config.github_repository}:{run.pr_number}:"
                        f"{thread_id}:{marker_identity}"
                    ),
                )
                github.reply_to_thread(  # type: ignore[attr-defined]
                    run.pr_number, thread_id, body
                )
                observed = github.thread_contains_comment(  # type: ignore[attr-defined]
                    run.pr_number, thread_id, body
                )
                if not observed:
                    return StepOutcome(
                        state=RunState.PUSHING_FEATURE,
                        action="review_reply_reconciliation_pending",
                        blocked_reason="review reply is not yet observable",
                    )
            if operation is not None and operation.status == "PENDING":
                self.repository.confirm_remote_operation(key)
            elif operation is None:
                self.repository.confirm_remote_operation(key)
            reply = self.repository.register_evidence(
                run.run_id,
                f"review-thread-replied:{marker_identity}",
                "review_thread_replied",
                {
                    "run_id": run.run_id,
                    "issue_fingerprint": issue["issue_fingerprint"],
                    "request_evidence_id": request.evidence_id,
                    "request_head_sha": payload["head_sha"],
                    "fix_sha": run.head_sha,
                    "thread_id": thread_id,
                    "pr_number": run.pr_number,
                    "reply_marker_sha256": marker_identity,
                    "passed": True,
                },
            )
            references.append(reply)
        for issue in payload["issues"]:
            pushed_identity = hashlib.sha256(
                (
                    f"{run.run_id}:{issue['issue_fingerprint']}:"
                    f"{request.evidence_id}:{run.head_sha}"
                ).encode("utf-8")
            ).hexdigest()
            pushed = self.repository.register_evidence(
                run.run_id,
                f"review-fix-pushed:{pushed_identity}",
                "review_fix_pushed",
                {
                    "issue_fingerprint": issue["issue_fingerprint"],
                    "observation_fingerprint": issue[
                        "observation_fingerprint"
                    ],
                    "request_evidence_id": request.evidence_id,
                    "request_head_sha": payload["head_sha"],
                    "fix_sha": run.head_sha,
                    "passed": True,
                },
            )
            references.append(pushed)
        return tuple(references)

    def _validated_review_fix_request(
        self,
        request: object,
        run: RunRecord,
    ) -> dict[str, object]:
        payload = json.loads(request.payload_json)  # type: ignore[attr-defined]
        if not hmac.compare_digest(
            request.sha256,  # type: ignore[attr-defined]
            sha256_payload(payload),
        ):
            raise ValueError("review fix request digest mismatch")
        if (
            type(payload) is not dict
            or set(payload) != {
                "attempt",
                "issues",
                "feedback_fingerprint",
                "head_sha",
                "pr_number",
                "untrusted_data",
            }
            or type(payload["attempt"]) is not int
            or type(payload["issues"]) is not list
            or not payload["issues"]
            or type(payload["feedback_fingerprint"]) is not str
            or type(payload["untrusted_data"]) is not dict
            or payload["head_sha"] == run.head_sha
            or type(payload["head_sha"]) is not str
            or _SHA.fullmatch(payload["head_sha"]) is None
            or type(payload["pr_number"]) is not int
            or payload["pr_number"] <= 0
            or payload["pr_number"] != run.pr_number
            or run.head_sha is None
            or run.pr_number is None
        ):
            raise ValueError("review fix request schema is invalid")
        untrusted_data = payload["untrusted_data"]
        if (
            set(untrusted_data) != {
                "failed_checks",
                "requested_reviews",
                "threads",
            }
            or untrusted_data["failed_checks"] != []
            or type(untrusted_data["requested_reviews"]) is not list
            or type(untrusted_data["threads"]) is not list
        ):
            raise ValueError("review fix untrusted data is invalid")
        for review in untrusted_data["requested_reviews"]:
            if type(review) is not dict or set(review) != {
                "review_id",
                "author",
                "body",
            }:
                raise ValueError("requested review evidence is invalid")
            try:
                RequestedReviewFact(**review)
            except (TypeError, ValueError):
                raise ValueError("requested review evidence is invalid") from None
        for thread in untrusted_data["threads"]:
            if (
                type(thread) is not dict
                or set(thread) != {"thread_id", "path", "line", "comments"}
                or type(thread["comments"]) is not list
                or any(type(comment) is not str for comment in thread["comments"])
            ):
                raise ValueError("review thread evidence is invalid")
            try:
                ReviewThread(
                    thread_id=thread["thread_id"],
                    resolved=False,
                    outdated=False,
                    path=thread["path"],
                    line=thread["line"],
                    comments=tuple(thread["comments"]),
                )
            except (TypeError, ValueError):
                raise ValueError("review thread evidence is invalid") from None
        feedback_fingerprint = hashlib.sha256(
            json.dumps(
                untrusted_data,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(
            payload["feedback_fingerprint"], feedback_fingerprint
        ):
            raise ValueError("review fix feedback fingerprint is invalid")
        observed_issues = {
            issue["issue_fingerprint"]: issue
            for issue in self._feedback_issues(untrusted_data)
        }
        attempts = []
        seen: set[str] = set()
        for issue in payload["issues"]:
            if (
                type(issue) is not dict
                or set(issue) != {
                    "issue_identity",
                    "issue_fingerprint",
                    "observation_identity",
                    "observation_fingerprint",
                    "existing_attempts",
                    "attempt",
                }
                or type(issue["existing_attempts"]) is not int
                or type(issue["attempt"]) is not int
                or issue["attempt"] != issue["existing_attempts"] + 1
                or not 1 <= issue["attempt"] <= 2
            ):
                raise ValueError("review fix issue schema is invalid")
            fingerprint = issue.get("issue_fingerprint")
            if type(fingerprint) is not str or fingerprint in seen:
                raise ValueError("review fix issue identity is invalid")
            expected = observed_issues.get(fingerprint)
            if expected is None or any(
                issue.get(key) != expected[key]
                for key in (
                    "issue_identity",
                    "issue_fingerprint",
                    "observation_identity",
                    "observation_fingerprint",
                )
            ):
                raise ValueError("review fix issue does not match observation")
            seen.add(fingerprint)
            attempts.append(issue["attempt"])
        if payload["attempt"] != max(attempts):
            raise ValueError("review fix aggregate attempt is invalid")
        return payload

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
        if existing is not None and existing.status == "FAILED":
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
                number = self._validate_pr_fact(
                    found, branch, config.base_branch, run.head_sha
                )
            else:
                created = github.ensure_draft_pr(
                    branch,
                    config.base_branch,
                    f"chore: delivery {run.run_id}",
                    f"Automated delivery run: {run.run_id}",
                )
                number = self._validate_pr_fact(
                    created, branch, config.base_branch, run.head_sha
                )
        except DependencyUnavailable:
            raise
        except Exception as error:
            return StepOutcome(
                state=run.state,
                action="pr_reconciliation_pending",
                blocked_reason=f"PR result is uncertain: {type(error).__name__}",
            )
        if existing is None or existing.status == "PENDING":
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

    @staticmethod
    def _validate_pr_fact(
        fact: object,
        head: str,
        base: str,
        head_sha: str,
    ) -> int:
        if (
            not isinstance(fact, PullRequestRef)
            or type(fact.number) is not int
            or fact.number <= 0
            or fact.head != head
            or fact.base != base
            or fact.head_sha != head_sha
        ):
            raise ValueError("PR fact is incomplete or does not match run")
        return fact.number

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
            if (
                snapshot.checks_status == "failed"
                or snapshot.review_status == "changes_requested"
                or bool(snapshot.unresolved_thread_ids)
            ):
                untrusted_data = self._actionable_feedback(
                    github, run.pr_number, snapshot
                )
                if untrusted_data["failed_checks"]:
                    missing = self.repository.register_evidence(
                        run.run_id,
                        f"actionable-ci-details-missing:{run.run_id}:{run.head_sha}",
                        "actionable_feedback_missing",
                        {
                            "checks_status": snapshot.checks_status,
                            "review_status": snapshot.review_status,
                            "pr_number": run.pr_number,
                            "reason": "failed checks lack a trusted diagnostic summary",
                        },
                    )
                    self.repository.transition_with_evidence(
                        run.run_id,
                        RunState.MONITORING_CI_REVIEW,
                        RunState.REVIEW_DECISION_REQUIRED,
                        "ACTIONABLE_CI_DETAILS_MISSING",
                        (missing,),
                    )
                    return StepOutcome(
                        state=RunState.REVIEW_DECISION_REQUIRED,
                        action="actionable_feedback_missing",
                        blocked_reason=(
                            "failed checks lack a trusted diagnostic summary"
                        ),
                        evidence_ids=(missing.evidence_id,),
                    )
                issues = self._feedback_issues(untrusted_data)
                if not issues:
                    missing = self.repository.register_evidence(
                        run.run_id,
                        f"actionable-feedback-missing:{run.run_id}:{run.head_sha}",
                        "actionable_feedback_missing",
                        {
                            "checks_status": snapshot.checks_status,
                            "review_status": snapshot.review_status,
                            "pr_number": run.pr_number,
                        },
                    )
                    self.repository.transition_with_evidence(
                        run.run_id,
                        RunState.MONITORING_CI_REVIEW,
                        RunState.REVIEW_DECISION_REQUIRED,
                        "ACTIONABLE_FEEDBACK_MISSING",
                        (missing,),
                    )
                    return StepOutcome(
                        state=RunState.REVIEW_DECISION_REQUIRED,
                        action="actionable_feedback_missing",
                        blocked_reason="remote failure lacks actionable details",
                        evidence_ids=(missing.evidence_id,),
                    )
                issues = tuple(
                    issue
                    for issue in issues
                    if not self._review_issue_awaits_update(
                        run.run_id,
                        issue["issue_fingerprint"],
                        issue["observation_fingerprint"],
                    )
                )
                if not issues:
                    return StepOutcome(
                        state=RunState.MONITORING_CI_REVIEW,
                        action="awaiting_review_update",
                        blocked_reason=(
                            "previous feedback has not been updated for fix head"
                        ),
                    )
                feedback_fingerprint = hashlib.sha256(
                    json.dumps(
                        untrusted_data,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()
                attempt_counts = self._review_fix_attempts(
                    run.run_id,
                    tuple(issue["issue_fingerprint"] for issue in issues),
                )
                exhausted = [
                    issue
                    for issue in issues
                    if attempt_counts[issue["issue_fingerprint"]] >= 2
                ]
                if exhausted:
                    limit_identity = hashlib.sha256(
                        json.dumps(
                            sorted(
                                issue["issue_fingerprint"]
                                for issue in exhausted
                            ),
                            separators=(",", ":"),
                        ).encode("ascii")
                    ).hexdigest()
                    limit = self.repository.register_evidence(
                        run.run_id,
                        f"review-fix-limit:{run.run_id}:{limit_identity}",
                        "review_fix_limit_reached",
                        {
                            "exhausted_issue_fingerprints": sorted(
                                issue["issue_fingerprint"]
                                for issue in exhausted
                            ),
                            "attempts": 2,
                            "pr_number": run.pr_number,
                        },
                    )
                    self.repository.transition_with_evidence(
                        run.run_id,
                        RunState.MONITORING_CI_REVIEW,
                        RunState.REVIEW_DECISION_REQUIRED,
                        "REVIEW_FIX_LIMIT_REACHED",
                        (limit,),
                    )
                    return StepOutcome(
                        state=RunState.REVIEW_DECISION_REQUIRED,
                        action="review_fix_limit_reached",
                        blocked_reason=(
                            "same feedback remained after two automatic fix rounds"
                        ),
                        evidence_ids=(limit.evidence_id,),
                    )
                issue_attempts = [
                    {
                        **issue,
                        "existing_attempts": attempt_counts[
                            issue["issue_fingerprint"]
                        ],
                        "attempt": (
                            attempt_counts[issue["issue_fingerprint"]] + 1
                        ),
                    }
                    for issue in issues
                ]
                attempt = max(item["attempt"] for item in issue_attempts)
                request_identity = hashlib.sha256(
                    json.dumps(
                        sorted(
                            item["issue_fingerprint"]
                            for item in issue_attempts
                        ),
                        separators=(",", ":"),
                    ).encode("ascii")
                ).hexdigest()
                request = self.repository.register_evidence(
                    run.run_id,
                    (
                        f"ci-review-fix-request:{run.run_id}:"
                        f"{request_identity}:{attempt}"
                    ),
                    "ci_review_fix_request",
                    {
                        "attempt": attempt,
                        "issues": issue_attempts,
                        "feedback_fingerprint": feedback_fingerprint,
                        "head_sha": run.head_sha,
                        "pr_number": run.pr_number,
                        "untrusted_data": untrusted_data,
                    },
                )
                self.repository.transition_with_evidence(
                    run.run_id,
                    RunState.MONITORING_CI_REVIEW,
                    RunState.FIXING_CI_REVIEW,
                    "CI_OR_REVIEW_CHANGES_REQUIRED",
                    (request,),
                )
                return StepOutcome(
                    state=RunState.FIXING_CI_REVIEW,
                    action="ci_review_changes_required",
                    blocked_reason="CI or review gates require changes",
                    evidence_ids=(request.evidence_id,),
                )
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
            if not hmac.compare_digest(
                evidence.sha256, sha256_payload(payload)
            ):
                raise ValueError("merge gate evidence digest mismatch")
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

    @staticmethod
    def _actionable_feedback(
        github: object,
        pr_number: int,
        snapshot: object,
    ) -> dict[str, object]:
        threads = github.list_actionable_reviews(pr_number)  # type: ignore[attr-defined]
        if not isinstance(threads, tuple) or any(
            not isinstance(thread, ReviewThread) for thread in threads
        ):
            raise ValueError("actionable review facts are invalid")
        thread_ids = tuple(thread.thread_id for thread in threads)
        if thread_ids != tuple(sorted(set(thread_ids))):
            raise ValueError("actionable review facts are not stable and unique")
        if set(thread_ids) != set(snapshot.unresolved_thread_ids):
            raise ValueError("actionable review facts changed during observation")
        return {
            "failed_checks": [
                fact.model_dump(mode="json")
                for fact in snapshot.failed_checks
            ],
            "requested_reviews": [
                fact.model_dump(mode="json")
                for fact in snapshot.requested_reviews
            ],
            "threads": [
                {
                    "thread_id": thread.thread_id,
                    "path": thread.path,
                    "line": thread.line,
                    "comments": list(thread.comments),
                }
                for thread in threads
            ],
        }

    @staticmethod
    def _feedback_issues(
        untrusted_data: dict[str, object],
    ) -> tuple[dict[str, str], ...]:
        identities: dict[tuple[str, ...], tuple[str, ...]] = {}
        for review in untrusted_data["requested_reviews"]:  # type: ignore[union-attr]
            normalized_body = " ".join(review["body"].split()).casefold()
            identity = (
                "requested_review",
                review["author"].casefold(),
                normalized_body,
            )
            identities[identity] = (
                *identity,
                review["review_id"],
            )
        for thread in untrusted_data["threads"]:  # type: ignore[union-attr]
            identity = ("review_thread", thread["thread_id"])
            reviewer_comments = tuple(
                comment
                for comment in thread["comments"]
                if "<!-- change-delivery-reply:" not in comment
            )
            identities[identity] = (
                *identity,
                thread.get("path") or "",
                str(thread.get("line") or ""),
                *reviewer_comments,
            )
        issues = []
        for identity, observation in sorted(identities.items()):
            encoded = json.dumps(
                identity,
                ensure_ascii=True,
                separators=(",", ":"),
            )
            observation_encoded = json.dumps(
                observation,
                ensure_ascii=True,
                separators=(",", ":"),
            )
            issues.append(
                {
                    "issue_identity": encoded,
                    "issue_fingerprint": hashlib.sha256(
                        encoded.encode("ascii")
                    ).hexdigest(),
                    "observation_identity": observation_encoded,
                    "observation_fingerprint": hashlib.sha256(
                        observation_encoded.encode("ascii")
                    ).hexdigest(),
                }
            )
        return tuple(issues)

    def _review_issue_awaits_update(
        self,
        run_id: str,
        issue_fingerprint: str,
        observation_fingerprint: str,
    ) -> bool:
        for evidence in reversed(self.repository.list_evidence(run_id)):
            if evidence.kind != "review_fix_pushed":
                continue
            payload = json.loads(evidence.payload_json)
            if not hmac.compare_digest(
                evidence.sha256, sha256_payload(payload)
            ):
                raise ValueError("review fix push evidence digest mismatch")
            if (
                type(payload) is dict
                and payload.get("issue_fingerprint") == issue_fingerprint
            ):
                return payload.get("observation_fingerprint") == (
                    observation_fingerprint
                )
        return False

    def _review_fix_attempts(
        self,
        run_id: str,
        fingerprints: tuple[str, ...],
    ) -> dict[str, int]:
        bound_ids = {
            evidence_id
            for event in self.repository.list_events(run_id)
            if event.to_state is RunState.FIXING_CI_REVIEW
            for evidence_id in event.evidence_ids
        }
        attempts = {fingerprint: set() for fingerprint in fingerprints}
        for evidence in self.repository.list_evidence(run_id):
            if (
                evidence.evidence_id not in bound_ids
                or evidence.kind != "ci_review_fix_request"
            ):
                continue
            payload = json.loads(evidence.payload_json)
            if not hmac.compare_digest(
                evidence.sha256, sha256_payload(payload)
            ):
                raise ValueError("review fix attempt evidence digest mismatch")
            if type(payload) is not dict or type(payload.get("issues")) is not list:
                raise ValueError("review fix attempt evidence is invalid")
            for issue in payload["issues"]:
                if (
                    type(issue) is not dict
                    or type(issue.get("issue_identity")) is not str
                    or type(issue.get("issue_fingerprint")) is not str
                    or type(issue.get("attempt")) is not int
                    or not 1 <= issue["attempt"] <= 2
                ):
                    raise ValueError("review fix issue attempt is invalid")
                fingerprint = issue["issue_fingerprint"]
                actual_fingerprint = hashlib.sha256(
                    issue["issue_identity"].encode("ascii")
                ).hexdigest()
                if not hmac.compare_digest(fingerprint, actual_fingerprint):
                    raise ValueError("review fix issue identity is invalid")
                if fingerprint in attempts:
                    attempts[fingerprint].add(issue["attempt"])
        for issue_attempts in attempts.values():
            if issue_attempts and issue_attempts != set(
                range(1, max(issue_attempts) + 1)
            ):
                raise ValueError("review fix attempt history is not contiguous")
        return {
            fingerprint: len(issue_attempts)
            for fingerprint, issue_attempts in attempts.items()
        }

    def cancel(self, run_id: str) -> StepOutcome:
        run = self.repository.get_run(run_id)
        if run.state is RunState.CANCELLED:
            return StepOutcome(state=RunState.CANCELLED, action="already_cancelled")
        if run.state in {
            RunState.COMPLETED,
            RunState.FAILED_TERMINAL,
        }:
            raise ValueError("terminal run cannot be cancelled")
        try:
            self.artifact_store.write_resource_inventory(
                run_id,
                branch=self._feature_branch(run),
                worktree=self.plugin_root / "worktrees" / run_id,
                pr_url="unavailable",
                pr_number=run.pr_number,
                test_sha=run.head_sha,
            )
        except (OSError, ValueError):
            pass
        self.repository.transition_with_evidence(
            run_id,
            run.state,
            RunState.CANCELLED,
            "RUN_CANCELLED",
        )
        return StepOutcome(state=RunState.CANCELLED, action="cancelled")
