from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from change_delivery.adapters.github import MergedPullRequestSnapshot
from change_delivery.cli import CliServices, main
from change_delivery.domain import EvidenceRecord, RunRecord, RunState
from change_delivery.errors import BlockedError
from change_delivery.release import ReleaseService
from change_delivery.evidence import sha256_payload


HEAD_SHA = "a" * 40
MERGE_SHA = "b" * 40
EVIDENCE_SHA = sha256_payload({"sha": HEAD_SHA, "passed": True})


class ReadOnlyRepository:
    def __init__(self) -> None:
        timestamp = datetime.now(UTC)
        self.run = RunRecord(
            run_id="run-release",
            project_id="project",
            config_hash="config-hash",
            state=RunState.READY_TO_MERGE,
            head_sha=HEAD_SHA,
            pr_number=42,
            created_at=timestamp,
            updated_at=timestamp,
        )
        self.evidence = [
            EvidenceRecord(
                evidence_id="acceptance-current",
                run_id=self.run.run_id,
                kind="acceptance_passed",
                payload_json=json.dumps(
                    {"sha": HEAD_SHA, "passed": True},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                sha256=EVIDENCE_SHA,
                created_at=timestamp,
            )
        ]
        self.reads: list[tuple[str, str]] = []

    def get_run(self, run_id: str) -> RunRecord:
        self.reads.append(("get_run", run_id))
        if run_id != self.run.run_id:
            raise KeyError(run_id)
        return self.run

    def list_evidence(self, run_id: str) -> list[EvidenceRecord]:
        self.reads.append(("list_evidence", run_id))
        return list(self.evidence)


class ReadOnlyGitHub:
    def __init__(self) -> None:
        self.snapshot = MergedPullRequestSnapshot(
            number=42,
            state="MERGED",
            head_sha=HEAD_SHA,
            merge_commit_sha=MERGE_SHA,
            checks_status="passed",
        )
        self.calls: list[int] = []

    def merged_snapshot(self, pr_number: int) -> MergedPullRequestSnapshot:
        self.calls.append(pr_number)
        return self.snapshot


@pytest.fixture
def release_fixture() -> SimpleNamespace:
    repository = ReadOnlyRepository()
    github = ReadOnlyGitHub()
    config = SimpleNamespace(production_enabled=False)
    service = ReleaseService(
        repository=repository,
        config_loader=lambda project_id: (config, "config-hash"),
        github_factory=lambda loaded: github,
    )
    return SimpleNamespace(
        repository=repository,
        github=github,
        config=config,
        service=service,
    )


def test_release_preflight_is_read_only_and_reports_merge_binding(
    release_fixture: SimpleNamespace,
) -> None:
    before_run = release_fixture.repository.run.model_dump()
    before_evidence = [
        item.model_dump() for item in release_fixture.repository.evidence
    ]

    report = release_fixture.service.preflight("run-release")

    assert report.eligible is True
    assert report.run_id == "run-release"
    assert report.pr_number == 42
    assert report.acceptance_sha == HEAD_SHA
    assert report.merge_commit_sha == MERGE_SHA
    assert report.checks_status == "passed"
    assert report.production_adapter_disabled is True
    assert report.acceptance_evidence_sha256 == EVIDENCE_SHA
    assert release_fixture.repository.run.model_dump() == before_run
    assert [
        item.model_dump() for item in release_fixture.repository.evidence
    ] == before_evidence
    assert release_fixture.repository.reads == [
        ("get_run", "run-release"),
        ("list_evidence", "run-release"),
    ]
    assert release_fixture.github.calls == [42]


def test_release_preflight_requires_ready_run(
    release_fixture: SimpleNamespace,
) -> None:
    release_fixture.repository.run = release_fixture.repository.run.model_copy(
        update={"state": RunState.MONITORING_CI_REVIEW}
    )

    with pytest.raises(BlockedError, match="READY_TO_MERGE"):
        release_fixture.service.preflight("run-release")

    assert release_fixture.github.calls == []


def test_release_preflight_requires_merged_pr(
    release_fixture: SimpleNamespace,
) -> None:
    release_fixture.github.snapshot = release_fixture.github.snapshot.model_copy(
        update={"state": "OPEN", "merge_commit_sha": None}
    )

    with pytest.raises(BlockedError, match="尚未合并"):
        release_fixture.service.preflight("run-release")


def test_release_preflight_fails_closed_without_merge_commit(
    release_fixture: SimpleNamespace,
) -> None:
    release_fixture.github.snapshot = release_fixture.github.snapshot.model_copy(
        update={"merge_commit_sha": None}
    )

    with pytest.raises(BlockedError, match="merge commit"):
        release_fixture.service.preflight("run-release")


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_release_preflight_rejects_nonpassing_required_checks(
    release_fixture: SimpleNamespace,
    status: str,
) -> None:
    release_fixture.github.snapshot = release_fixture.github.snapshot.model_copy(
        update={"checks_status": status}
    )

    with pytest.raises(BlockedError, match="required checks"):
        release_fixture.service.preflight("run-release")


def test_release_preflight_requires_acceptance_sha_bound_to_merged_head(
    release_fixture: SimpleNamespace,
) -> None:
    release_fixture.github.snapshot = release_fixture.github.snapshot.model_copy(
        update={"head_sha": "d" * 40}
    )

    with pytest.raises(BlockedError, match="验收 SHA.*merge"):
        release_fixture.service.preflight("run-release")


@pytest.mark.parametrize(
    "payload",
    [
        {"sha": "d" * 40, "passed": True},
        {"sha": HEAD_SHA, "passed": False},
        {"sha": HEAD_SHA, "passed": 1},
        {"sha": HEAD_SHA, "passed": "true"},
        {"sha": HEAD_SHA},
        ["not", "an", "object"],
    ],
)
def test_release_preflight_rejects_stale_or_non_strict_acceptance(
    release_fixture: SimpleNamespace,
    payload: object,
) -> None:
    release_fixture.repository.evidence[0] = (
        release_fixture.repository.evidence[0].model_copy(
            update={
                "payload_json": json.dumps(
                    payload,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            }
        )
    )

    with pytest.raises(BlockedError, match="测试验收"):
        release_fixture.service.preflight("run-release")


def test_release_preflight_rejects_enabled_production_configuration(
    release_fixture: SimpleNamespace,
) -> None:
    release_fixture.config.production_enabled = True

    with pytest.raises(BlockedError, match="正式发布适配"):
        release_fixture.service.preflight("run-release")

    assert release_fixture.github.calls == []


def test_release_preflight_rejects_injected_production_adapter(
    release_fixture: SimpleNamespace,
) -> None:
    service = ReleaseService(
        repository=release_fixture.repository,
        config_loader=lambda project_id: (release_fixture.config, "config-hash"),
        github_factory=lambda loaded: release_fixture.github,
        production_adapter=object(),
    )

    with pytest.raises(BlockedError, match="正式发布适配"):
        service.preflight("run-release")

    assert release_fixture.github.calls == []


def test_release_preflight_fails_closed_on_incomplete_remote_fact(
    release_fixture: SimpleNamespace,
) -> None:
    release_fixture.github.snapshot = SimpleNamespace(
        number=42,
        state="MERGED",
        head_sha=HEAD_SHA,
        checks_status="passed",
    )

    with pytest.raises(BlockedError, match="权威事实不完整"):
        release_fixture.service.preflight("run-release")


def test_release_execute_is_disabled_in_version_one(
    release_fixture: SimpleNamespace,
) -> None:
    release_fixture.config.production_enabled = True

    with pytest.raises(
        BlockedError,
        match="第一版未启用正式发布写操作",
    ):
        release_fixture.service.execute(
            "run-release",
            True,
            written_authorization="authorized",
        )

    assert release_fixture.repository.reads == []
    assert release_fixture.github.calls == []


class FakeReleaseService:
    def __init__(self) -> None:
        self.preflight_calls: list[str] = []
        self.execute_calls: list[str] = []

    def preflight(self, run_id: str) -> SimpleNamespace:
        self.preflight_calls.append(run_id)
        return SimpleNamespace(
            model_dump=lambda mode: {
                "eligible": True,
                "run_id": run_id,
                "pr_number": 42,
                "acceptance_sha": HEAD_SHA,
                "merge_commit_sha": MERGE_SHA,
                "checks_status": "passed",
                "production_adapter_disabled": True,
                "acceptance_evidence_sha256": EVIDENCE_SHA,
            }
        )

    def execute(self, run_id: str) -> None:
        self.execute_calls.append(run_id)
        raise AssertionError("CLI must not call execute")


def test_cli_release_calls_only_preflight_and_emits_json(tmp_path: Path) -> None:
    service = FakeReleaseService()
    output, error = StringIO(), StringIO()

    exit_code = main(
        ["release", "run-release", "--json"],
        services=CliServices(tmp_path, release_service=service),
        stdout=output,
        stderr=error,
    )

    assert exit_code == 0
    assert error.getvalue() == ""
    assert json.loads(output.getvalue()) == {
        "ok": True,
        "eligible": True,
        "run_id": "run-release",
        "pr_number": 42,
        "acceptance_sha": HEAD_SHA,
        "merge_commit_sha": MERGE_SHA,
        "checks_status": "passed",
        "production_adapter_disabled": True,
        "acceptance_evidence_sha256": EVIDENCE_SHA,
    }
    assert service.preflight_calls == ["run-release"]
    assert service.execute_calls == []


def test_cli_release_has_no_execute_switch(tmp_path: Path) -> None:
    service = FakeReleaseService()
    output, error = StringIO(), StringIO()

    exit_code = main(
        ["release", "run-release", "--execute", "--json"],
        services=CliServices(tmp_path, release_service=service),
        stdout=output,
        stderr=error,
    )

    assert exit_code == 2
    assert service.preflight_calls == []
    assert service.execute_calls == []
