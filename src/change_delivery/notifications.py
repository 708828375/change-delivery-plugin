from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import sys
from typing import TextIO
import unicodedata
from uuid import uuid4

from change_delivery.database import Database
from change_delivery.domain import RunState
from change_delivery.redaction import redact_text


@dataclass(frozen=True)
class NotificationResult:
    notification_key: str
    emitted: bool
    text: str


def _normalized(value: str, *, field: str) -> str:
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or any(ord(char) < 32 for char in normalized):
        raise ValueError(f"{field} must be non-blank single-line text")
    return normalized


class NotificationService:
    """Claim, deliver, and atomically deduplicate structured CLI notifications."""

    def __init__(self, database: Database, *, output: TextIO | None = None, secrets: Mapping[str, str] | None = None) -> None:
        self.database = database
        self.output = output or sys.stdout
        self.secrets = dict(secrets or {})

    def emit(self, run_id: str, state: RunState | str, message: str, *, action: str = "inspect run status") -> NotificationResult:
        canonical_run_id = _normalized(run_id, field="run_id")
        canonical_message = _normalized(message, field="message")
        canonical_action = _normalized(action, field="action")
        try:
            requested_state = state if isinstance(state, RunState) else RunState(state)
        except (TypeError, ValueError) as error:
            raise ValueError("state is invalid") from error
        canonical = json.dumps({"message": canonical_message, "run_id": canonical_run_id, "state": requested_state.value}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        notification_key = payload_hash
        token = uuid4().hex
        claimed = False
        with self.database.transaction() as connection:
            run = connection.execute("SELECT project_id, state, pr_number FROM runs WHERE run_id = ?", (canonical_run_id,)).fetchone()
            if run is None:
                raise KeyError(canonical_run_id)
            if run["state"] != requested_state.value:
                raise ValueError("notification state does not match authoritative run state")
            existing = connection.execute("SELECT run_id, payload_hash, delivery_status, delivery_token, updated_at FROM notifications WHERE notification_key = ?", (notification_key,)).fetchone()
            timestamp = datetime.now(UTC).isoformat()
            if existing is not None:
                if existing["run_id"] != canonical_run_id or existing["payload_hash"] != payload_hash:
                    raise ValueError("notification record is corrupt")
                if existing["delivery_status"] == "DELIVERED":
                    claimed = False
                elif existing["delivery_status"] == "PENDING":
                    previous_token = existing["delivery_token"]
                    updated_at = datetime.fromisoformat(existing["updated_at"])
                    stale = datetime.now(UTC) - updated_at.astimezone(UTC) >= timedelta(minutes=5)
                    if previous_token is None or stale:
                        connection.execute(
                            "UPDATE notifications SET delivery_token = ?, updated_at = ? "
                            "WHERE notification_key = ? AND delivery_status = 'PENDING' "
                            "AND delivery_token IS ? AND updated_at = ?",
                            (token, timestamp, notification_key, previous_token, existing["updated_at"]),
                        )
                        claimed = connection.execute("SELECT changes()").fetchone()[0] == 1
                else:
                    raise ValueError("notification record is corrupt")
            else:
                connection.execute("INSERT INTO notifications (notification_key, run_id, payload_hash, delivery_status, delivery_token, updated_at, created_at) VALUES (?, ?, ?, 'PENDING', ?, ?, ?)", (notification_key, canonical_run_id, payload_hash, token, timestamp, timestamp))
                claimed = True
            pr = f"#{run['pr_number']}" if run["pr_number"] is not None else "none"
            text = json.dumps({"action": redact_text(canonical_action, self.secrets), "pr": pr, "project": run["project_id"], "reason": redact_text(canonical_message, self.secrets), "run": canonical_run_id, "status": requested_state.value}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if claimed:
            try:
                print(text, file=self.output, flush=True)
            except (BrokenPipeError, OSError):
                self._release(notification_key, token)
                raise
            with self.database.transaction() as connection:
                connection.execute("UPDATE notifications SET delivery_status = 'DELIVERED', delivery_token = NULL, updated_at = ? WHERE notification_key = ? AND delivery_status = 'PENDING' AND delivery_token = ?", (datetime.now(UTC).isoformat(), notification_key, token))
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise ValueError("notification delivery claim was lost")
        return NotificationResult(notification_key=notification_key, emitted=claimed, text=text)

    def _release(self, notification_key: str, token: str) -> None:
        with self.database.transaction() as connection:
            connection.execute("UPDATE notifications SET delivery_token = NULL, updated_at = ? WHERE notification_key = ? AND delivery_status = 'PENDING' AND delivery_token = ?", (datetime.now(UTC).isoformat(), notification_key, token))
