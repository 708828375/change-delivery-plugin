from __future__ import annotations

from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from io import StringIO
import hashlib
import json
import os
import re
import sys
from types import SimpleNamespace

import pytest

import change_delivery.cli as cli_module
from change_delivery.approval import build_approval, save_approval
from change_delivery.adapters.codex import CodexAdapter
from change_delivery.adapters.github import (
    FailedCheckFact,
    PullRequestRef,
    RequestedReviewFact,
    ReviewThread,
)
from change_delivery.database import Database
from change_delivery.cli import CliServices, main
from change_delivery.config import load_project_config
from change_delivery.domain import (
    EvidenceRef,
    CommandSpec,
    Finding,
    ProjectConfig,
    RunState,
    StageResult,
)
from change_delivery.evidence import sha256_payload
from change_delivery.notifications import NotificationService
from change_delivery.orchestrator import Orchestrator
from change_delivery.process import ProcessResult
from change_delivery.repository import RunRepository
from change_delivery.retention import RetentionService
from change_delivery.runtime import (
    RuntimeAcceptancePort,
    RuntimeStageExecutor,
    RuntimeTestEnvironmentPort,
)
from change_delivery.stage_content import CommitDraft


SHA = "a" * 40
OTHER_SHA = "b" * 40
NEW_SHA = "c" * 40
TREE_SHA = "d" * 40
OTHER_TREE_SHA = "e" * 40
CONFIG_HASH = "c" * 64
FAILED_CHECK = FailedCheckFact(
    kind="check_run",
    name="unit-tests",
    workflow="CI",
    conclusion="FAILURE",
    details_url="https://github.com/owner/repo/actions/runs/7",
)
REQUESTED_REVIEW = RequestedReviewFact(
    review_id="PRR_actionable",
    author="reviewer",
    body="Add a regression test for the timeout path.",
)


def config(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "project_id": "test-project",
            "repo_path": tmp_path.resolve(),
            "remote": "origin",
            "remote_fetch_url": "https://example.invalid/repo.git",
            "remote_push_url": "https://example.invalid/repo.git",
            "base_branch": "develop",
            "test_branch": "test",
            "github_repository": "owner/repo",
            "version_endpoint": "https://example.invalid/version",
            "version_sha_field": "sha",
            "required_pre_pr_checks": [
                {
                    "executable": "python",
                    "args": ["-m", "pytest"],
                    "workdir": tmp_path.resolve(),
                }
            ],
        }
    )


def write_registered_project(root: Path) -> None:
    projects = root / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    (projects / "test-project.yaml").write_text(
        f"""
project_id: test-project
repo_path: {root.as_posix()}
remote: origin
remote_fetch_url: https://example.invalid/repo.git
remote_push_url: https://example.invalid/repo.git
base_branch: develop
test_branch: test
github_repository: owner/repo
version_endpoint: https://example.invalid/version
version_sha_field: sha
required_pre_pr_checks:
  - executable: python
    args: [-m, pytest]
    workdir: {root.as_posix()}
""",
        encoding="utf-8",
    )


@dataclass
class FakeGit:
    remote_feature_sha: str | None = None
    push_calls: int = 0
    fail_push: BaseException | None = None
    remove_worktree_calls: int = 0
    transaction_probe: object | None = None

    def feature_sha(self, branch: str) -> str | None:
        if self.transaction_probe is not None:
            with self.transaction_probe.transaction() as connection:  # type: ignore[union-attr]
                connection.execute("SELECT 1")
        return self.remote_feature_sha

    def push_feature(self, branch: str, expected_sha: str) -> str:
        self.push_calls += 1
        if self.transaction_probe is not None:
            with self.transaction_probe.transaction() as connection:  # type: ignore[union-attr]
                connection.execute("SELECT 1")
        if self.fail_push is not None:
            raise self.fail_push
        self.remote_feature_sha = expected_sha
        return expected_sha


@dataclass
class FakeGitHub:
    ensure_calls: int = 0
    find_calls: int = 0
    close_pr_calls: int = 0
    pr_number: int = 42
    snapshot_result: object | None = None
    get_calls: int = 0
    mark_ready_calls: int = 0
    is_draft: bool = True
    merged: bool = False
    merge_query_calls: int = 0
    actionable_reviews: tuple[ReviewThread, ...] = ()
    reply_calls: int = 0
    replied_comments: dict[str, list[str]] = field(default_factory=dict)
    hide_reply_once: bool = False

    def ensure_draft_pr(
        self, branch: str, base: str, title: str, body: str
    ) -> PullRequestRef:
        self.ensure_calls += 1
        return self._pr(branch, base)

    def find_pr(self, branch: str, base: str) -> PullRequestRef | None:
        self.find_calls += 1
        return self._pr(branch, base) if self.pr_number else None

    def _pr(self, branch: str, base: str) -> PullRequestRef:
        return PullRequestRef(
            number=self.pr_number,
            url=f"https://github.com/owner/repo/pull/{self.pr_number}",
            base=base,
            head=branch,
            is_draft=self.is_draft,
            head_sha=SHA,
        )

    def snapshot(self, pr_number: int) -> object:
        assert self.snapshot_result is not None
        return self.snapshot_result

    def list_actionable_reviews(
        self, pr_number: int
    ) -> tuple[ReviewThread, ...]:
        assert pr_number == self.pr_number
        return self.actionable_reviews

    def reply_to_thread(
        self, pr_number: int, thread_id: str, body: str
    ) -> None:
        assert pr_number == self.pr_number
        self.reply_calls += 1
        self.replied_comments.setdefault(thread_id, []).append(body)

    def thread_contains_comment(
        self, pr_number: int, thread_id: str, body: str
    ) -> bool:
        assert pr_number == self.pr_number
        if self.hide_reply_once and body in self.replied_comments.get(
            thread_id, []
        ):
            self.hide_reply_once = False
            return False
        return body in self.replied_comments.get(thread_id, [])

    def get_pr(self, pr_number: int) -> object:
        self.get_calls += 1
        return PrFact(self.pr_number, SHA, self.is_draft)

    def mark_ready(self, pr_number: int) -> object:
        self.mark_ready_calls += 1
        self.is_draft = False
        return PrFact(self.pr_number, SHA, False)

    def is_merged(self, pr_number: int) -> bool:
        self.merge_query_calls += 1
        return self.merged


@dataclass(frozen=True)
class Snapshot:
    head_sha: str
    checks_status: str = "passed"
    review_status: str = "approved"
    unresolved_thread_ids: tuple[str, ...] = ()
    mergeable: bool = True
    is_draft: bool = False
    failed_checks: tuple[FailedCheckFact, ...] = ()
    requested_reviews: tuple[RequestedReviewFact, ...] = ()

    @property
    def checks_passed(self) -> bool:
        return self.checks_status == "passed"

    @property
    def review_decision(self) -> bool:
        return self.review_status == "approved"

    @property
    def threads_resolved(self) -> bool:
        return not self.unresolved_thread_ids


@dataclass(frozen=True)
class PrFact:
    number: int
    head_sha: str
    is_draft: bool


class FakeStageExecutor:
    def __init__(
        self,
        repository: RunRepository,
        *,
        status: str = "passed",
        findings: tuple[Finding, ...] = (),
    ) -> None:
        self.repository = repository
        self.status = status
        self.findings = findings
        self.calls: list[str] = []
        self.commit_draft_calls = 0

    def generate_commit_draft(
        self,
        run: object,
        project: object,
        reviewed_tree_sha: str,
        expected_parent_sha: str,
    ) -> tuple[CommitDraft, str]:
        self.commit_draft_calls += 1
        return CommitDraft(message="feat(delivery): 使用阶段 Skill"), "f" * 64

    def execute(
        self,
        stage: str,
        run: object,
        project: object,
        run_root: Path,
        *,
        operation_identity: str | None = None,
        tested_tree_sha: str | None = None,
        checks_identity: str | None = None,
    ) -> StageResult:
        self.calls.append(stage)
        run_id = run.run_id  # type: ignore[attr-defined]
        head_sha = run.head_sha  # type: ignore[attr-defined]
        evidence_id = f"{stage}-proof:{run_id}"
        if stage == "local_validation" and operation_identity is not None:
            evidence_id = (
                f"{evidence_id}:{operation_identity.rsplit(':', 1)[-1]}"
            )
        reference = self.repository.register_evidence(
            run_id,
            evidence_id,
            (
                "internal_review_source"
                if stage == "internal_review"
                else f"{stage}_passed"
            ),
            {
                "sha": head_sha,
                "passed": True,
                "operation_identity": operation_identity,
                "tested_tree_sha": tested_tree_sha,
                "checks_identity": checks_identity,
            },
        )
        return StageResult(
            status=self.status,
            summary=f"{stage} passed",
            evidence=(reference,),
            findings=self.findings,
            next_action="continue",
        )


class FakeReconciledPort:
    def __init__(self, observed: dict[str, object] | None = None) -> None:
        self.observed = observed
        self.query_calls = 0
        self.execute_calls = 0

    def query(self, run: object, project: object) -> dict[str, object] | None:
        self.query_calls += 1
        return self.observed

    def execute(self, run: object, project: object) -> dict[str, object]:
        self.execute_calls += 1
        self.observed = {
            "sha": run.head_sha,  # type: ignore[attr-defined]
            "passed": True,
        }
        return self.observed


class FakeCleanupPort:
    def __init__(self, *, clean: bool = True, removed: bool = False) -> None:
        self.clean = clean
        self.removed = removed
        self.execute_calls = 0

    def query(self, run: object, project: object) -> dict[str, bool]:
        return {"clean": self.clean, "removed": self.removed}

    def execute(self, run: object, project: object) -> dict[str, bool]:
        self.execute_calls += 1
        self.removed = True
        return {"clean": True, "removed": True}


class FakeCommitPort:
    def __init__(
        self,
        *,
        tree_sha: str = TREE_SHA,
        commit_tree_sha: str = TREE_SHA,
        observed: dict[str, object] | None = None,
    ) -> None:
        self.tree_sha = tree_sha
        self.commit_tree_sha = commit_tree_sha
        self.observed = observed
        self.query_calls = 0
        self.execute_calls = 0
        self.review_tree_calls = 0
        self.validation_tree_calls = 0
        self.messages: list[str] = []

    def validation_tree(self, run: object, project: object) -> str:
        self.validation_tree_calls += 1
        return self.tree_sha

    def review_tree(self, run: object, project: object) -> str:
        self.review_tree_calls += 1
        return self.tree_sha

    def query(
        self,
        run: object,
        project: object,
        message: str,
    ) -> dict[str, object] | None:
        self.query_calls += 1
        self.messages.append(message)
        return self.observed

    def execute(
        self,
        run: object,
        project: object,
        message: str,
    ) -> dict[str, object]:
        self.execute_calls += 1
        self.messages.append(message)
        self.observed = {
            "new_head_sha": NEW_SHA,
            "commit_tree_sha": self.commit_tree_sha,
            "parent_sha": run.head_sha,  # type: ignore[attr-defined]
            "marker_run_id": run.run_id,  # type: ignore[attr-defined]
            "passed": True,
        }
        return self.observed

@dataclass
class Fixture:
    root: Path
    repository: RunRepository
    git: FakeGit = field(default_factory=FakeGit)
    github: FakeGitHub = field(default_factory=FakeGitHub)

    def create(self, state: RunState) -> str:
        run_id = f"run-{state.value.lower()}"
        self.repository.create_run(run_id, "test-project", CONFIG_HASH)
        with self.repository.database.transaction() as connection:
            connection.execute(
                "UPDATE runs SET state = ?, head_sha = ? WHERE run_id = ?",
                (state.value, SHA, run_id),
            )
        run_root = self.root / "runs" / run_id
        run_root.mkdir(parents=True)
        for name in ("requirement.md", "design.md", "implementation-plan.md"):
            (run_root / name).write_text(name, encoding="utf-8")
        return run_id

    def orchestrator(self) -> Orchestrator:
        return Orchestrator(
            repository=self.repository,
            plugin_root=self.root,
            config_loader=lambda project_id: (config(self.root), CONFIG_HASH),
            git_factory=lambda _: self.git,
            github_factory=lambda _: self.github,
            clock=lambda: datetime(2026, 7, 29, tzinfo=UTC),
            approval_validator=lambda run, project, root: True,
        )


@pytest.fixture
def fixture(tmp_path: Path) -> Fixture:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    return Fixture(tmp_path, RunRepository(database))


def approve_fixture_run(fixture: Fixture) -> tuple[str, Path]:
    run_id = fixture.create(RunState.AWAITING_DESIGN_APPROVAL)
    run_root = fixture.root / "runs" / run_id
    snapshot = build_approval(
        run_id=run_id,
        project_id="test-project",
        config_hash=CONFIG_HASH,
        base_branch="develop",
        base_sha=SHA,
        test_branch="test",
        files=tuple(
            run_root / name
            for name in ("requirement.md", "design.md", "implementation-plan.md")
        ),
        approved_user="tester",
        user_provider=lambda: "tester",
        clock=lambda: datetime(2026, 7, 29, tzinfo=UTC),
    )
    approval_file = run_root / "approval.json"
    save_approval(approval_file, snapshot)
    approval_hash = hashlib.sha256(approval_file.read_bytes()).hexdigest()
    services = CliServices(
        plugin_root=fixture.root,
        repository=fixture.repository,
        project_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
    )
    assert main(
        [
            "approve",
            run_id,
            "--approval-file",
            str(approval_file),
            "--confirm-hash",
            approval_hash,
            "--json",
        ],
        services=services,
        stdout=StringIO(),
        stderr=StringIO(),
    ) == 0
    return run_id, run_root


def test_remote_operation_and_evidence_are_fail_closed(fixture: Fixture) -> None:
    fixture.repository.create_run("run-1", "test-project", CONFIG_HASH)
    fixture.repository.create_run("run-2", "test-project", CONFIG_HASH)
    ref = fixture.repository.register_evidence(
        "run-1", "head-1", "head_sha_verified", {"sha": SHA}
    )
    assert fixture.repository.get_evidence("head-1").reference == ref
    with pytest.raises(ValueError, match="conflict"):
        fixture.repository.register_evidence(
            "run-1", "head-1", "head_sha_verified", {"sha": OTHER_SHA}
        )
    with pytest.raises(ValueError, match="belong"):
        fixture.repository.transition_with_evidence(
            "run-2",
            RunState.DRAFT,
            RunState.AWAITING_DESIGN_APPROVAL,
            "bad",
            (ref,),
        )

    operation = fixture.repository.begin_remote_operation(
        "push:run-1:feature:x:" + SHA,
        "run-1",
        "push_feature",
        f"feature/x:{SHA}",
    )
    assert operation.status == "PENDING"
    assert fixture.repository.begin_remote_operation(
        operation.operation_key,
        "run-1",
        "push_feature",
        f"feature/x:{SHA}",
    ) == operation
    confirmed = fixture.repository.confirm_remote_operation(operation.operation_key)
    assert confirmed.status == "CONFIRMED"
    with pytest.raises(ValueError, match="terminal"):
        fixture.repository.fail_remote_operation(operation.operation_key)


def test_primary_stage_result_is_rebound_by_id_kind_sha_and_run(
    fixture: Fixture,
) -> None:
    fixture.repository.create_run("run-1", "test-project", CONFIG_HASH)
    fixture.repository.create_run("run-2", "test-project", CONFIG_HASH)
    reference = fixture.repository.register_evidence(
        "run-1", "stage-proof", "local_validation", {"sha": SHA}
    )

    def result(ref: EvidenceRef) -> StageResult:
        return StageResult(
            status="passed",
            summary="validated",
            evidence=(ref,),
            findings=(),
            next_action="continue",
        )

    assert fixture.repository.rebind_primary_stage_result(
        "run-1", result(reference), primary=True
    ) == (reference,)
    with pytest.raises(ValueError, match="another run"):
        fixture.repository.rebind_primary_stage_result(
            "run-2", result(reference), primary=True
        )
    forged = EvidenceRef(
        evidence_id=reference.evidence_id,
        kind=reference.kind,
        sha256="f" * 64,
    )
    with pytest.raises(ValueError, match="digest"):
        fixture.repository.rebind_primary_stage_result(
            "run-1", result(forged), primary=True
        )
    with pytest.raises(ValueError, match="repair"):
        fixture.repository.rebind_primary_stage_result(
            "run-1", result(reference), primary=False
        )


def test_existing_remote_sha_advances_without_push(fixture: Fixture) -> None:
    run_id = fixture.create(RunState.PUSHING_FEATURE)
    fixture.git.remote_feature_sha = SHA

    outcome = fixture.orchestrator().step(run_id)

    assert outcome.state is RunState.PR_DRAFT
    assert outcome.action == "feature_already_pushed"
    assert fixture.git.push_calls == 0
    assert outcome.evidence_ids


def test_pending_push_reconciles_success_without_replay(fixture: Fixture) -> None:
    run_id = fixture.create(RunState.PUSHING_FEATURE)
    operation_key = f"push_feature:{run_id}:feature/{run_id}:{SHA}"
    fixture.repository.begin_remote_operation(
        operation_key,
        run_id,
        "push_feature",
        f"feature/{run_id}:{SHA}",
    )
    fixture.git.remote_feature_sha = SHA

    outcome = fixture.orchestrator().step(run_id)

    assert outcome.state is RunState.PR_DRAFT
    assert fixture.git.push_calls == 0
    assert fixture.repository.get_remote_operation(operation_key).status == "CONFIRMED"


def test_pending_push_with_no_remote_result_is_not_replayed(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.PUSHING_FEATURE)
    fixture.repository.begin_remote_operation(
        f"push_feature:{run_id}:feature/{run_id}:{SHA}",
        run_id,
        "push_feature",
        f"feature/{run_id}:{SHA}",
    )

    outcome = fixture.orchestrator().step(run_id)

    assert outcome.state is RunState.PUSHING_FEATURE
    assert outcome.blocked_reason
    assert fixture.git.push_calls == 0


def test_new_push_executes_one_side_effect_and_advances(fixture: Fixture) -> None:
    run_id = fixture.create(RunState.PUSHING_FEATURE)

    outcome = fixture.orchestrator().step(run_id)

    assert outcome.state is RunState.PR_DRAFT
    assert fixture.git.push_calls == 1
    assert fixture.github.ensure_calls == 0


def test_remote_calls_run_outside_repository_transactions(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.PUSHING_FEATURE)
    fixture.git.transaction_probe = fixture.repository.database

    outcome = fixture.orchestrator().step(run_id)

    assert outcome.state is RunState.PR_DRAFT


def test_push_exception_leaves_pending_for_reconciliation(fixture: Fixture) -> None:
    run_id = fixture.create(RunState.PUSHING_FEATURE)
    fixture.git.fail_push = TimeoutError("uncertain")

    outcome = fixture.orchestrator().step(run_id)

    assert outcome.state is RunState.PUSHING_FEATURE
    assert outcome.blocked_reason
    operations = fixture.repository.list_remote_operations(run_id)
    assert len(operations) == 1
    assert operations[0].status == "PENDING"


def test_pr_step_is_separate_side_effect(fixture: Fixture) -> None:
    run_id = fixture.create(RunState.PR_DRAFT)

    outcome = fixture.orchestrator().step(run_id)

    assert outcome.state is RunState.DEPLOYING_TEST
    assert fixture.github.ensure_calls == 1
    assert fixture.repository.get_run(run_id).pr_number == 42


def test_pending_pr_only_queries_and_never_replays_create(fixture: Fixture) -> None:
    run_id = fixture.create(RunState.PR_DRAFT)
    branch = f"feature/{run_id}"
    identity = f"owner/repo:{branch}:{SHA}"
    fixture.repository.begin_remote_operation(
        f"draft_pr:{run_id}:{identity}",
        run_id,
        "draft_pr",
        identity,
    )

    outcome = fixture.orchestrator().step(run_id)

    assert outcome.state is RunState.DEPLOYING_TEST
    assert fixture.github.find_calls == 1
    assert fixture.github.ensure_calls == 0


def test_confirmed_pr_operation_recovers_by_query_without_create(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.PR_DRAFT)
    branch = f"feature/{run_id}"
    identity = f"owner/repo:{branch}:{SHA}"
    key = f"draft_pr:{run_id}:{identity}"
    fixture.repository.begin_remote_operation(
        key,
        run_id,
        "draft_pr",
        identity,
    )
    fixture.repository.confirm_remote_operation(key)

    outcome = fixture.orchestrator().step(run_id)

    assert outcome.state is RunState.DEPLOYING_TEST
    assert fixture.github.find_calls == 1
    assert fixture.github.ensure_calls == 0


def test_pr_reconciliation_rejects_untyped_remote_fact(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.PR_DRAFT)
    branch = f"feature/{run_id}"
    identity = f"owner/repo:{branch}:{SHA}"
    fixture.repository.begin_remote_operation(
        f"draft_pr:{run_id}:{identity}",
        run_id,
        "draft_pr",
        identity,
    )
    fixture.github.find_pr = lambda branch, base: object()  # type: ignore[method-assign]

    outcome = fixture.orchestrator().step(run_id)

    assert outcome.state is RunState.PR_DRAFT
    assert fixture.github.ensure_calls == 0
    assert "ValueError" in (outcome.blocked_reason or "")


def test_cancel_is_idempotent_and_preserves_resources(fixture: Fixture) -> None:
    run_id = fixture.create(RunState.MONITORING_CI_REVIEW)
    orchestrator = fixture.orchestrator()

    first = orchestrator.cancel(run_id)
    second = orchestrator.cancel(run_id)

    assert first.state is second.state is RunState.CANCELLED
    assert fixture.git.remove_worktree_calls == 0
    assert fixture.github.close_pr_calls == 0
    inventory = fixture.root / "runs" / run_id / "reports" / "resource-inventory.md"
    assert inventory.is_file()
    assert f"feature/{run_id}" in inventory.read_text(encoding="utf-8")
    assert SHA in inventory.read_text(encoding="utf-8")


def test_blocked_resume_uses_persisted_from_state(fixture: Fixture) -> None:
    run_id = fixture.create(RunState.LOCAL_VALIDATION)
    fixture.repository.transition(
        run_id,
        RunState.LOCAL_VALIDATION,
        RunState.BLOCKED,
        "validation_blocked",
    )

    outcome = fixture.orchestrator().step(run_id)

    assert outcome.state is RunState.LOCAL_VALIDATION
    assert outcome.action == "resumed_interrupted_state"


def test_blocked_without_source_fails_closed(fixture: Fixture) -> None:
    run_id = fixture.create(RunState.BLOCKED)

    outcome = fixture.orchestrator().step(run_id)

    assert outcome.state is RunState.BLOCKED
    assert "source" in (outcome.blocked_reason or "")


def test_cli_help_lists_all_commands() -> None:
    stdout = StringIO()
    assert main(["--help"], stdout=stdout, stderr=StringIO()) == 0
    output = stdout.getvalue()
    for command in (
        "start",
        "register",
        "approve",
        "status",
        "resume",
        "run",
        "cancel",
        "doctor",
        "tick",
        "release",
    ):
        assert command in output


def test_cli_doctor_without_runtime_is_controlled_json(tmp_path: Path) -> None:
    stdout, stderr = StringIO(), StringIO()

    result = main(
        ["doctor", "--json"],
        services=CliServices(plugin_root=tmp_path),
        stdout=stdout,
        stderr=stderr,
    )

    assert result != 0
    payload = json.loads(stderr.getvalue())
    assert payload["ok"] is False
    assert not (tmp_path / "state.sqlite3").exists()


def test_default_cli_wires_git_and_github_adapters(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_initializations: list[tuple[object, dict[str, object]]] = []
    github_initializations: list[tuple[object, object, dict[str, object]]] = []

    class StubGitAdapter:
        def __init__(self, repo_path: object, **kwargs: object) -> None:
            git_initializations.append((repo_path, kwargs))

    class StubGitHubAdapter:
        def __init__(
            self,
            runner: object,
            project: object,
            **kwargs: object,
        ) -> None:
            github_initializations.append((runner, project, kwargs))

    gh = fixture.root / ("gh.exe" if os.name == "nt" else "gh")
    gh.write_bytes(b"trusted-gh")
    monkeypatch.setenv("CHANGE_DELIVERY_GH_EXECUTABLE", str(gh))
    monkeypatch.setenv(
        "CHANGE_DELIVERY_GH_SHA256",
        hashlib.sha256(gh.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(cli_module, "GitAdapter", StubGitAdapter)
    monkeypatch.setattr(cli_module, "GitHubAdapter", StubGitHubAdapter)
    services = CliServices(
        plugin_root=fixture.root,
        repository=fixture.repository,
        project_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
    )
    orchestrator = services.get_orchestrator()
    project = config(fixture.root)

    git = orchestrator.git_factory(project)
    github = orchestrator.github_factory(project)

    assert isinstance(git, cli_module.RuntimeGitPort)
    assert git_initializations
    assert isinstance(github, StubGitHubAdapter)
    assert github_initializations
    assert github_initializations[0][2]["gh_executable"] == gh.resolve()
    assert orchestrator.stage_executor is not None
    assert orchestrator.worktree_port is not None
    assert orchestrator.commit_port is not None


def test_default_runtime_skills_are_bundled_with_plugin() -> None:
    source_root = Path(cli_module.__file__).resolve().parents[2]
    skills_root = source_root / "plugin" / "change-delivery-plugin" / "skills"
    for name in (
            "delivery-planning",
            "delivery-implement",
            "delivery-commit",
            "delivery-review",
            "delivery-review-fix",
            "project-test-acceptance",
    ):
        assert (skills_root / name / "SKILL.md").is_file()

    globally_existing_names = {
        "writing-plans",
        "subagent-driven-development",
        "verification-before-completion",
        "requesting-code-review",
        "receiving-code-review",
    }
    bundled_names = {
        path.name
        for path in skills_root.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    }
    assert bundled_names == {
        "change-delivery",
            "delivery-planning",
            "delivery-implement",
            "delivery-commit",
            "delivery-review",
            "delivery-review-fix",
            "project-test-acceptance",
        "production-release",
    }
    assert bundled_names.isdisjoint(globally_existing_names)


def test_runtime_local_validation_registers_host_bound_source(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.LOCAL_VALIDATION)
    worktree = fixture.root / "worktrees" / run_id
    worktree.mkdir(parents=True)

    class PassingRunner:
        def run(self, spec: object) -> ProcessResult:
            return ProcessResult(
                command_id="1" * 16,
                exit_code=0,
                stdout="",
                stderr="",
                duration_ms=1,
            )

    executor = RuntimeStageExecutor(
        fixture.repository,
        fixture.root,
        runner=PassingRunner(),  # type: ignore[arg-type]
    )
    run = fixture.repository.get_run(run_id)

    result = executor.execute(
        "local_validation",
        run,
        config(fixture.root),
        fixture.root / "runs" / run_id,
        operation_identity=f"local-validation:{run_id}:nonce",
        tested_tree_sha=TREE_SHA,
        checks_identity="f" * 64,
    )

    evidence = fixture.repository.get_evidence(
        result.evidence[0].evidence_id
    )
    payload = json.loads(evidence.payload_json)
    assert evidence.kind == "local_validation_passed"
    assert payload["operation_identity"].endswith(":nonce")
    assert payload["tested_tree_sha"] == TREE_SHA
    assert payload["checks_identity"] == "f" * 64
    assert payload["commands"] == [
        {"command_id": "1" * 16, "exit_code": 0}
    ]


def test_runtime_codex_stage_replaces_untrusted_model_evidence(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.DRAFT)
    requests: list[object] = []

    class FakeCodex:
        def run_stage(self, request: object) -> StageResult:
            requests.append(request)
            worktree = request.worktree  # type: ignore[attr-defined]
            (worktree / "design.md").write_text("design", encoding="utf-8")
            (worktree / "implementation-plan.md").write_text(
                "plan",
                encoding="utf-8",
            )
            return StageResult(
                status="passed",
                summary="designed",
                evidence=(
                    EvidenceRef(
                        evidence_id="model-invented",
                        kind="model_invented",
                        sha256="f" * 64,
                    ),
                ),
                findings=(),
                next_action="approve",
            )

    executor = RuntimeStageExecutor(
        fixture.repository,
        fixture.root,
        codex_factory=lambda: FakeCodex(),
    )
    run = fixture.repository.get_run(run_id)

    result = executor.execute(
        "design",
        run,
        config(fixture.root),
        fixture.root / "runs" / run_id,
    )

    assert result.evidence[0].evidence_id != "model-invented"
    evidence = fixture.repository.get_evidence(
        result.evidence[0].evidence_id
    )
    assert evidence.kind == "design_passed"
    assert json.loads(evidence.payload_json)["artifact_hashes"]
    assert requests[0].expected_skill == "delivery-planning"  # type: ignore[attr-defined]
    with pytest.raises(KeyError):
        fixture.repository.get_evidence("model-invented")


@pytest.mark.parametrize(
    ("stage", "expected_skill", "expected_role"),
    [
        ("implementation", "delivery-implement", "writer"),
        ("internal_review", "delivery-review", "read_only"),
        ("review_fix", "delivery-review-fix", "writer"),
        (
            "acceptance",
            "project-test-acceptance",
            "writer",
        ),
    ],
)
def test_runtime_routes_new_explicit_skills_with_authoritative_schema(
    fixture: Fixture,
    stage: str,
    expected_skill: str,
    expected_role: str,
) -> None:
    run_id, _ = approve_fixture_run(fixture)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET state = ? WHERE run_id = ?",
            (RunState.IMPLEMENTING.value, run_id),
        )
    if stage == "review_fix":
        with fixture.repository.database.transaction() as connection:
            connection.execute(
                "UPDATE runs SET state = ?, pr_number = 42 WHERE run_id = ?",
                (RunState.MONITORING_CI_REVIEW.value, run_id),
            )
        fixture.github.snapshot_result = Snapshot(
            SHA,
            review_status="changes_requested",
            requested_reviews=(REQUESTED_REVIEW,),
        )
        fixture.orchestrator().step(run_id)
    worktree = fixture.root / "worktrees" / run_id
    worktree.mkdir(parents=True)
    captured: list[object] = []

    class FakeCodex:
        def run_stage(self, request: object) -> StageResult:
            captured.append(request)
            return StageResult(
                status="blocked",
                summary="bounded handoff",
                evidence=(),
                findings=(),
                next_action="host decides",
            )

    executor = RuntimeStageExecutor(
        fixture.repository,
        fixture.root,
        codex_factory=lambda: FakeCodex(),
    )

    executor.execute(
        stage,
        fixture.repository.get_run(run_id),
        config(fixture.root),
        fixture.root / "runs" / run_id,
    )

    request = captured[0]
    assert request.expected_skill == expected_skill  # type: ignore[attr-defined]
    assert request.role == expected_role  # type: ignore[attr-defined]
    assert json.loads(  # type: ignore[attr-defined]
        request.result_schema_json
    ) == StageResult.model_json_schema()
    assert request.status_policy  # type: ignore[attr-defined]
    prompt = CodexAdapter._render_prompt(request)  # type: ignore[arg-type]
    assert f"${expected_skill}" in prompt
    assert '"EvidenceRef"' in prompt
    assert '"Finding"' in prompt
    if stage == "acceptance":
        assert "ENVIRONMENT_PREEMPTED=blocked" in prompt

    source_root = Path(cli_module.__file__).resolve().parents[2]
    registry = executor._skill_registry(source_root)
    assert registry[expected_skill] == (
        source_root
        / "plugin"
        / "change-delivery-plugin"
        / "skills"
        / expected_skill
    ).resolve()


def test_runtime_acceptance_port_preserves_blocked_stage_as_nonpassing_fact(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.ACCEPTANCE_TESTING)
    run = fixture.repository.get_run(run_id)

    class BlockedAcceptanceStage:
        def execute(
            self,
            stage: str,
            current_run: object,
            project: object,
            run_root: Path,
        ) -> StageResult:
            del project, run_root
            assert stage == "acceptance"
            reference = fixture.repository.register_evidence(
                run_id,
                f"codex-stage:{run_id}:acceptance:test",
                "acceptance_result",
                {
                    "sha": current_run.head_sha,  # type: ignore[attr-defined]
                    "passed": False,
                    "stage": "acceptance",
                },
            )
            return StageResult(
                status="blocked",
                summary="ENVIRONMENT_PREEMPTED",
                evidence=(reference,),
                findings=(),
                next_action="reconcile_environment",
            )

    port = RuntimeAcceptancePort(
        fixture.repository,
        fixture.root,
        BlockedAcceptanceStage(),  # type: ignore[arg-type]
    )

    configured = config(fixture.root).model_copy(
        update={
            "acceptance_check": CommandSpec(
                executable=sys.executable,
                args=("-c", "raise SystemExit(1)"),
                workdir=fixture.root,
            )
        }
    )
    result = port.execute(run, configured)

    assert result["passed"] is False
    assert result["stage_status"] == "blocked"
    assert port.query(run, configured) is None


def test_runtime_test_environment_uses_configured_force_strategy_and_records_it(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.DEPLOYING_TEST)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET project_id = ? WHERE run_id = ?",
            ("example-backend", run_id),
        )
    calls: list[tuple[object, ...]] = []

    class ForceGit:
        current = OTHER_SHA

        def fetch(self, remote: str) -> None:
            calls.append(("fetch", remote))

        def remote_sha(self, remote: str, branch: str) -> str:
            calls.append(("remote_sha", remote, branch))
            return self.current

        def push_test(
            self,
            remote: str,
            branch: str,
            source_sha: str,
            observed_sha: str,
            *,
            strategy: str,
        ) -> str:
            calls.append(
                (
                    "push_test",
                    remote,
                    branch,
                    source_sha,
                    observed_sha,
                    strategy,
                )
            )
            self.current = source_sha
            return source_sha

    force_git = ForceGit()

    class VersionHttp:
        def get_json(self, url: str) -> object:
            return {"sha": force_git.current}

    configured = config(fixture.root).model_copy(
        update={
            "project_id": "example-backend",
            "version_endpoint": "https://api.test.example.invalid/health",
            "test_push_strategy": "force",
            "acceptance_check": CommandSpec(
                executable=sys.executable,
                args=("-c", "host-owned-acceptance"),
                workdir=fixture.root,
            ),
        }
    )
    port = RuntimeTestEnvironmentPort(
        fixture.repository,
        git_factory=lambda project: force_git,
        http_factory=lambda project: VersionHttp(),
    )

    result = port.execute(fixture.repository.get_run(run_id), configured)

    assert result["passed"] is True
    assert (
        "push_test",
        "origin",
        "test",
        SHA,
        OTHER_SHA,
        "force",
    ) in calls
    evidence = fixture.repository.get_evidence(
        f"test-branch-pushed:{run_id}:{SHA}"
    )
    payload = json.loads(evidence.payload_json)
    assert payload["push_strategy"] == "force"


def test_default_runtime_ports_deploy_with_exact_lease_then_require_host_acceptance(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.DEPLOYING_TEST)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET project_id = ? WHERE run_id = ?",
            ("example-backend", run_id),
        )
    old_sha = "b" * 40
    calls: list[tuple[object, ...]] = []

    class DeployGit:
        current = old_sha

        def fetch(self, remote: str) -> None:
            calls.append(("fetch", remote))

        def remote_sha(self, remote: str, branch: str) -> str:
            calls.append(("remote_sha", remote, branch))
            return self.current

        def push_test_with_lease(
            self,
            remote: str,
            branch: str,
            source_sha: str,
            observed_sha: str,
        ) -> str:
            calls.append(
                ("push_test_with_lease", remote, branch, source_sha, observed_sha)
            )
            assert observed_sha == old_sha
            self.current = source_sha
            return source_sha

    deploy_git = DeployGit()

    class VersionHttp:
        def get_json(self, url: str) -> object:
            calls.append(("GET", url))
            return {"sha": deploy_git.current}

    command = CommandSpec(
        executable=sys.executable,
        args=("-c", "host-owned-acceptance"),
        workdir=fixture.root,
        timeout_seconds=30,
    )
    configured = config(fixture.root).model_copy(
        update={
            "project_id": "example-backend",
            "version_endpoint": "https://api.test.example.invalid/health",
            "acceptance_check": command,
            "test_deploy_timeout_seconds": 5,
            "test_deploy_poll_interval_seconds": 1.0,
        }
    )
    deploy = RuntimeTestEnvironmentPort(
        fixture.repository,
        git_factory=lambda project: deploy_git,
        http_factory=lambda project: VersionHttp(),
    )

    class AdvisoryStage:
        def execute(
            self,
            stage: str,
            run: object,
            project: object,
            run_root: Path,
        ) -> StageResult:
            del project, run_root
            assert stage == "acceptance"
            reference = fixture.repository.register_evidence(
                run_id,
                f"acceptance-advisory:{run_id}",
                "acceptance_advisory_passed",
                {"sha": run.head_sha, "passed": True, "stage": "acceptance"},  # type: ignore[attr-defined]
            )
            return StageResult(
                status="passed",
                summary="advisory checks complete",
                evidence=(reference,),
                findings=(),
                next_action="run host authority",
            )

    class AcceptanceRunner:
        stdin: list[str] = []

        def run(
            self,
            spec: CommandSpec,
            *,
            stdin_text: str | None = None,
        ) -> ProcessResult:
            assert spec == command
            assert stdin_text is not None
            self.stdin.append(stdin_text)
            return ProcessResult(
                command_id="1" * 16,
                exit_code=0,
                stdout=json.dumps(
                    {
                        "target_sha": SHA,
                        "deployed_sha": SHA,
                        "assertions_passed": True,
                        "resource_ids": [f"codex-{run_id}--message-1"],
                        "cleanup_confirmed": True,
                    }
                ),
                stderr="",
                duration_ms=1,
            )

    authority = AcceptanceRunner()
    acceptance = RuntimeAcceptancePort(
        fixture.repository,
        fixture.root,
        AdvisoryStage(),  # type: ignore[arg-type]
        runner=authority,  # type: ignore[arg-type]
    )
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (configured, CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        test_environment=deploy,
        acceptance_executor=acceptance,
    )

    assert orchestrator.step(run_id).state is RunState.ACCEPTANCE_TESTING
    assert orchestrator.step(run_id).state is RunState.PR_READY_FOR_REVIEW
    assert (
        "push_test_with_lease",
        "origin",
        "test",
        SHA,
        old_sha,
    ) in calls
    assert len(authority.stdin) == 1
    context = json.loads(authority.stdin[0])
    assert context["target_sha"] == SHA
    assert context["run_id"] == run_id
    assert command.args == ("-c", "host-owned-acceptance")
    evidence = fixture.repository.get_evidence(
        f"acceptance_passed:{run_id}:{SHA}"
    )
    payload = json.loads(evidence.payload_json)
    assert payload["command_sha256"]
    assert payload["stdout_sha256"]


@pytest.mark.parametrize(
    "updates, missing",
    [
        ({"version_endpoint": ""}, "version_endpoint"),
        ({"acceptance_check": None}, "acceptance_check"),
    ],
)
def test_deploy_blocks_before_git_io_when_required_config_is_missing(
    fixture: Fixture,
    updates: dict[str, object],
    missing: str,
) -> None:
    run_id = fixture.create(RunState.DEPLOYING_TEST)
    calls: list[str] = []

    class NoIoGit:
        def fetch(self, remote: str) -> None:
            calls.append(f"fetch:{remote}")

        def remote_sha(self, remote: str, branch: str) -> str:
            calls.append(f"remote_sha:{remote}:{branch}")
            return OTHER_SHA

        def push_test_with_lease(self, remote: str, branch: str, source_sha: str, observed_sha: str) -> str:
            calls.append(f"push:{remote}:{branch}:{source_sha}:{observed_sha}")
            return source_sha

    configured = config(fixture.root).model_copy(
        update={
            "acceptance_check": CommandSpec(
                executable=sys.executable,
                args=("-c", "raise SystemExit(0)"),
                workdir=fixture.root,
            ),
            **updates,
        }
    )
    port = RuntimeTestEnvironmentPort(
        fixture.repository,
        git_factory=lambda project: NoIoGit(),
    )

    result = port.execute(fixture.repository.get_run(run_id), configured)

    assert result == {
        "sha": SHA,
        "passed": False,
        "reason": "DEPLOYMENT_CONFIG_INCOMPLETE",
        "missing_fields": [missing],
    }
    assert calls == []


def test_cli_release_is_disabled_and_has_no_side_effect(tmp_path: Path) -> None:
    before = tuple(tmp_path.iterdir())
    stdout, stderr = StringIO(), StringIO()

    result = main(
        ["release", "run-1", "--json"],
        services=CliServices(plugin_root=tmp_path),
        stdout=stdout,
        stderr=stderr,
    )

    assert result != 0
    assert "runtime database" in json.loads(stderr.getvalue())["error"]
    assert tuple(tmp_path.iterdir()) == before


def test_cli_register_hash_mismatch_and_failed_doctor_do_not_write(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate.yaml"
    candidate.write_text(
        f"""
project_id: test-project
repo_path: {tmp_path.as_posix()}
remote: origin
remote_fetch_url: https://example.invalid/repo.git
remote_push_url: https://example.invalid/repo.git
base_branch: develop
test_branch: test
github_repository: owner/repo
version_endpoint: https://example.invalid/version
version_sha_field: sha
required_pre_pr_checks:
  - executable: python
    args: [-m, pytest]
    workdir: {tmp_path.as_posix()}
""",
        encoding="utf-8",
    )
    services = CliServices(
        plugin_root=tmp_path / "runtime",
        registration_verifier=lambda loaded: (False, "doctor blocked"),
    )
    for digest in ("0" * 64, config(tmp_path).model_dump_json()):
        stdout, stderr = StringIO(), StringIO()
        result = main(
            [
                "register",
                "--candidate-file",
                str(candidate),
                "--confirm-hash",
                digest,
                "--json",
            ],
            services=services,
            stdout=stdout,
            stderr=stderr,
        )
        assert result != 0
    assert not (services.plugin_root / "projects").exists()


def test_cli_register_success_is_atomic(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.yaml"
    candidate.write_text(
        f"""
project_id: test-project
repo_path: {tmp_path.as_posix()}
remote: origin
remote_fetch_url: https://example.invalid/repo.git
remote_push_url: https://example.invalid/repo.git
base_branch: develop
test_branch: test
github_repository: owner/repo
version_endpoint: https://example.invalid/version
version_sha_field: sha
required_pre_pr_checks:
  - executable: python
    args: [-m, pytest]
    workdir: {tmp_path.as_posix()}
""",
        encoding="utf-8",
    )
    loaded = load_project_config(candidate)
    services = CliServices(
        plugin_root=tmp_path / "runtime",
        registration_verifier=lambda candidate: (True, "verified"),
    )

    result = main(
        [
            "register",
            "--candidate-file",
            str(candidate),
            "--confirm-hash",
            loaded.config_hash,
            "--json",
        ],
        services=services,
        stdout=StringIO(),
        stderr=StringIO(),
    )

    assert result == 0
    registered = services.plugin_root / "projects" / "test-project.yaml"
    assert registered.read_bytes() == candidate.read_bytes()
    assert not list(registered.parent.glob("*.tmp"))


@pytest.mark.parametrize(
    "argv",
    [
        ["start", "--project", "p", "--requirement-file", "missing", "--json"],
        [
            "register",
            "--candidate-file",
            "missing",
            "--confirm-hash",
            "0" * 64,
            "--json",
        ],
        ["approve", "run", "--approval-file", "missing", "--json"],
        ["status", "--json"],
        ["resume", "run", "--json"],
        ["run", "run", "--json"],
        ["cancel", "run", "--json"],
        ["doctor", "--json"],
        ["tick", "--json"],
        ["release", "run", "--json"],
    ],
)
def test_every_command_accepts_subcommand_json_and_errors_stably(
    tmp_path: Path,
    argv: list[str],
) -> None:
    stdout, stderr = StringIO(), StringIO()
    code = main(
        argv,
        services=CliServices(plugin_root=tmp_path),
        stdout=stdout,
        stderr=stderr,
    )
    assert code != 0
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue())["ok"] is False


def test_global_json_position_is_supported(tmp_path: Path) -> None:
    stderr = StringIO()
    code = main(
        ["--json", "release", "run"],
        services=CliServices(plugin_root=tmp_path),
        stdout=StringIO(),
        stderr=stderr,
    )
    assert code != 0
    assert json.loads(stderr.getvalue())["ok"] is False


def test_register_blocks_candidate_changed_during_doctor(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.yaml"
    original = f"""
project_id: test-project
repo_path: {tmp_path.as_posix()}
remote: origin
remote_fetch_url: https://example.invalid/repo.git
remote_push_url: https://example.invalid/repo.git
base_branch: develop
test_branch: test
github_repository: owner/repo
version_endpoint: https://example.invalid/version
version_sha_field: sha
required_pre_pr_checks:
  - executable: python
    args: [-m, pytest]
    workdir: {tmp_path.as_posix()}
"""
    candidate.write_text(original, encoding="utf-8")
    loaded = load_project_config(candidate)

    def aba_verifier(config: object) -> tuple[bool, str]:
        candidate.write_text("changed", encoding="utf-8")
        candidate.write_text(original, encoding="utf-8")
        return True, "verified"

    services = CliServices(
        plugin_root=tmp_path / "runtime",
        registration_verifier=aba_verifier,
    )
    code = main(
        [
            "register",
            "--candidate-file",
            str(candidate),
            "--confirm-hash",
            loaded.config_hash,
            "--json",
        ],
        services=services,
        stdout=StringIO(),
        stderr=StringIO(),
    )
    assert code != 0
    assert not (services.plugin_root / "projects").exists()


@pytest.mark.parametrize("state", list(RunState))
def test_every_run_state_has_an_explicit_step_outcome(
    fixture: Fixture,
    state: RunState,
) -> None:
    run_id = fixture.create(state)
    outcome = fixture.orchestrator().step(run_id)
    assert outcome.action != "noop_blocked"
    assert "handler not implemented" not in (outcome.blocked_reason or "")


def test_draft_calls_design_stage_once_before_awaiting(fixture: Fixture) -> None:
    run_id = fixture.create(RunState.DRAFT)
    executor = FakeStageExecutor(fixture.repository)
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        stage_executor=executor,
    )

    outcome = orchestrator.step(run_id)

    assert outcome.state is RunState.AWAITING_DESIGN_APPROVAL
    assert executor.calls == ["design"]


def test_run_advances_until_design_approval(fixture: Fixture) -> None:
    run_id = fixture.create(RunState.DRAFT)
    executor = FakeStageExecutor(fixture.repository)
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        stage_executor=executor,
    )

    outcome = orchestrator.run(run_id, max_steps=32)

    assert outcome.state is RunState.AWAITING_DESIGN_APPROVAL
    assert outcome.action == "design_artifacts_ready"
    assert outcome.steps_processed == 1
    assert outcome.step_actions == ("design_artifacts_ready",)


def test_run_stops_at_external_wait_without_busy_loop(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.MONITORING_CI_REVIEW)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET pr_number = 42 WHERE run_id = ?",
            (run_id,),
        )
    fixture.github.snapshot_result = Snapshot(SHA)

    outcome = fixture.orchestrator().run(run_id, max_steps=32)

    assert outcome.state is RunState.MONITORING_CI_REVIEW
    assert outcome.steps_processed == 1
    assert outcome.step_actions == (outcome.action,)


def test_run_has_hard_step_limit(fixture: Fixture) -> None:
    run_id = fixture.create(RunState.APPROVED)

    outcome = fixture.orchestrator().run(run_id, max_steps=1)

    assert outcome.state is RunState.PREPARING_WORKTREE
    assert outcome.action == "step_limit_reached"
    assert outcome.steps_processed == 1
    assert outcome.step_actions == ("approval_scope_accepted",)
    assert "1" in (outcome.blocked_reason or "")


def test_draft_empty_design_does_not_advance(fixture: Fixture) -> None:
    run_id = fixture.create(RunState.DRAFT)
    (fixture.root / "runs" / run_id / "design.md").write_bytes(b"")
    executor = FakeStageExecutor(fixture.repository)
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        stage_executor=executor,
    )

    outcome = orchestrator.step(run_id)

    assert outcome.state is RunState.DRAFT
    assert executor.calls == ["design"]


def test_default_services_support_resume_cancel_and_tick(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    write_registered_project(root)
    services = CliServices(plugin_root=root)
    repository = services.get_repository(create=True)
    for run_id in ("resume-run", "cancel-run"):
        repository.create_run(run_id, "test-project", load_project_config(
            root / "projects" / "test-project.yaml"
        ).config_hash)
        run_root = root / "runs" / run_id
        run_root.mkdir(parents=True)
        for name in ("requirement.md", "design.md", "implementation-plan.md"):
            (run_root / name).write_text(name, encoding="utf-8")

    stdout, stderr = StringIO(), StringIO()
    assert main(
        ["resume", "resume-run", "--json"],
        services=services,
        stdout=stdout,
        stderr=stderr,
    ) == 0
    assert json.loads(stdout.getvalue())["action"] != "blocked_dependency"

    stdout = StringIO()
    assert main(
        ["cancel", "cancel-run", "--json"],
        services=services,
        stdout=stdout,
        stderr=stderr,
    ) == 0
    assert json.loads(stdout.getvalue())["state"] == "CANCELLED"

    stdout = StringIO()
    assert main(
        ["tick", "--json"],
        services=services,
        stdout=stdout,
        stderr=stderr,
    ) == 0
    assert json.loads(stdout.getvalue())["processed"] == 1


@pytest.mark.parametrize(
    ("state", "stage", "target"),
    [
        (RunState.IMPLEMENTING, "implementation", RunState.LOCAL_VALIDATION),
        (RunState.LOCAL_VALIDATION, "local_validation", RunState.INTERNAL_REVIEW),
        (RunState.FIXING_CI_REVIEW, "review_fix", RunState.LOCAL_VALIDATION),
    ],
)
def test_stage_handlers_call_once_rebind_and_advance(
    fixture: Fixture,
    state: RunState,
    stage: str,
    target: RunState,
) -> None:
    run_id = fixture.create(state)
    executor = FakeStageExecutor(fixture.repository)
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        stage_executor=executor,
        commit_port=FakeCommitPort() if stage == "local_validation" else None,
    )

    outcome = orchestrator.step(run_id)

    assert outcome.state is target
    assert executor.calls == [stage]


def test_local_validation_rejects_tree_change_during_stage(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.LOCAL_VALIDATION)
    executor = FakeStageExecutor(fixture.repository)
    commit = FakeCommitPort()
    trees = iter((TREE_SHA, OTHER_TREE_SHA))
    commit.validation_tree = lambda run, project: next(trees)  # type: ignore[method-assign]
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        stage_executor=executor,
        commit_port=commit,
    )

    outcome = orchestrator.step(run_id)

    assert outcome.state is RunState.LOCAL_VALIDATION
    assert "during local validation" in (outcome.blocked_reason or "")
    assert not any(
        evidence.kind == "local_validation_attestation"
        for evidence in fixture.repository.list_evidence(run_id)
    )


def test_local_validation_rejects_stale_source_evidence_replay(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.LOCAL_VALIDATION)
    stale = fixture.repository.register_evidence(
        run_id,
        f"stale-validation:{run_id}",
        "local_validation_passed",
        {
            "sha": SHA,
            "passed": True,
            "operation_identity": "validation:stale",
            "tested_tree_sha": TREE_SHA,
            "checks_identity": "f" * 64,
        },
    )

    class ReplayExecutor:
        def execute(self, *args: object, **kwargs: object) -> StageResult:
            return StageResult(
                status="passed",
                summary="replayed",
                evidence=(stale,),
                findings=(),
                next_action="continue",
            )

    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        stage_executor=ReplayExecutor(),
        commit_port=FakeCommitPort(),
    )

    outcome = orchestrator.step(run_id)

    assert outcome.state is RunState.LOCAL_VALIDATION
    assert "source evidence" in (outcome.blocked_reason or "")
    assert not any(
        evidence.kind == "local_validation_attestation"
        for evidence in fixture.repository.list_evidence(run_id)
    )


def test_failed_local_validation_returns_to_implementation(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.LOCAL_VALIDATION)
    executor = FakeStageExecutor(fixture.repository, status="failed")
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        stage_executor=executor,
        commit_port=FakeCommitPort(),
    )

    outcome = orchestrator.step(run_id)

    assert outcome.state is RunState.IMPLEMENTING
    assert fixture.repository.get_run(run_id).state is RunState.IMPLEMENTING


def test_internal_review_rejects_validation_attestation_for_old_tree(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.LOCAL_VALIDATION)
    executor = FakeStageExecutor(fixture.repository)
    commit = FakeCommitPort()
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        stage_executor=executor,
        commit_port=commit,
    )
    assert orchestrator.step(run_id).state is RunState.INTERNAL_REVIEW
    commit.tree_sha = OTHER_TREE_SHA

    outcome = orchestrator.step(run_id)

    assert outcome.state is RunState.INTERNAL_REVIEW
    assert "changed after local validation" in (outcome.blocked_reason or "")
    assert executor.calls == ["local_validation"]


def test_internal_review_records_tree_bound_attestation(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.LOCAL_VALIDATION)
    executor = FakeStageExecutor(fixture.repository)
    commit = FakeCommitPort()
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        stage_executor=executor,
        commit_port=commit,
    )

    assert orchestrator.step(run_id).state is RunState.INTERNAL_REVIEW
    outcome = orchestrator.step(run_id)

    assert outcome.state is RunState.COMMITTING
    assert commit.review_tree_calls == 1
    attestations = [
        evidence
        for evidence in fixture.repository.list_evidence(run_id)
        if evidence.kind == "internal_review_attestation"
    ]
    assert len(attestations) == 1
    payload = json.loads(attestations[0].payload_json)
    assert payload["reviewed_tree_sha"] == TREE_SHA
    assert payload["source_evidence_ids"] == [f"internal_review-proof:{run_id}"]


def test_failed_internal_review_enters_bounded_fix_stage(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.LOCAL_VALIDATION)
    finding = Finding(
        severity="important",
        title="缺少回归测试",
        evidence="准确代码树缺少失败路径覆盖",
        actionable=True,
    )
    executor = FakeStageExecutor(fixture.repository)
    commit = FakeCommitPort()
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        stage_executor=executor,
        commit_port=commit,
    )
    assert orchestrator.step(run_id).state is RunState.INTERNAL_REVIEW
    executor.status = "failed"
    executor.findings = (finding,)

    outcome = orchestrator.step(run_id)

    assert outcome.state is RunState.FIXING_INTERNAL_REVIEW
    requests = [
        evidence
        for evidence in fixture.repository.list_evidence(run_id)
        if evidence.kind == "internal_review_fix_request"
    ]
    assert len(requests) == 1
    payload = json.loads(requests[0].payload_json)
    assert payload == {
        "attempt": 1,
        "expected_parent_sha": SHA,
        "findings": [finding.model_dump(mode="json")],
        "reviewed_tree_sha": TREE_SHA,
    }


def test_internal_review_fix_returns_to_validation_then_review(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.LOCAL_VALIDATION)
    finding = Finding(
        severity="important",
        title="修复边界条件",
        evidence="边界输入会返回错误结果",
        actionable=True,
    )
    executor = FakeStageExecutor(fixture.repository)
    commit = FakeCommitPort()
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        stage_executor=executor,
        commit_port=commit,
    )
    assert orchestrator.step(run_id).state is RunState.INTERNAL_REVIEW
    executor.status = "failed"
    executor.findings = (finding,)
    assert orchestrator.step(run_id).state is RunState.FIXING_INTERNAL_REVIEW
    executor.status = "passed"
    executor.findings = ()

    assert orchestrator.step(run_id).state is RunState.LOCAL_VALIDATION
    assert orchestrator.step(run_id).state is RunState.INTERNAL_REVIEW
    assert orchestrator.step(run_id).state is RunState.COMMITTING
    assert executor.calls == [
        "local_validation",
        "internal_review",
        "internal_review_fix",
        "local_validation",
        "internal_review",
    ]


def test_runtime_internal_review_fix_receives_only_bound_feedback(
    fixture: Fixture,
) -> None:
    run_id, _ = approve_fixture_run(fixture)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET state = ? WHERE run_id = ?",
            (RunState.LOCAL_VALIDATION.value, run_id),
        )
    finding = Finding(
        severity="important",
        title="补充失败路径测试",
        evidence="当前准确树未覆盖失败路径",
        actionable=True,
    )
    stage = FakeStageExecutor(fixture.repository)
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        stage_executor=stage,
        commit_port=FakeCommitPort(),
    )
    assert orchestrator.step(run_id).state is RunState.INTERNAL_REVIEW
    stage.status = "failed"
    stage.findings = (finding,)
    assert orchestrator.step(run_id).state is RunState.FIXING_INTERNAL_REVIEW
    worktree = fixture.root / "worktrees" / run_id
    worktree.mkdir(parents=True)
    captured: list[object] = []

    class FakeCodex:
        def run_stage(self, request: object) -> StageResult:
            captured.append(request)
            return StageResult(
                status="blocked",
                summary="bounded handoff",
                evidence=(),
                findings=(),
                next_action="host decides",
            )

    runtime = RuntimeStageExecutor(
        fixture.repository,
        fixture.root,
        codex_factory=lambda: FakeCodex(),
    )
    runtime.execute(
        "internal_review_fix",
        fixture.repository.get_run(run_id),
        config(fixture.root),
        fixture.root / "runs" / run_id,
    )

    request = captured[0]
    assert request.expected_skill == "delivery-review-fix"  # type: ignore[attr-defined]
    scope = json.loads(request.approval_scope)  # type: ignore[attr-defined]
    assert scope["actionable_feedback"] == {
        "attempt": 1,
        "expected_parent_sha": SHA,
        "findings": [finding.model_dump(mode="json")],
        "reviewed_tree_sha": TREE_SHA,
    }
    assert set(scope) == {"approved_artifacts", "actionable_feedback"}


def test_internal_review_fourth_fix_requires_user_decision(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.LOCAL_VALIDATION)
    finding = Finding(
        severity="critical",
        title="重复问题",
        evidence="三轮修改后问题仍然存在",
        actionable=True,
    )
    executor = FakeStageExecutor(fixture.repository)
    commit = FakeCommitPort()
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        stage_executor=executor,
        commit_port=commit,
    )
    assert orchestrator.step(run_id).state is RunState.INTERNAL_REVIEW
    for _ in range(3):
        executor.status = "failed"
        executor.findings = (finding,)
        assert orchestrator.step(run_id).state is RunState.FIXING_INTERNAL_REVIEW
        executor.status = "passed"
        executor.findings = ()
        assert orchestrator.step(run_id).state is RunState.LOCAL_VALIDATION
        assert orchestrator.step(run_id).state is RunState.INTERNAL_REVIEW

    executor.status = "failed"
    executor.findings = (finding,)
    stopped = orchestrator.step(run_id)

    assert stopped.state is RunState.REVIEW_DECISION_REQUIRED
    assert stopped.action == "internal_review_fix_limit_reached"
    assert len(
        [
            evidence
            for evidence in fixture.repository.list_evidence(run_id)
            if evidence.kind == "internal_review_fix_request"
        ]
    ) == 3


def test_commit_derives_review_evidence_for_new_head(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.LOCAL_VALIDATION)
    executor = FakeStageExecutor(fixture.repository)
    commit = FakeCommitPort()
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        stage_executor=executor,
        commit_port=commit,
    )
    assert orchestrator.step(run_id).state is RunState.INTERNAL_REVIEW
    assert orchestrator.step(run_id).state is RunState.COMMITTING

    outcome = orchestrator.step(run_id)

    assert outcome.state is RunState.PUSHING_FEATURE
    assert fixture.repository.get_run(run_id).head_sha == NEW_SHA
    derived = [
        evidence
        for evidence in fixture.repository.list_evidence(run_id)
        if evidence.kind == "internal_review_passed"
    ]
    assert len(derived) == 1
    payload = json.loads(derived[0].payload_json)
    assert payload["sha"] == NEW_SHA
    assert payload["tree_sha"] == TREE_SHA
    assert payload["passed"] is True


def test_commit_message_is_generated_bound_and_passed_to_commit_port(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.LOCAL_VALIDATION)
    executor = FakeStageExecutor(fixture.repository)
    commit = FakeCommitPort()
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        stage_executor=executor,
        commit_port=commit,
    )
    assert orchestrator.step(run_id).state is RunState.INTERNAL_REVIEW
    assert orchestrator.step(run_id).state is RunState.COMMITTING

    assert orchestrator.step(run_id).state is RunState.PUSHING_FEATURE

    expected_message = (
        "feat(delivery): 使用阶段 Skill\n\n"
        f"Change-Delivery-Run: {run_id}"
    )
    assert commit.messages == [expected_message, expected_message]
    assert executor.commit_draft_calls == 1
    generated = [
        evidence
        for evidence in fixture.repository.list_evidence(run_id)
        if evidence.kind == "commit_message_generated"
    ]
    assert len(generated) == 1
    payload = json.loads(generated[0].payload_json)
    assert payload == {
        "expected_parent_sha": SHA,
        "message": expected_message,
        "message_sha256": hashlib.sha256(
            expected_message.encode("utf-8")
        ).hexdigest(),
        "reviewed_tree_sha": TREE_SHA,
        "skill_sha256": "f" * 64,
    }


def test_runtime_commit_message_generator_binds_skill_and_tree(
    fixture: Fixture,
) -> None:
    run_id, _ = approve_fixture_run(fixture)
    worktree = fixture.root / "worktrees" / run_id
    worktree.mkdir(parents=True)
    captured: list[object] = []

    class FakeCodex:
        def run_stage(self, request: object) -> StageResult:
            raise AssertionError("stage execution is not expected")

        def generate_commit_draft(self, request: object) -> CommitDraft:
            captured.append(request)
            return CommitDraft(message="feat: 自定义提交")

    runtime = RuntimeStageExecutor(
        fixture.repository,
        fixture.root,
        codex_factory=lambda: FakeCodex(),
    )

    draft, skill_sha256 = runtime.generate_commit_draft(
        fixture.repository.get_run(run_id),
        config(fixture.root),
        TREE_SHA,
        SHA,
    )

    assert draft.message == "feat: 自定义提交"
    request = captured[0]
    assert request.expected_skill == "delivery-commit"  # type: ignore[attr-defined]
    scope = json.loads(request.approval_scope)  # type: ignore[attr-defined]
    assert scope["reviewed_tree_sha"] == TREE_SHA
    assert scope["expected_parent_sha"] == SHA
    assert set(scope["approved_artifacts"]) == {
        "design.md",
        "implementation-plan.md",
        "requirement.md",
    }
    assert re.fullmatch(r"[0-9a-f]{64}", skill_sha256)


def test_commit_message_is_reused_after_uncertain_execution(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.LOCAL_VALIDATION)
    executor = FakeStageExecutor(fixture.repository)

    class UncertainCommit(FakeCommitPort):
        def execute(
            self,
            run: object,
            project: object,
            message: str,
        ) -> dict[str, object]:
            self.messages.append(message)
            raise RuntimeError("uncertain")

    commit = UncertainCommit()
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        stage_executor=executor,
        commit_port=commit,
    )
    assert orchestrator.step(run_id).state is RunState.INTERNAL_REVIEW
    assert orchestrator.step(run_id).state is RunState.COMMITTING
    assert orchestrator.step(run_id).action == "commit_reconciliation_pending"

    restarted = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        stage_executor=executor,
        commit_port=commit,
    )
    assert restarted.step(run_id).action == "commit_reconciliation_pending"
    assert executor.commit_draft_calls == 1


def test_commit_rejects_validation_attestation_without_validation_event(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.LOCAL_VALIDATION)
    executor = FakeStageExecutor(fixture.repository)
    commit = FakeCommitPort()
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        stage_executor=executor,
        commit_port=commit,
    )
    assert orchestrator.step(run_id).state is RunState.INTERNAL_REVIEW
    assert orchestrator.step(run_id).state is RunState.COMMITTING
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            """
            DELETE FROM events
            WHERE run_id = ? AND from_state = ? AND to_state = ?
            """,
            (
                run_id,
                RunState.LOCAL_VALIDATION.value,
                RunState.INTERNAL_REVIEW.value,
            ),
        )

    outcome = orchestrator.step(run_id)

    assert outcome.state is RunState.COMMITTING
    assert fixture.repository.get_run(run_id).head_sha == SHA
    assert not any(
        evidence.kind == "internal_review_passed"
        for evidence in fixture.repository.list_evidence(run_id)
    )


@pytest.mark.parametrize("pending", [False, True])
def test_commit_tree_mismatch_never_derives_review_pass(
    fixture: Fixture,
    pending: bool,
) -> None:
    run_id = fixture.create(RunState.LOCAL_VALIDATION)
    executor = FakeStageExecutor(fixture.repository)
    commit = FakeCommitPort(commit_tree_sha=OTHER_TREE_SHA)
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        stage_executor=executor,
        commit_port=commit,
    )
    assert orchestrator.step(run_id).state is RunState.INTERNAL_REVIEW
    assert orchestrator.step(run_id).state is RunState.COMMITTING
    if pending:
        message = (
            "feat(delivery): 使用阶段 Skill\n\n"
            f"Change-Delivery-Run: {run_id}"
        )
        commit.observed = commit.execute(
            fixture.repository.get_run(run_id),
            config(fixture.root),
            message,
        )
        commit.execute_calls = 0
        message_sha256 = hashlib.sha256(message.encode("utf-8")).hexdigest()
        fixture.repository.begin_remote_operation(
            f"commit:{run_id}:{TREE_SHA}:{SHA}:{message_sha256}",
            run_id,
            "commit",
            f"test-project:{TREE_SHA}:{SHA}:{message_sha256}",
        )

    outcome = orchestrator.step(run_id)

    assert outcome.state is RunState.COMMITTING
    assert "tree" in (outcome.blocked_reason or "")
    assert not any(
        evidence.kind == "internal_review_passed"
        for evidence in fixture.repository.list_evidence(run_id)
    )
    if pending:
        assert commit.execute_calls == 0


def test_forged_review_attestation_source_is_rejected(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.COMMITTING)
    fixture.repository.register_evidence(
        run_id,
        f"review-attestation:{run_id}:{TREE_SHA}",
        "internal_review_attestation",
        {
            "passed": True,
            "reviewed_tree_sha": TREE_SHA,
            "expected_parent_sha": SHA,
            "source_evidence_ids": ["missing-source"],
        },
    )
    commit = FakeCommitPort()
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        commit_port=commit,
    )

    outcome = orchestrator.step(run_id)

    assert outcome.state is RunState.COMMITTING
    assert commit.execute_calls == 0
    assert not any(
        evidence.kind == "internal_review_passed"
        for evidence in fixture.repository.list_evidence(run_id)
    )


def test_monitor_uses_review_evidence_derived_from_commit(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.LOCAL_VALIDATION)
    executor = FakeStageExecutor(fixture.repository)
    commit = FakeCommitPort()
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        stage_executor=executor,
        commit_port=commit,
    )
    assert orchestrator.step(run_id).state is RunState.INTERNAL_REVIEW
    assert orchestrator.step(run_id).state is RunState.COMMITTING
    assert orchestrator.step(run_id).state is RunState.PUSHING_FEATURE
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET state = ?, pr_number = 42 WHERE run_id = ?",
            (RunState.MONITORING_CI_REVIEW.value, run_id),
        )
    fixture.github.snapshot_result = Snapshot(NEW_SHA)
    for kind in (
        "ci_passed",
        "acceptance_passed",
        "threads_resolved",
        "mergeable",
        "head_sha_verified",
    ):
        fixture.repository.register_evidence(
            run_id,
            f"{kind}:{run_id}:{NEW_SHA}",
            kind,
            {"sha": NEW_SHA, "passed": True},
        )

    outcome = orchestrator.step(run_id)

    assert outcome.state is RunState.READY_TO_MERGE


@pytest.mark.parametrize(
    ("state", "target", "port_name"),
    [
        (RunState.DEPLOYING_TEST, RunState.ACCEPTANCE_TESTING, "test"),
        (
            RunState.ACCEPTANCE_TESTING,
            RunState.PR_READY_FOR_REVIEW,
            "acceptance",
        ),
    ],
)
def test_test_environment_handlers_execute_at_most_one_side_effect(
    fixture: Fixture,
    state: RunState,
    target: RunState,
    port_name: str,
) -> None:
    run_id = fixture.create(state)
    port = FakeReconciledPort()
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        test_environment=port if port_name == "test" else None,
        acceptance_executor=port if port_name == "acceptance" else None,
    )

    outcome = orchestrator.step(run_id)

    assert outcome.state is target
    assert port.query_calls == 1
    assert port.execute_calls == 1


def test_pr_ready_marks_once_and_pending_recovery_only_queries(
    fixture: Fixture,
) -> None:
    first_id = fixture.create(RunState.PR_READY_FOR_REVIEW)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET pr_number = 42 WHERE run_id = ?",
            (first_id,),
        )
    first = fixture.orchestrator().step(first_id)
    assert first.state is RunState.MONITORING_CI_REVIEW
    assert fixture.github.mark_ready_calls == 1

    second_id = "run-pr-ready-pending"
    fixture.repository.create_run(second_id, "test-project", CONFIG_HASH)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET state = ?, head_sha = ?, pr_number = 42 WHERE run_id = ?",
            (RunState.PR_READY_FOR_REVIEW.value, SHA, second_id),
        )
    second_root = fixture.root / "runs" / second_id
    second_root.mkdir(parents=True)
    for name in ("requirement.md", "design.md", "implementation-plan.md"):
        (second_root / name).write_text(name, encoding="utf-8")
    fixture.repository.begin_remote_operation(
        f"mark_ready:{second_id}:42:{SHA}",
        second_id,
        "mark_ready",
        f"owner/repo:42:{SHA}",
    )
    fixture.github.is_draft = False
    before = fixture.github.mark_ready_calls
    second = fixture.orchestrator().step(second_id)
    assert second.state is RunState.MONITORING_CI_REVIEW
    assert fixture.github.mark_ready_calls == before


def test_worktree_and_commit_ports_reconcile_without_replay(
    fixture: Fixture,
) -> None:
    worktree_id = fixture.create(RunState.PREPARING_WORKTREE)
    worktree = FakeReconciledPort()
    worktree_orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        worktree_port=worktree,
    )
    assert worktree_orchestrator.step(worktree_id).state is RunState.IMPLEMENTING
    assert worktree.execute_calls == 1

    commit_id = fixture.create(RunState.LOCAL_VALIDATION)
    commit = FakeCommitPort()
    executor = FakeStageExecutor(fixture.repository)
    commit_orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        stage_executor=executor,
        commit_port=commit,
    )
    assert (
        commit_orchestrator.step(commit_id).state
        is RunState.INTERNAL_REVIEW
    )
    assert commit_orchestrator.step(commit_id).state is RunState.COMMITTING
    assert commit_orchestrator.step(commit_id).state is RunState.PUSHING_FEATURE
    assert commit.execute_calls == 1



def test_ready_merge_and_cleanup_are_observation_driven(
    fixture: Fixture,
) -> None:
    ready_id = fixture.create(RunState.READY_TO_MERGE)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET pr_number = 42 WHERE run_id = ?",
            (ready_id,),
        )
    fixture.github.merged = True
    outcome = fixture.orchestrator().step(ready_id)
    assert outcome.state is RunState.READY_TO_MERGE
    assert outcome.action == "ready_to_merge"
    assert fixture.github.merge_query_calls == 0
    assert fixture.repository.get_run(ready_id).state is RunState.READY_TO_MERGE

    merged_id = fixture.create(RunState.MERGED)
    cleanup = FakeCleanupPort()
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
        cleanup_port=cleanup,
    )
    assert orchestrator.step(merged_id).state is RunState.CLEANING_LOCAL
    assert cleanup.execute_calls == 1


def test_cli_start_dry_run_bypasses_draft_creation_path(
    fixture: Fixture,
) -> None:
    requirement = fixture.root / "input.md"
    requirement.write_text("requirement", encoding="utf-8")

    class DryDiscovery:
        def __init__(self) -> None:
            self.calls = 0

        def discover(self, *, dry_run: bool):
            assert dry_run is True
            self.calls += 1
            return SimpleNamespace(
                repo_path=fixture.root.resolve(),
                remote="origin",
                base_branch="develop",
                test_branch="test",
                remote_shas={"origin/develop": SHA, "origin/test": OTHER_SHA},
            )

        def deployed_sha(self) -> str:
            return OTHER_SHA

    discovery = DryDiscovery()
    dry_run_config = config(fixture.root).model_copy(
        update={
            "project_id": "example-backend",
            "version_endpoint": "https://api.test.example.invalid/health",
        }
    )
    services = CliServices(
        plugin_root=fixture.root,
        repository=fixture.repository,
        project_loader=lambda project_id: (dry_run_config, CONFIG_HASH),
        discovery_factory=lambda _project: discovery,
    )
    stdout, stderr = StringIO(), StringIO()

    result = main(
        [
            "start",
            "--project",
            "example-backend",
            "--requirement-file",
            str(requirement),
            "--dry-run",
            "--json",
        ],
        services=services,
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 0, stderr.getvalue()
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "passed"
    assert payload["base_sha"] == SHA
    assert payload["test_sha"] == OTHER_SHA
    assert discovery.calls == 2
    assert fixture.repository.list_active_runs() == []
    assert not (fixture.root / "runs").exists()


def test_cli_start_prepare_and_approve_lifecycle_is_hash_bound(
    fixture: Fixture,
) -> None:
    requirement = fixture.root / "input.md"
    requirement.write_text("# Requirement\n\nShip safely.", encoding="utf-8")

    class DesignStage:
        def execute(
            self,
            stage: str,
            run: object,
            project: object,
            run_root: Path,
            **_: object,
        ) -> StageResult:
            assert stage == "design"
            (run_root / "design.md").write_text(
                "# Design\n\nBounded design.",
                encoding="utf-8",
            )
            (run_root / "implementation-plan.md").write_text(
                "# Implementation Plan\n\n1. Test first.",
                encoding="utf-8",
            )
            reference = fixture.repository.register_evidence(
                run.run_id,  # type: ignore[attr-defined]
                f"design:{run.run_id}",  # type: ignore[attr-defined]
                "design_passed",
                {"passed": True, "sha": SHA},
            )
            return StageResult(
                status="passed",
                summary="design ready",
                evidence=(reference,),
                findings=(),
                next_action="prepare approval",
            )

    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        stage_executor=DesignStage(),
    )
    services = CliServices(
        plugin_root=fixture.root,
        repository=fixture.repository,
        project_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        orchestrator=orchestrator,
        base_sha_provider=lambda project: SHA,
    )

    started = StringIO()
    assert main(
        [
            "start",
            "--project",
            "test-project",
            "--requirement-file",
            str(requirement),
            "--json",
        ],
        services=services,
        stdout=started,
        stderr=StringIO(),
    ) == 0
    run_id = json.loads(started.getvalue())["run_id"]

    assert main(
        ["resume", run_id, "--json"],
        services=services,
        stdout=StringIO(),
        stderr=StringIO(),
    ) == 0
    assert (
        fixture.repository.get_run(run_id).state
        is RunState.AWAITING_DESIGN_APPROVAL
    )

    first, second = StringIO(), StringIO()
    assert main(
        ["prepare-approval", run_id, "--json"],
        services=services,
        stdout=first,
        stderr=StringIO(),
    ) == 0
    events_after_first = fixture.repository.list_events(run_id)
    approval_bytes = (
        fixture.root / "runs" / run_id / "approval.json"
    ).read_bytes()
    assert main(
        ["prepare-approval", run_id, "--json"],
        services=services,
        stdout=second,
        stderr=StringIO(),
    ) == 0
    prepared = json.loads(first.getvalue())
    assert json.loads(second.getvalue())["approval_sha256"] == prepared[
        "approval_sha256"
    ]
    assert (
        fixture.root / "runs" / run_id / "approval.json"
    ).read_bytes() == approval_bytes
    assert fixture.repository.list_events(run_id) == events_after_first
    assert fixture.repository.get_run(run_id).state is RunState.AWAITING_DESIGN_APPROVAL

    wrong_error = StringIO()
    assert main(
        [
            "approve",
            run_id,
            "--approval-file",
            prepared["approval_file"],
            "--confirm-hash",
            "0" * 64,
            "--json",
        ],
        services=services,
        stdout=StringIO(),
        stderr=wrong_error,
    ) != 0
    assert fixture.repository.get_run(run_id).state is RunState.AWAITING_DESIGN_APPROVAL

    approved = StringIO()
    assert main(
        [
            "approve",
            run_id,
            "--approval-file",
            prepared["approval_file"],
            "--confirm-hash",
            prepared["approval_sha256"],
            "--json",
        ],
        services=services,
        stdout=approved,
        stderr=StringIO(),
    ) == 0
    run = fixture.repository.get_run(run_id)
    assert run.state is RunState.APPROVED
    assert run.head_sha == SHA
    assert json.loads(approved.getvalue())["approval_sha256"] == prepared[
        "approval_sha256"
    ]
    approved_artifacts = fixture.repository.get_approved_artifacts(run_id)
    original_design = (
        fixture.root / "runs" / run_id / "design.md"
    ).read_bytes()
    assert approved_artifacts["requirement.md"] == requirement.read_bytes()
    assert approved_artifacts["design.md"] == original_design
    (fixture.root / "runs" / run_id / "design.md").write_text(
        "replaced after approval",
        encoding="utf-8",
    )
    executor = RuntimeStageExecutor(fixture.repository, fixture.root)
    scope = json.loads(
        executor._approval_scope(  # type: ignore[attr-defined]
            "implementation",
            fixture.repository.get_run(run_id),
            fixture.root / "runs" / run_id,
        )
    )
    assert scope["design.md"] == original_design.decode("utf-8")


def test_approved_artifact_rejects_content_and_row_digest_double_tamper(
    fixture: Fixture,
) -> None:
    run_id, _ = approve_fixture_run(fixture)
    malicious = b"attacker-controlled design"
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            """
            UPDATE approved_artifacts
            SET content = ?, sha256 = ?
            WHERE run_id = ? AND artifact_name = 'design.md'
            """,
            (malicious, hashlib.sha256(malicious).hexdigest(), run_id),
        )

    with pytest.raises(ValueError, match="approved artifact snapshot is corrupt"):
        fixture.repository.get_approved_artifacts(run_id)


def test_same_hash_reapprove_backfills_legacy_missing_artifacts_in_later_state(
    fixture: Fixture,
) -> None:
    run_id, run_root = approve_fixture_run(fixture)
    fixture.repository.transition(
        run_id,
        RunState.APPROVED,
        RunState.PREPARING_WORKTREE,
        "APPROVAL_SCOPE_ACCEPTED",
    )
    before_events = fixture.repository.list_events(run_id)
    before_evidence = fixture.repository.list_evidence(run_id)
    with fixture.repository.database.transaction() as connection:
        connection.execute("DROP TABLE approved_artifacts")
    fixture.repository.database.migrate()
    approval_file = run_root / "approval.json"
    approval_hash = hashlib.sha256(approval_file.read_bytes()).hexdigest()
    services = CliServices(
        plugin_root=fixture.root,
        repository=fixture.repository,
        project_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
    )
    stdout, stderr = StringIO(), StringIO()

    result = main(
        [
            "approve",
            run_id,
            "--approval-file",
            str(approval_file),
            "--confirm-hash",
            approval_hash,
            "--json",
        ],
        services=services,
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 0, stderr.getvalue()
    assert fixture.repository.get_run(run_id).state is RunState.PREPARING_WORKTREE
    assert fixture.repository.list_events(run_id) == before_events
    assert fixture.repository.list_evidence(run_id) == before_evidence
    assert set(fixture.repository.get_approved_artifacts(run_id)) == {
        "requirement.md",
        "design.md",
        "implementation-plan.md",
    }
    executor = RuntimeStageExecutor(fixture.repository, fixture.root)
    scope = json.loads(
        executor._approval_scope(  # type: ignore[attr-defined]
            "implementation",
            fixture.repository.get_run(run_id),
            run_root,
        )
    )
    assert scope["design.md"] == "design.md"
    assert json.loads(stdout.getvalue())["backfilled"] is True


@pytest.mark.parametrize("attack", ["wrong_hash", "missing", "changed"])
def test_legacy_backfill_rejects_nonidentical_approval_scope(
    fixture: Fixture,
    attack: str,
) -> None:
    run_id, run_root = approve_fixture_run(fixture)
    fixture.repository.transition(
        run_id,
        RunState.APPROVED,
        RunState.PREPARING_WORKTREE,
        "APPROVAL_SCOPE_ACCEPTED",
    )
    approval_file = run_root / "approval.json"
    approval_hash = hashlib.sha256(approval_file.read_bytes()).hexdigest()
    with fixture.repository.database.transaction() as connection:
        connection.execute("DROP TABLE approved_artifacts")
    fixture.repository.database.migrate()
    if attack == "missing":
        (run_root / "design.md").unlink()
    elif attack == "changed":
        (run_root / "design.md").write_text("changed", encoding="utf-8")
    if attack == "wrong_hash":
        approval_hash = "0" * 64
    services = CliServices(
        plugin_root=fixture.root,
        repository=fixture.repository,
        project_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
    )
    stderr = StringIO()

    result = main(
        [
            "approve",
            run_id,
            "--approval-file",
            str(approval_file),
            "--confirm-hash",
            approval_hash,
            "--json",
        ],
        services=services,
        stdout=StringIO(),
        stderr=stderr,
    )

    assert result != 0
    assert fixture.repository.get_run(run_id).state is RunState.PREPARING_WORKTREE
    with fixture.repository.database.connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM approved_artifacts WHERE run_id = ?",
            (run_id,),
        ).fetchone()["count"]
    assert count == 0
    assert "restore exact approved content" in stderr.getvalue()
    assert "same confirmation hash" in stderr.getvalue()


def test_prepare_approval_converges_and_releases_lock_after_error(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.AWAITING_DESIGN_APPROVAL)
    services = CliServices(
        plugin_root=fixture.root,
        repository=fixture.repository,
        project_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        base_sha_provider=lambda project: SHA,
    )

    def prepare() -> tuple[int, dict[str, object]]:
        output, error = StringIO(), StringIO()
        result = main(
            ["prepare-approval", run_id, "--json"],
            services=services,
            stdout=output,
            stderr=error,
        )
        return result, json.loads(output.getvalue() or error.getvalue())

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: prepare(), range(2)))

    assert [result for result, _payload in results] == [0, 0]
    assert len(
        {payload["approval_sha256"] for _result, payload in results}
    ) == 1

    second_run = fixture.repository.create_run(
        "run-lock-error",
        "test-project",
        CONFIG_HASH,
    )
    run_root = fixture.root / "runs" / second_run.run_id
    run_root.mkdir(parents=True)
    for name in ("requirement.md", "design.md", "implementation-plan.md"):
        (run_root / name).write_text(name, encoding="utf-8")
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET state = ? WHERE run_id = ?",
            (RunState.AWAITING_DESIGN_APPROVAL.value, second_run.run_id),
        )
    services.base_sha_provider = lambda project: (_ for _ in ()).throw(
        RuntimeError("transient provider failure")
    )
    failed, _ = prepare_for_run(services, second_run.run_id)
    assert failed != 0
    services.base_sha_provider = lambda project: SHA
    recovered, _ = prepare_for_run(services, second_run.run_id)
    assert recovered == 0


def prepare_for_run(
    services: CliServices,
    run_id: str,
) -> tuple[int, dict[str, object]]:
    output, error = StringIO(), StringIO()
    result = main(
        ["prepare-approval", run_id, "--json"],
        services=services,
        stdout=output,
        stderr=error,
    )
    return result, json.loads(output.getvalue() or error.getvalue())


def test_monitor_requires_all_six_gates_for_current_head(fixture: Fixture) -> None:
    run_id = fixture.create(RunState.MONITORING_CI_REVIEW)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET pr_number = 42 WHERE run_id = ?",
            (run_id,),
        )
    fixture.github.snapshot_result = Snapshot(SHA)
    for kind in ("acceptance_passed", "internal_review_passed"):
        fixture.repository.register_evidence(
            run_id,
            f"old-{kind}",
            kind,
            {"sha": OTHER_SHA, "passed": True},
        )

    blocked = fixture.orchestrator().step(run_id)

    assert blocked.state is RunState.MONITORING_CI_REVIEW
    assert "current head SHA" in (blocked.blocked_reason or "")
    for kind in ("acceptance_passed", "internal_review_passed"):
        fixture.repository.register_evidence(
            run_id,
            f"current-{kind}",
            kind,
            {"sha": SHA, "passed": True},
        )

    ready = fixture.orchestrator().step(run_id)

    assert ready.state is RunState.READY_TO_MERGE
    assert len(ready.evidence_ids) == 6


@pytest.mark.parametrize(
    ("changes", "expected_state"),
    [
        ({"checks_status": "pending"}, RunState.MONITORING_CI_REVIEW),
        ({"checks_status": "failed"}, RunState.REVIEW_DECISION_REQUIRED),
        ({"review_status": "pending"}, RunState.MONITORING_CI_REVIEW),
        ({"review_status": "changes_requested"}, RunState.FIXING_CI_REVIEW),
        (
            {"unresolved_thread_ids": ("PRRT_actionable",)},
            RunState.FIXING_CI_REVIEW,
        ),
        ({"mergeable": False}, RunState.MONITORING_CI_REVIEW),
        ({"is_draft": True}, RunState.MONITORING_CI_REVIEW),
    ],
)
def test_monitor_distinguishes_waiting_from_actionable_failure(
    fixture: Fixture,
    changes: dict[str, object],
    expected_state: RunState,
) -> None:
    run_id = fixture.create(RunState.MONITORING_CI_REVIEW)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET pr_number = 42 WHERE run_id = ?",
            (run_id,),
        )
    actionable_facts: dict[str, object] = {}
    if changes.get("checks_status") == "failed":
        actionable_facts["failed_checks"] = (FAILED_CHECK,)
    if (
        changes.get("review_status") == "changes_requested"
        and not changes.get("unresolved_thread_ids")
    ):
        actionable_facts["requested_reviews"] = (REQUESTED_REVIEW,)
    fixture.github.snapshot_result = Snapshot(
        SHA, **changes, **actionable_facts
    )
    if changes.get("unresolved_thread_ids"):
        fixture.github.actionable_reviews = (
            ReviewThread(
                thread_id="PRRT_actionable",
                resolved=False,
                outdated=False,
                path="src/example.py",
                line=17,
                comments=("Please fix this issue.",),
            ),
        )
    for kind in ("acceptance_passed", "internal_review_passed"):
        fixture.repository.register_evidence(
            run_id, f"{kind}-ok", kind, {"sha": SHA, "passed": True}
        )

    outcome = fixture.orchestrator().step(run_id)

    assert outcome.state is expected_state


def test_monitor_binds_actionable_review_content_and_attempt_to_fix_event(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.MONITORING_CI_REVIEW)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET pr_number = 42 WHERE run_id = ?",
            (run_id,),
        )
    fixture.github.snapshot_result = Snapshot(
        SHA,
        review_status="changes_requested",
        unresolved_thread_ids=("PRRT_actionable",),
    )
    fixture.github.actionable_reviews = (
        ReviewThread(
            thread_id="PRRT_actionable",
            resolved=False,
            outdated=False,
            path="src/example.py",
            line=17,
            comments=("Handle the empty input before indexing.",),
        ),
    )

    outcome = fixture.orchestrator().step(run_id)

    assert outcome.state is RunState.FIXING_CI_REVIEW
    event = fixture.repository.list_events(run_id)[-1]
    evidence_ids = event.evidence_ids
    assert len(evidence_ids) == 1
    evidence = fixture.repository.get_evidence(evidence_ids[0])
    payload = json.loads(evidence.payload_json)
    assert evidence.kind == "ci_review_fix_request"
    assert payload["attempt"] == 1
    assert payload["issues"][0]["existing_attempts"] == 0
    assert payload["untrusted_data"]["threads"] == [
        {
            "comments": ["Handle the empty input before indexing."],
            "line": 17,
            "path": "src/example.py",
            "thread_id": "PRRT_actionable",
        }
    ]


def test_runtime_review_fix_receives_event_bound_feedback_and_attempt(
    fixture: Fixture,
) -> None:
    run_id, _ = approve_fixture_run(fixture)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET state = ?, pr_number = 42 WHERE run_id = ?",
            (RunState.MONITORING_CI_REVIEW.value, run_id),
        )
    fixture.github.snapshot_result = Snapshot(
        SHA,
        review_status="changes_requested",
        requested_reviews=(REQUESTED_REVIEW,),
    )
    fixture.orchestrator().step(run_id)
    worktree = fixture.root / "worktrees" / run_id
    worktree.mkdir(parents=True)
    captured: list[object] = []

    class FakeCodex:
        def run_stage(self, request: object) -> StageResult:
            captured.append(request)
            return StageResult(
                status="blocked",
                summary="captured",
                evidence=(),
                findings=(),
                next_action="host decides",
            )

    executor = RuntimeStageExecutor(
        fixture.repository,
        fixture.root,
        codex_factory=lambda: FakeCodex(),
    )
    executor.execute(
        "review_fix",
        fixture.repository.get_run(run_id),
        config(fixture.root),
        fixture.root / "runs" / run_id,
    )

    request = captured[0]
    scope = json.loads(request.approval_scope)  # type: ignore[attr-defined]
    assert request.attempt == 1  # type: ignore[attr-defined]
    assert scope["actionable_feedback"]["untrusted_data"] == {
        "failed_checks": [],
        "requested_reviews": [REQUESTED_REVIEW.model_dump(mode="json")],
        "threads": [],
    }
    assert set(scope["approved_artifacts"]) == {
        "requirement.md",
        "design.md",
        "implementation-plan.md",
    }


def test_monitor_stops_same_feedback_after_two_automatic_fix_rounds(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.MONITORING_CI_REVIEW)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET pr_number = 42 WHERE run_id = ?",
            (run_id,),
        )
    fixture.github.snapshot_result = Snapshot(
        SHA,
        review_status="changes_requested",
        requested_reviews=(REQUESTED_REVIEW,),
    )
    orchestrator = fixture.orchestrator()

    first = orchestrator.step(run_id)
    assert first.state is RunState.FIXING_CI_REVIEW
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET state = ? WHERE run_id = ?",
            (RunState.MONITORING_CI_REVIEW.value, run_id),
        )
    second = orchestrator.step(run_id)
    assert second.state is RunState.FIXING_CI_REVIEW
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET state = ? WHERE run_id = ?",
            (RunState.MONITORING_CI_REVIEW.value, run_id),
        )

    stopped = orchestrator.step(run_id)

    assert stopped.state is RunState.REVIEW_DECISION_REQUIRED
    assert stopped.action == "review_fix_limit_reached"
    assert "two automatic fix rounds" in (stopped.blocked_reason or "")
    requests = [
        evidence
        for evidence in fixture.repository.list_evidence(run_id)
        if evidence.kind == "ci_review_fix_request"
    ]
    assert len(requests) == 2


def test_same_thread_comment_changes_do_not_reset_two_round_budget(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.MONITORING_CI_REVIEW)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET pr_number = 42 WHERE run_id = ?", (run_id,)
        )
    fixture.github.snapshot_result = Snapshot(
        SHA, unresolved_thread_ids=("PRRT_stable",)
    )
    orchestrator = fixture.orchestrator()
    for comment in ("first wording", "clarified wording"):
        fixture.github.actionable_reviews = (
            ReviewThread(
                thread_id="PRRT_stable",
                resolved=False,
                outdated=False,
                path="src/app.py",
                line=9,
                comments=(comment,),
            ),
        )
        assert orchestrator.step(run_id).state is RunState.FIXING_CI_REVIEW
        with fixture.repository.database.transaction() as connection:
            connection.execute(
                "UPDATE runs SET state = ? WHERE run_id = ?",
                (RunState.MONITORING_CI_REVIEW.value, run_id),
            )
    fixture.github.actionable_reviews = (
        ReviewThread(
            thread_id="PRRT_stable",
            resolved=False,
            outdated=False,
            path="src/app.py",
            line=10,
            comments=("third wording",),
        ),
    )

    stopped = orchestrator.step(run_id)

    assert stopped.state is RunState.REVIEW_DECISION_REQUIRED
    assert stopped.action == "review_fix_limit_reached"


def test_existing_thread_budget_is_not_reset_when_another_thread_is_added(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.MONITORING_CI_REVIEW)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET pr_number = 42 WHERE run_id = ?", (run_id,)
        )
    orchestrator = fixture.orchestrator()
    thread_a = ReviewThread(
        thread_id="PRRT_thread_a",
        resolved=False,
        outdated=False,
        path="a.py",
        line=1,
        comments=("fix A",),
    )
    thread_b = ReviewThread(
        thread_id="PRRT_thread_b",
        resolved=False,
        outdated=False,
        path="b.py",
        line=2,
        comments=("fix B",),
    )
    fixture.github.snapshot_result = Snapshot(
        SHA, unresolved_thread_ids=("PRRT_thread_a",)
    )
    fixture.github.actionable_reviews = (thread_a,)
    assert orchestrator.step(run_id).state is RunState.FIXING_CI_REVIEW
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET state = ? WHERE run_id = ?",
            (RunState.MONITORING_CI_REVIEW.value, run_id),
        )
    fixture.github.snapshot_result = Snapshot(
        SHA,
        unresolved_thread_ids=("PRRT_thread_a", "PRRT_thread_b"),
    )
    fixture.github.actionable_reviews = (thread_a, thread_b)
    assert orchestrator.step(run_id).state is RunState.FIXING_CI_REVIEW
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET state = ? WHERE run_id = ?",
            (RunState.MONITORING_CI_REVIEW.value, run_id),
        )

    stopped = orchestrator.step(run_id)

    assert stopped.state is RunState.REVIEW_DECISION_REQUIRED
    assert stopped.action == "review_fix_limit_reached"


@pytest.mark.parametrize(
    "snapshot",
    [
        Snapshot(SHA, checks_status="failed"),
        Snapshot(SHA, review_status="changes_requested"),
    ],
)
def test_monitor_pauses_when_remote_failure_has_no_actionable_details(
    fixture: Fixture,
    snapshot: Snapshot,
) -> None:
    run_id = fixture.create(RunState.MONITORING_CI_REVIEW)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET pr_number = 42 WHERE run_id = ?", (run_id,)
        )
    fixture.github.snapshot_result = snapshot

    outcome = fixture.orchestrator().step(run_id)

    assert outcome.state is RunState.REVIEW_DECISION_REQUIRED
    assert outcome.action == "actionable_feedback_missing"


def test_monitor_pauses_mixed_ci_failure_instead_of_sending_metadata_to_codex(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.MONITORING_CI_REVIEW)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET pr_number = 42 WHERE run_id = ?", (run_id,)
        )
    fixture.github.snapshot_result = Snapshot(
        SHA,
        checks_status="failed",
        failed_checks=(FAILED_CHECK,),
        review_status="changes_requested",
        requested_reviews=(REQUESTED_REVIEW,),
    )

    outcome = fixture.orchestrator().step(run_id)

    assert outcome.state is RunState.REVIEW_DECISION_REQUIRED
    assert outcome.action == "actionable_feedback_missing"
    assert not any(
        evidence.kind == "ci_review_fix_request"
        for evidence in fixture.repository.list_evidence(run_id)
    )


def test_review_fix_push_replies_once_and_recovery_does_not_duplicate(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.MONITORING_CI_REVIEW)
    thread = ReviewThread(
        thread_id="PRRT_reply_once",
        resolved=False,
        outdated=False,
        path="src/app.py",
        line=12,
        comments=("Please handle empty input.",),
    )
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET pr_number = 42 WHERE run_id = ?", (run_id,)
        )
    fixture.github.snapshot_result = Snapshot(
        SHA, unresolved_thread_ids=(thread.thread_id,)
    )
    fixture.github.actionable_reviews = (thread,)
    assert fixture.orchestrator().step(run_id).state is RunState.FIXING_CI_REVIEW
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET state = ?, head_sha = ? WHERE run_id = ?",
            (RunState.PUSHING_FEATURE.value, NEW_SHA, run_id),
        )
    fixture.git.remote_feature_sha = NEW_SHA
    fixture.github.hide_reply_once = True
    orchestrator = fixture.orchestrator()

    pending = orchestrator.step(run_id)
    recovered = orchestrator.step(run_id)

    assert pending.state is RunState.PUSHING_FEATURE
    assert pending.action == "review_reply_reconciliation_pending"
    assert recovered.state is RunState.PR_DRAFT
    assert fixture.github.reply_calls == 1
    replies = [
        evidence
        for evidence in fixture.repository.list_evidence(run_id)
        if evidence.kind == "review_thread_replied"
    ]
    assert len(replies) == 1
    payload = json.loads(replies[0].payload_json)
    assert payload["request_head_sha"] == SHA
    assert payload["fix_sha"] == NEW_SHA
    assert payload["thread_id"] == thread.thread_id

    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET state = ? WHERE run_id = ?",
            (RunState.MONITORING_CI_REVIEW.value, run_id),
        )
    fixture.github.snapshot_result = Snapshot(
        NEW_SHA, unresolved_thread_ids=(thread.thread_id,)
    )
    waiting = orchestrator.step(run_id)
    assert waiting.state is RunState.MONITORING_CI_REVIEW
    assert waiting.action == "awaiting_review_update"


def test_review_fix_push_rejects_tampered_request_before_thread_reply(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.MONITORING_CI_REVIEW)
    thread = ReviewThread(
        thread_id="PRRT_tampered_request",
        resolved=False,
        outdated=False,
        path="src/app.py",
        line=12,
        comments=("Please handle empty input.",),
    )
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET pr_number = 42 WHERE run_id = ?", (run_id,)
        )
    fixture.github.snapshot_result = Snapshot(
        SHA, unresolved_thread_ids=(thread.thread_id,)
    )
    fixture.github.actionable_reviews = (thread,)
    requested = fixture.orchestrator().step(run_id)
    evidence_id = requested.evidence_ids[0]
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE evidence SET payload_json = ? WHERE evidence_id = ?",
            ('{"tampered":true}', evidence_id),
        )
        connection.execute(
            "UPDATE runs SET state = ?, head_sha = ? WHERE run_id = ?",
            (RunState.PUSHING_FEATURE.value, NEW_SHA, run_id),
        )
    fixture.git.remote_feature_sha = NEW_SHA

    outcome = fixture.orchestrator().step(run_id)

    assert outcome.state is RunState.PUSHING_FEATURE
    assert outcome.action == "review_fix_cycle_blocked"
    assert fixture.github.reply_calls == 0


def test_review_fix_push_rejects_digest_valid_malformed_nested_feedback(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.MONITORING_CI_REVIEW)
    thread = ReviewThread(
        thread_id="PRRT_malformed_nested",
        resolved=False,
        outdated=False,
        path="src/app.py",
        line=12,
        comments=("Please handle empty input.",),
    )
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET pr_number = 42 WHERE run_id = ?", (run_id,)
        )
    fixture.github.snapshot_result = Snapshot(
        SHA, unresolved_thread_ids=(thread.thread_id,)
    )
    fixture.github.actionable_reviews = (thread,)
    requested = fixture.orchestrator().step(run_id)
    evidence = fixture.repository.get_evidence(requested.evidence_ids[0])
    payload = json.loads(evidence.payload_json)
    payload["untrusted_data"]["requested_reviews"] = [{}]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE evidence SET payload_json = ?, sha256 = ? WHERE evidence_id = ?",
            (encoded, sha256_payload(payload), evidence.evidence_id),
        )
        connection.execute(
            "UPDATE runs SET state = ?, head_sha = ? WHERE run_id = ?",
            (RunState.PUSHING_FEATURE.value, NEW_SHA, run_id),
        )
    fixture.git.remote_feature_sha = NEW_SHA

    outcome = fixture.orchestrator().step(run_id)

    assert outcome.state is RunState.PUSHING_FEATURE
    assert outcome.action == "review_fix_cycle_blocked"
    assert fixture.github.reply_calls == 0


def test_top_level_same_problem_across_review_ids_shares_budget(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.MONITORING_CI_REVIEW)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET pr_number = 42 WHERE run_id = ?", (run_id,)
        )
    orchestrator = fixture.orchestrator()
    for review_id in ("PRR_first", "PRR_second"):
        fixture.github.snapshot_result = Snapshot(
            SHA,
            review_status="changes_requested",
            requested_reviews=(
                RequestedReviewFact(
                    review_id=review_id,
                    author="same-reviewer",
                    body="  Add   timeout regression TEST.  ",
                ),
            ),
        )
        assert orchestrator.step(run_id).state is RunState.FIXING_CI_REVIEW
        with fixture.repository.database.transaction() as connection:
            connection.execute(
                "UPDATE runs SET state = ? WHERE run_id = ?",
                (RunState.MONITORING_CI_REVIEW.value, run_id),
            )
    fixture.github.snapshot_result = Snapshot(
        SHA,
        review_status="changes_requested",
        requested_reviews=(
            RequestedReviewFact(
                review_id="PRR_third",
                author="same-reviewer",
                body="add timeout regression test.",
            ),
        ),
    )

    stopped = orchestrator.step(run_id)

    assert stopped.state is RunState.REVIEW_DECISION_REQUIRED
    assert stopped.action == "review_fix_limit_reached"


def test_monitor_resets_attempt_for_changed_feedback_fingerprint(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.MONITORING_CI_REVIEW)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET pr_number = 42 WHERE run_id = ?",
            (run_id,),
        )
    fixture.github.snapshot_result = Snapshot(
        SHA, checks_status="failed", failed_checks=(FAILED_CHECK,)
    )
    orchestrator = fixture.orchestrator()
    orchestrator.step(run_id)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET state = ? WHERE run_id = ?",
            (RunState.MONITORING_CI_REVIEW.value, run_id),
        )
    fixture.github.snapshot_result = Snapshot(
        SHA,
        review_status="changes_requested",
        requested_reviews=(REQUESTED_REVIEW,),
    )

    changed = orchestrator.step(run_id)

    assert changed.state is RunState.FIXING_CI_REVIEW
    latest = fixture.repository.get_evidence(changed.evidence_ids[0])
    assert json.loads(latest.payload_json)["attempt"] == 1


@pytest.mark.parametrize("bad_payload", [
    {"sha": SHA, "passed": False},
    {"sha": SHA, "passed": 1},
    {"sha": SHA, "passed": "true"},
    {"sha": SHA},
    ["not", "an", "object"],
])
def test_monitor_rejects_non_strict_local_gate_payload(
    fixture: Fixture,
    bad_payload: object,
) -> None:
    run_id = fixture.create(RunState.MONITORING_CI_REVIEW)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET pr_number = 42 WHERE run_id = ?",
            (run_id,),
        )
    fixture.github.snapshot_result = Snapshot(SHA)
    fixture.repository.register_evidence(
        run_id, "bad-acceptance", "acceptance_passed", bad_payload
    )
    fixture.repository.register_evidence(
        run_id,
        "good-review",
        "internal_review_passed",
        {"sha": SHA, "passed": True},
    )

    outcome = fixture.orchestrator().step(run_id)

    assert outcome.state is RunState.MONITORING_CI_REVIEW


def test_cli_approve_binds_exact_file_and_event_atomically(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.AWAITING_DESIGN_APPROVAL)
    run_root = fixture.root / "runs" / run_id
    snapshot = build_approval(
        run_id=run_id,
        project_id="test-project",
        config_hash=CONFIG_HASH,
        base_branch="develop",
        base_sha=SHA,
        test_branch="test",
        files=tuple(
            run_root / name
            for name in ("requirement.md", "design.md", "implementation-plan.md")
        ),
        approved_user="tester",
        user_provider=lambda: "tester",
        clock=lambda: datetime(2026, 7, 29, tzinfo=UTC),
    )
    approval_file = run_root / "approval.json"
    save_approval(approval_file, snapshot)
    approval_hash = hashlib.sha256(approval_file.read_bytes()).hexdigest()
    services = CliServices(
        plugin_root=fixture.root,
        repository=fixture.repository,
        project_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
    )
    stdout, stderr = StringIO(), StringIO()

    result = main(
        [
            "approve",
            run_id,
            "--approval-file",
            str(approval_file),
            "--confirm-hash",
            approval_hash,
            "--json",
        ],
        services=services,
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 0, stderr.getvalue()
    assert fixture.repository.get_run(run_id).state is RunState.APPROVED
    event = fixture.repository.list_events(run_id)[-1]
    evidence = fixture.repository.get_evidence(event.evidence_ids[0])
    assert evidence.kind == "explicit_approval"
    assert evidence.sha256 == json.loads(stdout.getvalue())["approval_sha256"]


def test_default_approval_validation_rejects_replaced_approved_scope(
    fixture: Fixture,
) -> None:
    run_id, run_root = approve_fixture_run(fixture)
    for name in ("requirement.md", "design.md", "implementation-plan.md"):
        (run_root / name).write_text(f"changed-{name}", encoding="utf-8")
    replacement = build_approval(
        run_id=run_id,
        project_id="test-project",
        config_hash=CONFIG_HASH,
        base_branch="develop",
        base_sha=SHA,
        test_branch="test",
        files=tuple(
            run_root / name
            for name in ("requirement.md", "design.md", "implementation-plan.md")
        ),
        approved_user="tester",
        user_provider=lambda: "tester",
        clock=lambda: datetime(2026, 7, 29, tzinfo=UTC),
    )
    save_approval(run_root / "approval.json", replacement)
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
    )

    outcome = orchestrator.step(run_id)

    assert outcome.state is RunState.APPROVED
    assert outcome.action == "blocked"


@pytest.mark.parametrize("table", ["approvals", "evidence"])
def test_default_approval_validation_rejects_missing_audit_binding(
    fixture: Fixture,
    table: str,
) -> None:
    run_id, _ = approve_fixture_run(fixture)
    with fixture.repository.database.transaction() as connection:
        if table == "approvals":
            connection.execute("DELETE FROM approvals WHERE run_id = ?", (run_id,))
        else:
            connection.execute(
                "DELETE FROM evidence WHERE run_id = ? AND kind = ?",
                (run_id, "explicit_approval"),
            )
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
    )

    outcome = orchestrator.step(run_id)

    assert outcome.state is RunState.APPROVED
    assert outcome.action == "blocked"


def test_orchestrator_error_reason_does_not_echo_exception_secrets(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.PUSHING_FEATURE)
    secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    orchestrator = Orchestrator(
        repository=fixture.repository,
        plugin_root=fixture.root,
        config_loader=lambda project_id: (_ for _ in ()).throw(
            ValueError(
                f"{secret} https://user:password@example.invalid response-body"
            )
        ),
        git_factory=lambda project: fixture.git,
        github_factory=lambda project: fixture.github,
        approval_validator=lambda run, project, root: True,
    )

    outcome = orchestrator.step(run_id)

    reason = outcome.blocked_reason or ""
    assert secret not in reason
    assert "password" not in reason
    assert "response-body" not in reason


def test_cli_approve_rejects_path_escape(fixture: Fixture) -> None:
    run_id = fixture.create(RunState.AWAITING_DESIGN_APPROVAL)
    outside = fixture.root / "approval.json"
    outside.write_text("{}", encoding="utf-8")
    services = CliServices(
        plugin_root=fixture.root,
        repository=fixture.repository,
        project_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
    )

    result = main(
        [
            "approve",
            run_id,
            "--approval-file",
            str(outside),
            "--confirm-hash",
            "0" * 64,
            "--json",
        ],
        services=services,
        stdout=StringIO(),
        stderr=StringIO(),
    )

    assert result != 0
    assert fixture.repository.get_run(run_id).state is RunState.AWAITING_DESIGN_APPROVAL

def test_cancel_survives_missing_or_changed_project_config(fixture: Fixture) -> None:
    run_id = fixture.create(RunState.MONITORING_CI_REVIEW)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET pr_number = 42 WHERE run_id = ?", (run_id,)
        )
    orchestrator = fixture.orchestrator()
    orchestrator.config_loader = lambda _project_id: (_ for _ in ()).throw(FileNotFoundError("gone"))
    outcome = orchestrator.cancel(run_id)
    assert outcome.state is RunState.CANCELLED
    inventory = fixture.root / "runs" / run_id / "reports" / "resource-inventory.md"
    text = inventory.read_text(encoding="utf-8")
    assert "unavailable" in text
    assert "#42" in text


def test_cli_start_failure_leaves_no_db_run_or_partial_tree(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    requirement = fixture.root / "failed-input.md"
    requirement.write_text("requirement", encoding="utf-8")
    services = CliServices(
        plugin_root=fixture.root,
        repository=fixture.repository,
        project_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
    )
    monkeypatch.setattr(
        fixture.repository,
        "create_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("db failed")),
    )
    result = main(
        ["start", "--project", "test-project", "--requirement-file", str(requirement), "--json"],
        services=services,
        stdout=StringIO(),
        stderr=StringIO(),
    )
    assert result == 2
    runs = fixture.root / "runs"
    assert not runs.exists() or list(runs.iterdir()) == []
    assert fixture.repository.list_active_runs() == []

def test_real_cli_services_scheduler_wires_governance_services(fixture: Fixture) -> None:
    services = CliServices(
        plugin_root=fixture.root,
        repository=fixture.repository,
        project_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
    )
    scheduler = services.get_scheduler()
    assert isinstance(scheduler.retention_service, RetentionService)
    assert isinstance(scheduler.notification_service, NotificationService)

def test_cancel_inventory_failure_does_not_block_cancellation(fixture: Fixture) -> None:
    run_id = fixture.create(RunState.MONITORING_CI_REVIEW)
    orchestrator = fixture.orchestrator()
    orchestrator.artifact_store.write_resource_inventory = (
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk unavailable"))
    )
    assert orchestrator.cancel(run_id).state is RunState.CANCELLED


def test_real_cli_scheduler_reaches_daily_retention(fixture: Fixture) -> None:
    run_id = fixture.create(RunState.COMPLETED)
    raw = fixture.root / "runs" / run_id / "evidence" / "ci-raw-old.log"
    raw.parent.mkdir(exist_ok=True)
    raw.write_text("old", encoding="utf-8")
    old = datetime(2026, 6, 1, tzinfo=UTC).timestamp()
    os.utime(raw, (old, old))
    services = CliServices(
        plugin_root=fixture.root,
        repository=fixture.repository,
        project_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
    )
    scheduler = services.get_scheduler()
    scheduler.clock = lambda: datetime(2026, 7, 31, tzinfo=UTC)
    assert scheduler.tick().status == "idle"
    assert not raw.exists()

def test_cli_pin_persists_across_restart_and_controls_retention(
    fixture: Fixture,
) -> None:
    run_id = fixture.create(RunState.COMPLETED)
    summary = fixture.root / "runs" / run_id / "sanitized-summary.md"
    summary.write_text("safe", encoding="utf-8")
    expired = datetime(2025, 1, 1, tzinfo=UTC).timestamp()
    os.utime(summary, (expired, expired))
    services = CliServices(plugin_root=fixture.root, repository=fixture.repository)

    assert main(
        ["pin", run_id, "--json"], services=services,
        stdout=StringIO(), stderr=StringIO(),
    ) == 0
    restarted_database = Database(fixture.root / "state.sqlite3")
    restarted_database.migrate()
    restarted_repository = RunRepository(restarted_database)
    assert restarted_repository.get_run(run_id).pinned is True
    RetentionService(fixture.root, restarted_repository).clean(
        now=datetime(2026, 7, 31, tzinfo=UTC)
    )
    assert summary.exists()

    restarted_services = CliServices(
        plugin_root=fixture.root, repository=restarted_repository
    )
    assert main(
        ["pin", run_id, "--off", "--json"], services=restarted_services,
        stdout=StringIO(), stderr=StringIO(),
    ) == 0
    final_database = Database(fixture.root / "state.sqlite3")
    final_database.migrate()
    final_repository = RunRepository(final_database)
    assert final_repository.get_run(run_id).pinned is False
    RetentionService(fixture.root, final_repository).clean(
        now=datetime(2026, 7, 31, tzinfo=UTC)
    )
    assert not summary.exists()


def test_cli_start_compensation_preserves_concurrently_advanced_run(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    requirement = fixture.root / "racing-input.md"
    requirement.write_text("requirement", encoding="utf-8")
    created_run_ids: list[str] = []
    original_create = fixture.repository.create_run

    def create_then_advance(run_id: str, project_id: str, config_hash: str):
        original_create(run_id, project_id, config_hash)
        created_run_ids.append(run_id)
        fixture.repository.transition(
            run_id,
            RunState.DRAFT,
            RunState.AWAITING_DESIGN_APPROVAL,
            "CONCURRENT_ADVANCE",
        )
        raise OSError("artifact completion failed")

    monkeypatch.setattr(fixture.repository, "create_run", create_then_advance)
    services = CliServices(
        plugin_root=fixture.root,
        repository=fixture.repository,
        project_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
    )

    result = main(
        ["start", "--project", "test-project", "--requirement-file", str(requirement), "--json"],
        services=services, stdout=StringIO(), stderr=StringIO(),
    )

    assert result == 2
    run_id = created_run_ids[0]
    assert fixture.repository.get_run(run_id).state is RunState.AWAITING_DESIGN_APPROVAL
    assert (fixture.root / "runs" / run_id).is_dir()
