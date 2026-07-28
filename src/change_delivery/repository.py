from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from change_delivery.database import Database
from change_delivery.domain import EventRecord, RunRecord, RunState


TERMINAL_STATES = (
    RunState.COMPLETED,
    RunState.CANCELLED,
    RunState.FAILED_TERMINAL,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_event_id() -> str:
    return str(uuid4())


class RunRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_run(
        self,
        run_id: str,
        project_id: str,
        config_hash: str,
    ) -> RunRecord:
        timestamp = utc_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, project_id, config_hash, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    project_id,
                    config_hash,
                    RunState.DRAFT.value,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        assert row is not None
        return RunRecord.model_validate(dict(row))

    def get_run(self, run_id: str) -> RunRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return RunRecord.model_validate(dict(row))

    def list_active_runs(self) -> list[RunRecord]:
        placeholders = ", ".join("?" for _ in TERMINAL_STATES)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM runs
                WHERE state NOT IN ({placeholders})
                ORDER BY created_at, run_id
                """,
                tuple(state.value for state in TERMINAL_STATES),
            ).fetchall()
        return [RunRecord.model_validate(dict(row)) for row in rows]

    def transition(
        self,
        run_id: str,
        expected: RunState,
        target: RunState,
        event_type: str,
        evidence_ids: tuple[str, ...] = (),
    ) -> EventRecord:
        timestamp = utc_now()
        event_id = new_event_id()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            current = RunState(row["state"])
            if current is not expected:
                raise RuntimeError(
                    f"运行 {run_id} 状态已变化：期望 {expected.value}，"
                    f"实际 {current.value}"
                )
            connection.execute(
                "UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ?",
                (target.value, timestamp, run_id),
            )
            connection.execute(
                """
                INSERT INTO events (
                    event_id, run_id, event_type, from_state, to_state,
                    evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    run_id,
                    event_type,
                    current.value,
                    target.value,
                    json.dumps(evidence_ids, ensure_ascii=False),
                    timestamp,
                ),
            )
            event_row = connection.execute(
                "SELECT * FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        assert event_row is not None
        return EventRecord.model_validate(dict(event_row))

    def list_events(self, run_id: str) -> list[EventRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM events
                WHERE run_id = ?
                ORDER BY created_at, event_id
                """,
                (run_id,),
            ).fetchall()
        return [EventRecord.model_validate(dict(row)) for row in rows]
