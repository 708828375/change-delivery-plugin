from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from io import StringIO
import json

import pytest

import change_delivery.cli as cli_module
from change_delivery.approval import build_approval, save_approval
from change_delivery.adapters.github import PullRequestRef
from change_delivery.database import Database
from change_delivery.cli import CliServices, main
from change_delivery.config import load_project_config
from change_delivery.domain import (
    EvidenceRef,
    ProjectConfig,
    RunState,
    StageResult,
)
from change_delivery.orchestrator import Orchestrator
from change_delivery.repository import RunRepository


SHA = "a" * 40
OTHER_SHA = "b" * 40
NEW_SHA = "c" * 40
TREE_SHA = "d" * 40
OTHER_TREE_SHA = "e" * 40
CONFIG_HASH = "c" * 64


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
    checks_passed: bool = True
    threads_resolved: bool = True
    mergeable: bool = True
    review_decision: bool = True
    is_draft: bool = False


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
    ) -> None:
        self.repository = repository
        self.status = status
        self.calls: list[str] = []

    def execute(
        self,
        stage: str,
        run: object,
        project: object,
        run_root: Path,
    ) -> StageResult:
        self.calls.append(stage)
        run_id = run.run_id  # type: ignore[attr-defined]
        head_sha = run.head_sha  # type: ignore[attr-defined]
        reference = self.repository.register_evidence(
            run_id,
            f"{stage}-proof:{run_id}",
            (
                "internal_review_source"
                if stage == "internal_review"
                else f"{stage}_passed"
            ),
            {"sha": head_sha, "passed": True},
        )
        return StageResult(
            status=self.status,
            summary=f"{stage} passed",
            evidence=(reference,),
            findings=(),
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

    def validation_tree(self, run: object, project: object) -> str:
        self.validation_tree_calls += 1
        return self.tree_sha

    def review_tree(self, run: object, project: object) -> str:
        self.review_tree_calls += 1
        return self.tree_sha

    def query(self, run: object, project: object) -> dict[str, object] | None:
        self.query_calls += 1
        return self.observed

    def execute(self, run: object, project: object) -> dict[str, object]:
        self.execute_calls += 1
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


def test_cli_help_lists_all_nine_commands() -> None:
    stdout = StringIO()
    assert main(["--help"], stdout=stdout, stderr=StringIO()) == 0
    output = stdout.getvalue()
    for command in (
        "start",
        "register",
        "approve",
        "status",
        "resume",
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
    github_initializations: list[tuple[object, object]] = []

    class StubGitAdapter:
        def __init__(self, repo_path: object, **kwargs: object) -> None:
            git_initializations.append((repo_path, kwargs))

    class StubGitHubAdapter:
        def __init__(self, runner: object, project: object) -> None:
            github_initializations.append((runner, project))

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
    assert "Task14" in json.loads(stderr.getvalue())["error"]
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
    assert json.loads(stdout.getvalue())["action"] == "blocked_dependency"

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
        commit.observed = commit.execute(
            fixture.repository.get_run(run_id),
            config(fixture.root),
        )
        commit.execute_calls = 0
        fixture.repository.begin_remote_operation(
            f"commit:{run_id}:{TREE_SHA}:{SHA}",
            run_id,
            "commit",
            f"test-project:{TREE_SHA}:{SHA}",
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
    assert fixture.orchestrator().step(ready_id).state is RunState.MERGED
    assert fixture.github.merge_query_calls == 1

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


def test_cli_start_dry_run_creates_only_draft_artifacts(
    fixture: Fixture,
) -> None:
    requirement = fixture.root / "input.md"
    requirement.write_text("requirement", encoding="utf-8")
    services = CliServices(
        plugin_root=fixture.root,
        repository=fixture.repository,
        project_loader=lambda project_id: (config(fixture.root), CONFIG_HASH),
    )
    stdout, stderr = StringIO(), StringIO()

    result = main(
        [
            "start",
            "--project",
            "test-project",
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
    run = fixture.repository.get_run(payload["run_id"])
    assert run.state is RunState.DRAFT
    assert {
        path.name for path in (fixture.root / "runs" / run.run_id).iterdir()
    } == {"requirement.md", "design.md", "implementation-plan.md"}


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
    ("field", "value", "expected_state"),
    [
        ("checks_passed", False, RunState.FIXING_CI_REVIEW),
        ("threads_resolved", False, RunState.FIXING_CI_REVIEW),
        ("mergeable", False, RunState.MONITORING_CI_REVIEW),
        ("review_decision", False, RunState.FIXING_CI_REVIEW),
        ("is_draft", True, RunState.MONITORING_CI_REVIEW),
    ],
)
def test_monitor_rejects_each_false_remote_gate(
    fixture: Fixture,
    field: str,
    value: bool,
    expected_state: RunState,
) -> None:
    run_id = fixture.create(RunState.MONITORING_CI_REVIEW)
    with fixture.repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET pr_number = 42 WHERE run_id = ?",
            (run_id,),
        )
    fixture.github.snapshot_result = Snapshot(SHA, **{field: value})
    for kind in ("acceptance_passed", "internal_review_passed"):
        fixture.repository.register_evidence(
            run_id, f"{kind}-ok", kind, {"sha": SHA, "passed": True}
        )

    outcome = fixture.orchestrator().step(run_id)

    assert outcome.state is expected_state


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
            "--json",
        ],
        services=services,
        stdout=StringIO(),
        stderr=StringIO(),
    )

    assert result != 0
    assert fixture.repository.get_run(run_id).state is RunState.AWAITING_DESIGN_APPROVAL
