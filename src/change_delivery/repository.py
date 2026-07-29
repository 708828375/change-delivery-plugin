from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from change_delivery.database import Database
from change_delivery.approval import ApprovalSnapshot
from change_delivery.approval import REQUIRED_ARTIFACT_NAMES
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

    def get_scheduler_poll_at(self, run_id: str) -> datetime | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT polled_at FROM scheduler_polls WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row["polled_at"])

    def record_scheduler_poll(
        self,
        run_id: str,
        polled_at: datetime,
    ) -> None:
        if polled_at.tzinfo is None:
            raise ValueError("scheduler poll timestamp must be timezone-aware")
        timestamp = polled_at.astimezone(UTC).isoformat()
        with self.database.transaction() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                is None
            ):
                raise KeyError(run_id)
            connection.execute(
                """
                INSERT INTO scheduler_polls (
                    run_id, polled_at, effective_at, generation
                ) VALUES (?, ?, ?, 1)
                ON CONFLICT(run_id) DO UPDATE SET
                    polled_at = MAX(scheduler_polls.polled_at, excluded.polled_at),
                    effective_at = excluded.effective_at,
                    generation = scheduler_polls.generation + 1
                """,
                (run_id, timestamp, timestamp),
            )

    def claim_scheduler_run(
        self,
        observed: RunRecord,
        *,
        now: datetime,
        window: object,
    ) -> bool:
        from datetime import timedelta

        if now.tzinfo is None or not isinstance(window, timedelta):
            raise ValueError("scheduler claim requires aware time and timedelta")
        if window <= timedelta(0):
            raise ValueError("scheduler claim window must be positive")
        now = now.astimezone(UTC)
        timestamp = now.isoformat()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT state, updated_at FROM runs WHERE run_id = ?",
                (observed.run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(observed.run_id)
            if (
                RunState(row["state"]) is not observed.state
                or datetime.fromisoformat(row["updated_at"])
                != observed.updated_at
            ):
                return False
            poll = connection.execute(
                """
                SELECT polled_at, effective_at, generation
                FROM scheduler_polls WHERE run_id = ?
                """,
                (observed.run_id,),
            ).fetchone()
            if poll is not None:
                effective = datetime.fromisoformat(
                    poll["effective_at"] or poll["polled_at"]
                ).astimezone(UTC)
                if effective <= now and now - effective < window:
                    return False
                connection.execute(
                    """
                    UPDATE scheduler_polls SET
                        polled_at = MAX(polled_at, ?),
                        effective_at = ?,
                        generation = generation + 1
                    WHERE run_id = ? AND generation = ?
                    """,
                    (
                        timestamp,
                        timestamp,
                        observed.run_id,
                        poll["generation"],
                    ),
                )
                return connection.execute(
                    "SELECT changes()"
                ).fetchone()[0] == 1
            try:
                connection.execute(
                    """
                    INSERT INTO scheduler_polls (
                        run_id, polled_at, effective_at, generation
                    ) VALUES (?, ?, ?, 1)
                    """,
                    (observed.run_id, timestamp, timestamp),
                )
            except sqlite3.IntegrityError:
                return False
            return True

    def block_for_scheduler(
        self,
        run_id: str,
        expected: RunState,
        *,
        evidence_kind: str,
        event_type: str,
        reason_code: str,
        occurred_at: datetime,
        observed_updated_at: datetime | None = None,
        inactivity_threshold: object | None = None,
        evidence_details: dict[str, object] | None = None,
    ) -> EventRecord | None:
        from datetime import timedelta

        if occurred_at.tzinfo is None:
            raise ValueError("scheduler timestamp must be timezone-aware")
        timestamp = occurred_at.astimezone(UTC).isoformat()
        evidence_id = f"{evidence_kind}:{uuid4().hex}"
        payload: dict[str, object] = {
            "reason": reason_code,
            "safe_summary": "automatic processing stopped; manual review required",
        }
        if evidence_kind == "scheduler_timeout":
            payload["threshold_hours"] = 72
        if evidence_details:
            payload.update(evidence_details)
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        digest = sha256_payload(payload)
        reference = EvidenceRef(
            evidence_id=evidence_id,
            kind=evidence_kind,
            sha256=digest,
        )
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
                return None
            if observed_updated_at is not None:
                stored_updated_at = connection.execute(
                    "SELECT updated_at FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()["updated_at"]
                activity_at = datetime.fromisoformat(stored_updated_at)
                if activity_at != observed_updated_at:
                    return None
                if inactivity_threshold is not None:
                    threshold = inactivity_threshold
                    if not isinstance(threshold, timedelta):
                        raise ValueError("inactivity threshold must be timedelta")
                    if (
                        occurred_at.astimezone(UTC)
                        - activity_at.astimezone(UTC)
                        <= threshold
                    ):
                        return None
            assert_transition(
                current,
                RunState.BLOCKED,
                (evidence_kind,),
                evidence_refs=(reference,),
            )
            self._register_evidence_row(
                connection,
                run_id,
                evidence_id,
                evidence_kind,
                payload_json,
                digest,
                timestamp,
            )
            connection.execute(
                "UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ?",
                (RunState.BLOCKED.value, timestamp, run_id),
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
                    RunState.BLOCKED.value,
                    json.dumps((evidence_id,), ensure_ascii=False),
                    timestamp,
                ),
            )
            event_row = connection.execute(
                "SELECT * FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        assert event_row is not None
        return EventRecord.model_validate(dict(event_row))

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

    def get_approval_binding(
        self,
        run_id: str,
    ) -> tuple[str, EvidenceRecord]:
        with self.database.connection() as connection:
            approval = connection.execute(
                "SELECT approval_json FROM approvals WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if approval is None:
                raise KeyError(f"approval:{run_id}")
            rows = connection.execute(
                """
                SELECT e.* FROM evidence e
                WHERE e.run_id = ? AND e.kind = 'explicit_approval'
                ORDER BY e.created_at, e.evidence_id
                """,
                (run_id,),
            ).fetchall()
            events = connection.execute(
                """
                SELECT evidence_json FROM events
                WHERE run_id = ? AND to_state = ?
                """,
                (run_id, RunState.APPROVED.value),
            ).fetchall()
        if len(rows) != 1:
            raise ValueError("approval evidence binding is missing or ambiguous")
        evidence = EvidenceRecord.model_validate(dict(rows[0]))
        if not any(
            evidence.evidence_id in json.loads(event["evidence_json"])
            for event in events
        ):
            raise ValueError("approval evidence is not bound to approval event")
        return approval["approval_json"], evidence

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
        approved_artifacts: dict[str, bytes],
    ) -> EventRecord:
        if re.fullmatch(r"[0-9a-f]{64}", approval_sha256) is None:
            raise ValueError("approval SHA-256 is invalid")
        snapshot = ApprovalSnapshot.model_validate(approval_payload)
        if snapshot.run_id != run_id:
            raise ValueError("approval run identity mismatch")
        if (
            snapshot.base_sha != snapshot.base_sha.lower()
            or re.fullmatch(r"[0-9a-f]{40}", snapshot.base_sha) is None
        ):
            raise ValueError("approval base SHA is invalid")
        payload_json = json.dumps(
            approval_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        if sha256_payload(approval_payload) != approval_sha256:
            raise ValueError("approval file digest mismatch")
        base_sha = snapshot.base_sha
        if set(approved_artifacts) != set(REQUIRED_ARTIFACT_NAMES):
            raise ValueError("approved artifact snapshot is incomplete")
        artifact_rows = tuple(
            (
                name,
                approved_artifacts[name],
                hashlib.sha256(approved_artifacts[name]).hexdigest(),
            )
            for name in REQUIRED_ARTIFACT_NAMES
        )
        if any(
            digest != snapshot.artifact_hashes[name]
            for name, _contents, digest in artifact_rows
        ):
            raise ValueError("approved artifact snapshot digest mismatch")
        timestamp = utc_now()
        event_id = new_event_id()
        reference = EvidenceRef(
            evidence_id=evidence_id,
            kind="explicit_approval",
            sha256=approval_sha256,
        )
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT state, project_id, config_hash, head_sha
                FROM runs WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            current = RunState(row["state"])
            if (
                snapshot.project_id != row["project_id"]
                or snapshot.config_hash != row["config_hash"]
            ):
                raise ValueError("approval project or config identity mismatch")
            if current is RunState.APPROVED:
                approval = connection.execute(
                    "SELECT approval_json FROM approvals WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                evidence = connection.execute(
                    "SELECT * FROM evidence WHERE evidence_id = ?",
                    (evidence_id,),
                ).fetchone()
                events = connection.execute(
                    """
                    SELECT * FROM events
                    WHERE run_id = ? AND event_type = 'DESIGN_APPROVED'
                    ORDER BY created_at, event_id
                    """,
                    (run_id,),
                ).fetchall()
                stored_artifacts = connection.execute(
                    """
                    SELECT artifact_name, content, sha256, approval_sha256
                    FROM approved_artifacts WHERE run_id = ?
                    ORDER BY artifact_name
                    """,
                    (run_id,),
                ).fetchall()
                matching_events = [
                    event
                    for event in events
                    if json.loads(event["evidence_json"]) == [evidence_id]
                ]
                if (
                    approval is None
                    or approval["approval_json"] != payload_json
                    or evidence is None
                    or evidence["run_id"] != run_id
                    or evidence["kind"] != "explicit_approval"
                    or evidence["payload_json"] != payload_json
                    or evidence["sha256"] != approval_sha256
                    or row["head_sha"] != base_sha
                    or len(matching_events) != 1
                    or {
                        item["artifact_name"]: (
                            bytes(item["content"]),
                            item["sha256"],
                            item["approval_sha256"],
                        )
                        for item in stored_artifacts
                    }
                    != {
                        name: (contents, digest, approval_sha256)
                        for name, contents, digest in artifact_rows
                    }
                ):
                    raise ValueError("approved run has a conflicting audit binding")
                return EventRecord.model_validate(dict(matching_events[0]))
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
            existing_approval = connection.execute(
                "SELECT approval_json FROM approvals WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if existing_approval is not None:
                raise ValueError("approval row exists before approval transition")
            connection.execute(
                """
                INSERT INTO approvals (run_id, approval_json, approved_at)
                VALUES (?, ?, ?)
                """,
                (run_id, payload_json, timestamp),
            )
            connection.executemany(
                """
                INSERT INTO approved_artifacts (
                    run_id, artifact_name, content, sha256, approval_sha256
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (run_id, name, contents, digest, approval_sha256)
                    for name, contents, digest in artifact_rows
                ),
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

    def get_approved_artifacts(self, run_id: str) -> dict[str, bytes]:
        approval_json, evidence = self.get_approval_binding(run_id)
        approval_hash = sha256_payload(json.loads(approval_json))
        if evidence.sha256 != approval_hash:
            raise ValueError("approval artifact binding digest is inconsistent")
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT artifact_name, content, sha256, approval_sha256
                FROM approved_artifacts WHERE run_id = ?
                """,
                (run_id,),
            ).fetchall()
        captured = {
            row["artifact_name"]: bytes(row["content"])
            for row in rows
        }
        if set(captured) != set(REQUIRED_ARTIFACT_NAMES):
            raise ValueError("approved artifact snapshot is incomplete")
        for row in rows:
            if (
                row["approval_sha256"] != approval_hash
                or hashlib.sha256(bytes(row["content"])).hexdigest()
                != row["sha256"]
            ):
                raise ValueError("approved artifact snapshot is corrupt")
        return captured

    def complete_commit_with_review(
        self,
        run_id: str,
        *,
        operation_key: str,
        expected_parent_sha: str | None,
        new_head_sha: str,
        commit_tree_sha: str,
        source_evidence_ids: tuple[str, ...],
        attestation_id: str,
    ) -> tuple[EvidenceRef, EvidenceRef]:
        derived_payload = {
            "sha": new_head_sha,
            "passed": True,
            "tree_sha": commit_tree_sha,
            "source_evidence_ids": list(source_evidence_ids),
        }
        commit_payload = {
            "sha": new_head_sha,
            "passed": True,
            "tree_sha": commit_tree_sha,
            "parent_sha": expected_parent_sha,
            "run_id": run_id,
        }
        derived_json = json.dumps(
            derived_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        commit_json = json.dumps(
            commit_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        derived = EvidenceRef(
            evidence_id=f"internal-review:{run_id}:{new_head_sha}",
            kind="internal_review_passed",
            sha256=sha256_payload(derived_payload),
        )
        commit = EvidenceRef(
            evidence_id=f"commit:{run_id}:{new_head_sha}",
            kind="commit_created",
            sha256=sha256_payload(commit_payload),
        )
        timestamp = utc_now()
        event_id = new_event_id()
        with self.database.transaction() as connection:
            run_row = connection.execute(
                "SELECT state, head_sha FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise KeyError(run_id)
            if (
                RunState(run_row["state"]) is not RunState.COMMITTING
                or run_row["head_sha"] != expected_parent_sha
            ):
                raise RuntimeError("run changed before commit completion")
            attestation = connection.execute(
                "SELECT * FROM evidence WHERE evidence_id = ?",
                (attestation_id,),
            ).fetchone()
            if (
                attestation is None
                or attestation["run_id"] != run_id
                or attestation["kind"] != "internal_review_attestation"
            ):
                raise ValueError("review attestation is missing or forged")
            try:
                attestation_payload = json.loads(attestation["payload_json"])
            except (json.JSONDecodeError, TypeError):
                raise ValueError("review attestation payload is invalid") from None
            validation_id = (
                attestation_payload.get("validation_attestation_id")
                if type(attestation_payload) is dict
                else None
            )
            if not isinstance(validation_id, str):
                raise ValueError("review attestation lacks validation binding")
            validation = connection.execute(
                "SELECT * FROM evidence WHERE evidence_id = ?",
                (validation_id,),
            ).fetchone()
            if (
                validation is None
                or validation["run_id"] != run_id
                or validation["kind"] != "local_validation_attestation"
            ):
                raise ValueError("validation attestation is missing or forged")
            try:
                validation_payload = json.loads(validation["payload_json"])
            except (json.JSONDecodeError, TypeError):
                raise ValueError(
                    "validation attestation payload is invalid"
                ) from None
            if (
                type(validation_payload) is not dict
                or validation_payload.get("tested_tree_sha") != commit_tree_sha
                or attestation_payload.get("reviewed_tree_sha")
                != commit_tree_sha
                or attestation_payload.get("expected_parent_sha")
                != expected_parent_sha
            ):
                raise ValueError("commit tree is not bound to validation and review")
            validation_source_ids = validation_payload.get(
                "source_evidence_ids"
            )
            if (
                type(validation_source_ids) is not list
                or not validation_source_ids
                or any(
                    not isinstance(source_id, str)
                    for source_id in validation_source_ids
                )
            ):
                raise ValueError("validation source binding is invalid")
            for source_id in validation_source_ids:
                source = connection.execute(
                    "SELECT * FROM evidence WHERE evidence_id = ?",
                    (source_id,),
                ).fetchone()
                if source is None or source["run_id"] != run_id:
                    raise ValueError(
                        "validation source evidence is missing or forged"
                    )
                try:
                    source_payload = json.loads(source["payload_json"])
                except (json.JSONDecodeError, TypeError):
                    raise ValueError(
                        "validation source evidence payload is invalid"
                    ) from None
                if (
                    source["kind"] != "local_validation_passed"
                    or type(source_payload) is not dict
                    or source_payload.get("passed") is not True
                    or source_payload.get("sha") != expected_parent_sha
                    or source_payload.get("tested_tree_sha")
                    != commit_tree_sha
                    or source_payload.get("operation_identity")
                    != validation_payload.get("operation_identity")
                    or source_payload.get("checks_identity")
                    != validation_payload.get("checks_identity")
                ):
                    raise ValueError(
                        "validation source evidence does not match attestation"
                    )
            validation_events = connection.execute(
                """
                SELECT evidence_json FROM events
                WHERE run_id = ? AND from_state = ? AND to_state = ?
                """,
                (
                    run_id,
                    RunState.LOCAL_VALIDATION.value,
                    RunState.INTERNAL_REVIEW.value,
                ),
            ).fetchall()
            required_validation_ids = {
                validation_id,
                *validation_source_ids,
            }
            if not any(
                required_validation_ids
                <= set(json.loads(event["evidence_json"]))
                for event in validation_events
            ):
                raise ValueError(
                    "validation attestation sources are not bound "
                    "to validation event"
                )
            for source_id in source_evidence_ids:
                source = connection.execute(
                    "SELECT run_id FROM evidence WHERE evidence_id = ?",
                    (source_id,),
                ).fetchone()
                if source is None or source["run_id"] != run_id:
                    raise ValueError("review source evidence is missing or forged")
            review_events = connection.execute(
                """
                SELECT evidence_json FROM events
                WHERE run_id = ? AND from_state = ? AND to_state = ?
                """,
                (
                    run_id,
                    RunState.INTERNAL_REVIEW.value,
                    RunState.COMMITTING.value,
                ),
            ).fetchall()
            required_review_ids = {
                attestation_id,
                validation_id,
                *source_evidence_ids,
            }
            if not any(
                required_review_ids
                <= set(json.loads(event["evidence_json"]))
                for event in review_events
            ):
                raise ValueError(
                    "review attestation sources are not bound to review event"
                )
            operation = connection.execute(
                "SELECT status FROM remote_operations WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            if operation is not None:
                if operation["status"] == "PENDING":
                    connection.execute(
                        """
                        UPDATE remote_operations
                        SET status = 'CONFIRMED', updated_at = ?
                        WHERE operation_key = ?
                        """,
                        (timestamp, operation_key),
                    )
                elif operation["status"] != "CONFIRMED":
                    raise ValueError("commit operation is terminally failed")
            self._register_evidence_row(
                connection,
                run_id,
                commit.evidence_id,
                commit.kind,
                commit_json,
                commit.sha256,
                timestamp,
            )
            self._register_evidence_row(
                connection,
                run_id,
                derived.evidence_id,
                derived.kind,
                derived_json,
                derived.sha256,
                timestamp,
            )
            assert_transition(
                RunState.COMMITTING,
                RunState.PUSHING_FEATURE,
                (commit.kind, derived.kind),
                evidence_refs=(commit, derived),
            )
            connection.execute(
                """
                UPDATE runs SET state = ?, head_sha = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    RunState.PUSHING_FEATURE.value,
                    new_head_sha,
                    timestamp,
                    run_id,
                ),
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
                    "COMMIT_RECONCILED",
                    RunState.COMMITTING.value,
                    RunState.PUSHING_FEATURE.value,
                    json.dumps([commit.evidence_id, derived.evidence_id]),
                    timestamp,
                ),
            )
        return commit, derived
