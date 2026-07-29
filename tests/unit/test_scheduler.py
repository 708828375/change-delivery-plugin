from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from filelock import FileLock

from change_delivery.database import Database
from change_delivery.domain import RunState, StepOutcome
from change_delivery.repository import RunRepository
from change_delivery.scheduler import Scheduler


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


class FakeOrchestrator:
    def __init__(self, outcomes: dict[str, StepOutcome] | None = None) -> None:
        self.calls: list[str] = []
        self.outcomes = outcomes or {}

    def step(self, run_id: str) -> StepOutcome:
        self.calls.append(run_id)
        return self.outcomes.get(
            run_id,
            StepOutcome(state=RunState.DRAFT, action="advanced"),
        )


def make_scheduler(
    tmp_path: Path,
    *,
    clock=lambda: NOW,
    outcomes: dict[str, StepOutcome] | None = None,
) -> SimpleNamespace:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    repository = RunRepository(database)
    orchestrator = FakeOrchestrator(outcomes)
    scheduler = Scheduler(
        runtime_root=tmp_path,
        repository=repository,
        orchestrator=orchestrator,
        clock=clock,
    )
    return SimpleNamespace(
        database=database,
        repository=repository,
        orchestrator=orchestrator,
        scheduler=scheduler,
    )


def backdate_run(
    fixture: SimpleNamespace,
    run_id: str,
    *,
    hours: float,
) -> None:
    timestamp = (NOW - timedelta(hours=hours)).isoformat()
    with fixture.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET created_at = ?, updated_at = ? WHERE run_id = ?",
            (timestamp, timestamp, run_id),
        )


def test_scheduler_skips_when_lock_is_held(tmp_path: Path) -> None:
    fixture = make_scheduler(tmp_path)
    lock = FileLock(tmp_path / "tick.lock", timeout=0)
    lock.acquire()
    try:
        result = fixture.scheduler.tick()
    finally:
        lock.release()

    assert result.status == "already_running"
    assert result.processed == 0


def test_scheduler_does_not_invoke_orchestrator_without_active_runs(
    tmp_path: Path,
) -> None:
    fixture = make_scheduler(tmp_path)

    result = fixture.scheduler.tick()

    assert result.status == "idle"
    assert fixture.orchestrator.calls == []


def test_recent_run_uses_five_minute_poll_window(tmp_path: Path) -> None:
    current = [NOW]
    fixture = make_scheduler(tmp_path, clock=lambda: current[0])
    fixture.repository.create_run("run-1", "project", "hash")

    first = fixture.scheduler.tick()
    current[0] += timedelta(minutes=4, seconds=59)
    throttled = fixture.scheduler.tick()
    current[0] += timedelta(seconds=1)
    due = fixture.scheduler.tick()

    assert first.status == "processed"
    assert throttled.status == "throttled"
    assert due.status == "processed"
    assert fixture.orchestrator.calls == ["run-1", "run-1"]


def test_24_to_72_hour_run_uses_thirty_minute_window(
    tmp_path: Path,
) -> None:
    current = [NOW]
    fixture = make_scheduler(tmp_path, clock=lambda: current[0])
    fixture.repository.create_run("run-1", "project", "hash")
    backdate_run(fixture, "run-1", hours=25)

    fixture.scheduler.tick()
    current[0] += timedelta(minutes=29, seconds=59)
    throttled = fixture.scheduler.tick()
    current[0] += timedelta(seconds=1)
    due = fixture.scheduler.tick()

    assert throttled.status == "throttled"
    assert due.status == "processed"
    assert fixture.orchestrator.calls == ["run-1", "run-1"]


def test_scheduler_marks_72_hour_run_timed_out_with_safe_summary(
    tmp_path: Path,
) -> None:
    fixture = make_scheduler(tmp_path)
    fixture.repository.create_run("run-1", "project", "hash")
    backdate_run(fixture, "run-1", hours=73)

    result = fixture.scheduler.tick()

    run = fixture.repository.get_run("run-1")
    events = fixture.repository.list_events("run-1")
    evidence = fixture.repository.list_evidence("run-1")
    assert result.status == "blocked"
    assert run.state is RunState.BLOCKED
    assert events[-1].event_type == "SCHEDULER_INACTIVITY_TIMEOUT"
    assert events[-1].evidence_ids == (evidence[-1].evidence_id,)
    assert evidence[-1].kind == "scheduler_timeout"
    assert "73" not in evidence[-1].payload_json
    assert fixture.orchestrator.calls == []


def test_one_tick_advances_only_one_run_by_one_action(tmp_path: Path) -> None:
    fixture = make_scheduler(tmp_path)
    fixture.repository.create_run("run-1", "project", "hash")
    fixture.repository.create_run("run-2", "project", "hash")

    result = fixture.scheduler.tick()

    assert result.processed == 1
    assert fixture.orchestrator.calls == ["run-1"]


def test_process_timeout_is_recorded_as_blocked(tmp_path: Path) -> None:
    timeout = StepOutcome(
        state=RunState.IMPLEMENTING,
        action="process_timeout",
        blocked_reason="ProcessTimeoutError: operation timed out",
    )
    fixture = make_scheduler(tmp_path, outcomes={"run-1": timeout})
    fixture.repository.create_run("run-1", "project", "hash")
    with fixture.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET state = ? WHERE run_id = ?",
            (RunState.IMPLEMENTING.value, "run-1"),
        )

    result = fixture.scheduler.tick()

    assert result.status == "blocked"
    assert fixture.repository.get_run("run-1").state is RunState.BLOCKED
    evidence = fixture.repository.list_evidence("run-1")[-1]
    assert evidence.kind == "process_timeout"
    assert "ProcessTimeoutError" not in evidence.payload_json


def test_blocked_runs_are_not_automatically_resumed(tmp_path: Path) -> None:
    fixture = make_scheduler(tmp_path)
    fixture.repository.create_run("run-1", "project", "hash")
    with fixture.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET state = ? WHERE run_id = ?",
            (RunState.BLOCKED.value, "run-1"),
        )

    result = fixture.scheduler.tick()

    assert result.status == "waiting_for_intervention"
    assert fixture.orchestrator.calls == []


def test_scheduler_rejects_tick_lock_directory(tmp_path: Path) -> None:
    (tmp_path / "tick.lock").mkdir()

    fixture = make_scheduler(tmp_path)
    result = fixture.scheduler.tick()

    assert result.status == "unsafe_lock"
    assert fixture.orchestrator.calls == []
