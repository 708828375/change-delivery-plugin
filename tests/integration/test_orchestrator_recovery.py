from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from io import StringIO
import json

import pytest

from change_delivery.approval import build_approval, save_approval
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
    close_pr_calls: int = 0
    pr_number: int = 42
    snapshot_result: object | None = None

    def ensure_draft_pr(self, branch: str, base: str, head_sha: str) -> int:
        self.ensure_calls += 1
        return self.pr_number

    def snapshot(self, pr_number: int) -> object:
        assert self.snapshot_result is not None
        return self.snapshot_result


@dataclass(frozen=True)
class Snapshot:
    head_sha: str
    checks_passed: bool = True
    threads_resolved: bool = True
    mergeable: bool = True


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
