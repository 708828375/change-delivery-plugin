from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from change_delivery.database import Database
from change_delivery.domain import EventRecord, RunState
from change_delivery.repository import RunRepository


TABLES = {
    "runs",
    "events",
    "approvals",
    "evidence",
    "remote_operations",
    "notifications",
}


@pytest.fixture
def database(tmp_path: Path) -> Database:
    result = Database(tmp_path / "state.sqlite3")
    result.migrate()
    return result


@pytest.fixture
def repository(database: Database) -> RunRepository:
    return RunRepository(database)


def test_migrate_is_idempotent_and_creates_six_tables(tmp_path: Path) -> None:
    database = Database(tmp_path / "nested" / "state.sqlite3")

    database.migrate()
    database.migrate()

    with database.connection() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert TABLES <= tables


def test_migrate_rejects_directory_as_database_path(tmp_path: Path) -> None:
    with pytest.raises(IsADirectoryError):
        Database(tmp_path).migrate()


def test_connections_enable_foreign_keys_and_close_after_context(
    database: Database,
) -> None:
    with database.connection() as connection:
        assert connection.row_factory is sqlite3.Row
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute(
                """
                INSERT INTO runs
                    (run_id, project_id, config_hash, state, created_at, updated_at)
                VALUES ('blocked', 'project', 'hash', 'DRAFT', 'now', 'now')
                """
            )

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_transaction_commits_on_success(database: Database) -> None:
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO runs
                (run_id, project_id, config_hash, state, created_at, updated_at)
            VALUES ('run-1', 'project', 'hash', 'DRAFT', 'now', 'now')
            """
        )

    with database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_transaction_rolls_back_on_exception_and_closes(database: Database) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO runs
                    (run_id, project_id, config_hash, state, created_at, updated_at)
                VALUES ('run-1', 'project', 'hash', 'DRAFT', 'now', 'now')
                """
            )
            raise RuntimeError("boom")

    with database.connection() as read_connection:
        assert read_connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_create_and_get_run(repository: RunRepository) -> None:
    created = repository.create_run("run-1", "project", "hash")

    loaded = repository.get_run("run-1")
    assert loaded == created
    assert loaded.state is RunState.DRAFT
    assert loaded.created_at.tzinfo is not None
    assert loaded.updated_at.tzinfo is not None


def test_duplicate_run_is_rejected(repository: RunRepository) -> None:
    repository.create_run("run-1", "project", "hash")

    with pytest.raises(sqlite3.IntegrityError):
        repository.create_run("run-1", "project", "hash")


def test_missing_run_raises_key_error(repository: RunRepository) -> None:
    with pytest.raises(KeyError, match="missing"):
        repository.get_run("missing")


def test_transition_updates_state_and_appends_json_evidence_event(
    repository: RunRepository,
) -> None:
    repository.create_run("run-1", "project", "hash")

    event = repository.transition(
        "run-1",
        RunState.DRAFT,
        RunState.APPROVED,
        "DESIGN_REQUESTED",
        ("evidence-2", "evidence-1"),
    )

    assert repository.get_run("run-1").state is RunState.APPROVED
    assert event.event_type == "DESIGN_REQUESTED"
    assert event.from_state is RunState.DRAFT
    assert event.to_state is RunState.APPROVED
    assert event.evidence_ids == ("evidence-2", "evidence-1")
    assert repository.list_events("run-1") == [event]


def test_stale_transition_changes_nothing(repository: RunRepository) -> None:
    repository.create_run("run-1", "project", "hash")

    with pytest.raises(RuntimeError, match="状态已变化"):
        repository.transition(
            "run-1",
            expected=RunState.APPROVED,
            target=RunState.IMPLEMENTING,
            event_type="IMPLEMENTATION_STARTED",
        )

    assert repository.get_run("run-1").state is RunState.DRAFT
    assert repository.list_events("run-1") == []


def test_transition_of_missing_run_raises_key_error(
    repository: RunRepository,
) -> None:
    with pytest.raises(KeyError, match="missing"):
        repository.transition(
            "missing",
            expected=RunState.DRAFT,
            target=RunState.APPROVED,
            event_type="DESIGN_APPROVED",
        )


def test_event_insert_failure_rolls_back_run_update(
    repository: RunRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository.create_run("run-1", "project", "hash")
    monkeypatch.setattr(
        "change_delivery.repository.new_event_id",
        lambda: "duplicate-event",
    )
    repository.transition(
        "run-1",
        expected=RunState.DRAFT,
        target=RunState.APPROVED,
        event_type="DESIGN_APPROVED",
    )

    with pytest.raises(sqlite3.IntegrityError):
        repository.transition(
            "run-1",
            expected=RunState.APPROVED,
            target=RunState.IMPLEMENTING,
            event_type="IMPLEMENTATION_STARTED",
        )

    assert repository.get_run("run-1").state is RunState.APPROVED
    assert len(repository.list_events("run-1")) == 1


def test_active_runs_exclude_terminal_states_and_are_stably_sorted(
    repository: RunRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "change_delivery.repository.utc_now",
        lambda: "2026-07-28T00:00:00+00:00",
    )
    for run_id in ("b", "a", "completed", "cancelled", "failed"):
        repository.create_run(run_id, "project", "hash")
    terminal_states = {
        "completed": RunState.COMPLETED,
        "cancelled": RunState.CANCELLED,
        "failed": RunState.FAILED_TERMINAL,
    }
    for run_id, state in terminal_states.items():
        repository.transition(
            run_id,
            expected=RunState.DRAFT,
            target=state,
            event_type="RUN_TERMINATED",
        )

    assert [run.run_id for run in repository.list_active_runs()] == ["a", "b"]


def test_active_runs_exclude_ready_to_merge(repository: RunRepository) -> None:
    repository.create_run("ready", "project", "hash")
    with repository.database.transaction() as connection:
        connection.execute(
            "UPDATE runs SET state = ? WHERE run_id = ?",
            (RunState.READY_TO_MERGE.value, "ready"),
        )

    assert repository.list_active_runs() == []


def test_event_record_rejects_invalid_evidence_json() -> None:
    common = {
        "event_id": "event",
        "run_id": "run",
        "event_type": "transition",
        "from_state": RunState.DRAFT,
        "to_state": RunState.APPROVED,
        "created_at": datetime.now().astimezone(),
    }

    with pytest.raises(ValidationError):
        EventRecord.model_validate({**common, "evidence_json": "not-json"})
    with pytest.raises(ValidationError):
        EventRecord.model_validate({**common, "evidence_json": json.dumps({"x": 1})})


def test_event_record_evidence_json_roundtrip_is_immutable() -> None:
    record = EventRecord.model_validate(
        {
            "event_id": "event",
            "run_id": "run",
            "event_type": "transition",
            "from_state": "DRAFT",
            "to_state": "APPROVED",
            "evidence_json": '["one", "two"]',
            "created_at": datetime.now().astimezone().isoformat(),
        }
    )

    assert record.evidence_ids == ("one", "two")
    assert isinstance(record.evidence_ids, tuple)
