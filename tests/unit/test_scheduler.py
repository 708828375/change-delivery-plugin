from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from filelock import FileLock
import pytest

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
        configured_timeout_seconds=321,
        operation_hash="a" * 64,
        command_hash="b" * 64,
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
    payload = json.loads(evidence.payload_json)
    assert "threshold_hours" not in payload
    assert payload["configured_timeout_seconds"] == 321
    assert payload["operation_hash"] == "a" * 64
    assert payload["command_hash"] == "b" * 64


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


class NoopLock:
    def acquire(self, *, timeout: float) -> "NoopLock":
        return self

    def __enter__(self) -> "NoopLock":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_database_claim_allows_only_one_concurrent_scheduler_step(
    tmp_path: Path,
) -> None:
    fixture = make_scheduler(tmp_path)
    fixture.repository.create_run("run-1", "project", "hash")
    calls: list[str] = []
    calls_lock = threading.Lock()

    class SlowOrchestrator:
        def step(self, run_id: str) -> StepOutcome:
            with calls_lock:
                calls.append(run_id)
            time.sleep(0.2)
            return StepOutcome(state=RunState.DRAFT, action="advanced")

    schedulers = [
        Scheduler(
            runtime_root=tmp_path,
            repository=fixture.repository,
            orchestrator=SlowOrchestrator(),
            clock=lambda: NOW,
        )
        for _ in range(2)
    ]
    (tmp_path / "tick.lock").touch()
    for scheduler in schedulers:
        scheduler.lock = NoopLock()  # type: ignore[assignment]
        scheduler._validate_held_lock_identity = lambda expected=None: None  # type: ignore[method-assign]
    threads = [threading.Thread(target=scheduler.tick) for scheduler in schedulers]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == ["run-1"]


def test_timeout_cas_rejects_concurrent_activity_without_evidence(
    tmp_path: Path,
) -> None:
    fixture = make_scheduler(tmp_path)
    observed = fixture.repository.create_run("run-1", "project", "hash")
    backdate_run(fixture, "run-1", hours=73)
    observed = fixture.repository.get_run("run-1")
    with fixture.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET updated_at = ? WHERE run_id = ?",
            (NOW.isoformat(), "run-1"),
        )

    event = fixture.repository.block_for_scheduler(
        "run-1",
        RunState.DRAFT,
        observed_updated_at=observed.updated_at,
        inactivity_threshold=timedelta(hours=72),
        evidence_kind="scheduler_timeout",
        event_type="SCHEDULER_INACTIVITY_TIMEOUT",
        reason_code="inactivity_limit_exceeded",
        occurred_at=NOW,
    )

    assert event is None
    assert fixture.repository.get_run("run-1").state is RunState.DRAFT
    assert fixture.repository.list_evidence("run-1") == []


def test_future_poll_claim_is_monotonic_and_does_not_duplicate(
    tmp_path: Path,
) -> None:
    fixture = make_scheduler(tmp_path)
    run = fixture.repository.create_run("run-1", "project", "hash")
    future = NOW + timedelta(days=30)
    fixture.repository.record_scheduler_poll("run-1", future)

    first = fixture.repository.claim_scheduler_run(
        run,
        now=NOW,
        window=timedelta(minutes=5),
    )
    second = fixture.repository.claim_scheduler_run(
        run,
        now=NOW,
        window=timedelta(minutes=5),
    )

    assert first is True
    assert second is False
    assert fixture.repository.get_scheduler_poll_at("run-1") >= future


def test_tick_script_uses_frozen_isolated_runtime_host() -> None:
    script = (
        Path(__file__).parents[2] / "scripts" / "run-tick.ps1"
    ).read_text(encoding="utf-8")

    assert "runtime-host.json" in script
    assert "PYTHONPATH = $null" in script
    assert "PYTHONHOME = $null" in script
    assert "& $pythonPath -I" in script
    assert "python -m change_delivery" not in script


@pytest.mark.skipif(os.name != "nt", reason="Windows lock sharing semantics")
def test_tick_holds_non_replaceable_lock_handle(tmp_path: Path) -> None:
    fixture = make_scheduler(tmp_path)
    fixture.repository.create_run("run-1", "project", "hash")
    replaced = []

    class Replacer:
        def step(self, run_id: str) -> StepOutcome:
            try:
                (tmp_path / "tick.lock").unlink()
                (tmp_path / "tick.lock").write_text("replacement")
                replaced.append(True)
            except OSError:
                replaced.append(False)
            return StepOutcome(state=RunState.DRAFT, action="advanced")

    scheduler = Scheduler(
        runtime_root=tmp_path,
        repository=fixture.repository,
        orchestrator=Replacer(),
        clock=lambda: NOW,
    )

    scheduler.tick()

    assert replaced == [False]


def timeout_outcome(state: RunState) -> StepOutcome:
    return StepOutcome(
        state=state,
        action="process_timeout",
        blocked_reason="configured process timeout exceeded",
        configured_timeout_seconds=321,
        operation_hash="a" * 64,
        command_hash="b" * 64,
    )


def test_process_timeout_does_not_overwrite_terminal_state_from_step(
    tmp_path: Path,
) -> None:
    fixture = make_scheduler(tmp_path)
    fixture.repository.create_run("run-1", "project", "hash")

    class TerminalOrchestrator:
        def step(self, run_id: str) -> StepOutcome:
            fixture.repository.transition(
                run_id,
                RunState.DRAFT,
                RunState.CANCELLED,
                "MANUAL_CANCEL",
            )
            return timeout_outcome(RunState.DRAFT)

    scheduler = Scheduler(
        runtime_root=tmp_path,
        repository=fixture.repository,
        orchestrator=TerminalOrchestrator(),
        clock=lambda: NOW,
    )

    result = scheduler.tick()

    assert result.status == "concurrent_state_change"
    assert result.current_state is RunState.CANCELLED
    assert fixture.repository.get_run("run-1").state is RunState.CANCELLED
    assert fixture.repository.list_evidence("run-1") == []


def test_process_timeout_cas_loss_reports_authoritative_terminal_state(
    tmp_path: Path,
) -> None:
    fixture = make_scheduler(tmp_path)
    fixture.repository.create_run("run-1", "project", "hash")

    class RacingRepository:
        def __getattr__(self, name: str) -> object:
            return getattr(fixture.repository, name)

        def block_for_scheduler(self, run_id: str, expected: RunState, **kwargs: object):
            fixture.repository.transition(
                run_id,
                expected,
                RunState.CANCELLED,
                "MANUAL_CANCEL",
            )
            return fixture.repository.block_for_scheduler(
                run_id,
                expected,
                **kwargs,
            )

    scheduler = Scheduler(
        runtime_root=tmp_path,
        repository=RacingRepository(),  # type: ignore[arg-type]
        orchestrator=FakeOrchestrator(
            {"run-1": timeout_outcome(RunState.DRAFT)}
        ),
        clock=lambda: NOW,
    )

    result = scheduler.tick()

    assert result.status == "concurrent_state_change"
    assert result.current_state is RunState.CANCELLED
    assert fixture.repository.get_run("run-1").state is RunState.CANCELLED
    assert fixture.repository.list_evidence("run-1") == []
