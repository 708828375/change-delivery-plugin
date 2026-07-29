from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from change_delivery.database import Database
from change_delivery.domain import (
    EvidenceRecord,
    EvidenceRef,
    EventRecord,
    RemoteOperationRecord,
    RunRecord,
    RunState,
    StageResult,
)
from change_delivery.evidence import sha256_payload
from change_delivery.state_machine import assert_transition


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

    def register_evidence(
        self,
        run_id: str,
        evidence_id: str,
        kind: str,
        payload: object,
    ) -> EvidenceRef:
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        digest = sha256_payload(payload)
        timestamp = utc_now()
        with self.database.transaction() as connection:
            self._register_evidence_row(
                connection,
                run_id,
                evidence_id,
                kind,
                payload_json,
                digest,
                timestamp,
            )
        return EvidenceRef(evidence_id=evidence_id, kind=kind, sha256=digest)

    @staticmethod
    def _register_evidence_row(
        connection: sqlite3.Connection,
        run_id: str,
        evidence_id: str,
        kind: str,
        payload_json: str,
        digest: str,
        timestamp: str,
    ) -> None:
        existing = connection.execute(
            "SELECT * FROM evidence WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchone()
        if existing is not None:
            if (
                existing["run_id"] != run_id
                or existing["kind"] != kind
                or existing["payload_json"] != payload_json
                or existing["sha256"] != digest
            ):
                raise ValueError("evidence identity conflict")
            return
        connection.execute(
            """
            INSERT INTO evidence (
                evidence_id, run_id, kind, payload_json, sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (evidence_id, run_id, kind, payload_json, digest, timestamp),
        )

    def get_evidence(self, evidence_id: str) -> EvidenceRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
        if row is None:
            raise KeyError(evidence_id)
        return EvidenceRecord.model_validate(dict(row))

    def list_evidence(self, run_id: str) -> list[EvidenceRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM evidence WHERE run_id = ?
                ORDER BY created_at, evidence_id
                """,
                (run_id,),
            ).fetchall()
        return [EvidenceRecord.model_validate(dict(row)) for row in rows]

    def rebind_primary_stage_result(
        self,
        run_id: str,
        result: StageResult,
        *,
        primary: bool,
    ) -> tuple[EvidenceRef, ...]:
        if not primary:
            raise ValueError("format repair result cannot authorize a transition")
        rebound: list[EvidenceRef] = []
        with self.database.connection() as connection:
            for reference in result.evidence:
                row = connection.execute(
                    "SELECT * FROM evidence WHERE evidence_id = ?",
                    (reference.evidence_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("primary result evidence is missing")
                if row["run_id"] != run_id:
                    raise ValueError("primary result evidence belongs to another run")
                if (
                    row["kind"] != reference.kind
                    or row["sha256"] != reference.sha256
                    or sha256_payload(json.loads(row["payload_json"]))
                    != reference.sha256
                ):
                    raise ValueError("primary result evidence digest mismatch")
                rebound.append(reference)
        return tuple(rebound)

    def transition_with_evidence(
        self,
        run_id: str,
        expected: RunState,
        target: RunState,
        event_type: str,
        evidence_refs: tuple[EvidenceRef, ...] = (),
        *,
        interrupted_from: RunState | None = None,
        approval_snapshot_sha256: str | None = None,
        run_updates: dict[str, object] | None = None,
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
                raise RuntimeError("run state changed")
            rebound: list[EvidenceRef] = []
            for reference in evidence_refs:
                evidence = connection.execute(
                    "SELECT * FROM evidence WHERE evidence_id = ?",
                    (reference.evidence_id,),
                ).fetchone()
                if evidence is None:
                    raise ValueError("evidence is missing")
                if evidence["run_id"] != run_id:
                    raise ValueError("evidence does not belong to run")
                if (
                    evidence["kind"] != reference.kind
                    or evidence["sha256"] != reference.sha256
                    or sha256_payload(json.loads(evidence["payload_json"]))
                    != reference.sha256
                ):
                    raise ValueError("evidence digest mismatch")
                rebound.append(reference)
            assert_transition(
                current,
                target,
                (reference.kind for reference in rebound),
                interrupted_from=interrupted_from,
                evidence_refs=rebound,
                approval_snapshot_sha256=approval_snapshot_sha256,
            )
            updates = {"state": target.value, "updated_at": timestamp}
            if run_updates:
                if not set(run_updates) <= {"head_sha", "pr_number"}:
                    raise ValueError("unsupported run update")
                updates.update(run_updates)
            assignments = ", ".join(f"{name} = ?" for name in updates)
            connection.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ?",
                (*updates.values(), run_id),
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
                    json.dumps(
                        [reference.evidence_id for reference in rebound],
                        ensure_ascii=False,
                    ),
                    timestamp,
                ),
            )
            event_row = connection.execute(
                "SELECT * FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        assert event_row is not None
        return EventRecord.model_validate(dict(event_row))

    def begin_remote_operation(
        self,
        operation_key: str,
        run_id: str,
        operation_type: str,
        remote_identity: str,
    ) -> RemoteOperationRecord:
        timestamp = utc_now()
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM remote_operations WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["run_id"] != run_id
                    or existing["operation_type"] != operation_type
                    or existing["remote_identity"] != remote_identity
                ):
                    raise ValueError("remote operation identity conflict")
                return RemoteOperationRecord.model_validate(dict(existing))
            connection.execute(
                """
                INSERT INTO remote_operations (
                    operation_key, run_id, operation_type, remote_identity,
                    status, updated_at
                ) VALUES (?, ?, ?, ?, 'PENDING', ?)
                """,
                (
                    operation_key,
                    run_id,
                    operation_type,
                    remote_identity,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM remote_operations WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
        assert row is not None
        return RemoteOperationRecord.model_validate(dict(row))

    def get_remote_operation(self, operation_key: str) -> RemoteOperationRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM remote_operations WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
        if row is None:
            raise KeyError(operation_key)
        return RemoteOperationRecord.model_validate(dict(row))

    def list_remote_operations(self, run_id: str) -> list[RemoteOperationRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM remote_operations WHERE run_id = ?
                ORDER BY updated_at, operation_key
                """,
                (run_id,),
            ).fetchall()
        return [RemoteOperationRecord.model_validate(dict(row)) for row in rows]

    def _finish_remote_operation(
        self,
        operation_key: str,
        target: str,
    ) -> RemoteOperationRecord:
        timestamp = utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM remote_operations WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            if row is None:
                raise KeyError(operation_key)
            if row["status"] == target:
                return RemoteOperationRecord.model_validate(dict(row))
            if row["status"] != "PENDING":
                raise ValueError("remote operation is terminal")
            connection.execute(
                """
                UPDATE remote_operations SET status = ?, updated_at = ?
                WHERE operation_key = ?
                """,
                (target, timestamp, operation_key),
            )
            updated = connection.execute(
                "SELECT * FROM remote_operations WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
        assert updated is not None
        return RemoteOperationRecord.model_validate(dict(updated))

    def confirm_remote_operation(
        self,
        operation_key: str,
    ) -> RemoteOperationRecord:
        return self._finish_remote_operation(operation_key, "CONFIRMED")

    def fail_remote_operation(self, operation_key: str) -> RemoteOperationRecord:
        return self._finish_remote_operation(operation_key, "FAILED")

    def approve_with_evidence(
        self,
        run_id: str,
        *,
        approval_payload: object,
        approval_sha256: str,
        evidence_id: str,
    ) -> EventRecord:
        payload_json = json.dumps(
            approval_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        if sha256_payload(approval_payload) != approval_sha256:
            raise ValueError("approval file digest mismatch")
        if (
            not isinstance(approval_payload, dict)
            or not isinstance(approval_payload.get("base_sha"), str)
            or len(approval_payload["base_sha"]) != 40
        ):
            raise ValueError("approval base SHA is invalid")
        base_sha = approval_payload["base_sha"].lower()
        timestamp = utc_now()
        event_id = new_event_id()
        reference = EvidenceRef(
            evidence_id=evidence_id,
            kind="explicit_approval",
            sha256=approval_sha256,
        )
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT state FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            current = RunState(row["state"])
            assert_transition(
                current,
                RunState.APPROVED,
                ("explicit_approval",),
                evidence_refs=(reference,),
                approval_snapshot_sha256=approval_sha256,
            )
            self._register_evidence_row(
                connection,
                run_id,
                evidence_id,
                "explicit_approval",
                payload_json,
                approval_sha256,
                timestamp,
            )
            connection.execute(
                """
                INSERT INTO approvals (run_id, approval_json, approved_at)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO NOTHING
                """,
                (run_id, payload_json, timestamp),
            )
            connection.execute(
                """
                UPDATE runs SET state = ?, head_sha = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (RunState.APPROVED.value, base_sha, timestamp, run_id),
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
                    "DESIGN_APPROVED",
                    current.value,
                    RunState.APPROVED.value,
                    json.dumps([evidence_id]),
                    timestamp,
                ),
            )
            event_row = connection.execute(
                "SELECT * FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        assert event_row is not None
        return EventRecord.model_validate(dict(event_row))
