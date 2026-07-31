from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import sys
from typing import TextIO
import unicodedata

from change_delivery.database import Database
from change_delivery.domain import RunState


@dataclass(frozen=True)
class NotificationResult:
    notification_key: str
    emitted: bool
    text: str


def _normalized(value: str, *, field: str) -> str:
    normalized = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized or any(ord(char) < 32 and char not in "\n\t" for char in normalized):
        raise ValueError(f"{field} must be non-blank text without control characters")
    return normalized


class NotificationService:
    """Atomically deduplicate deterministic CLI/log notifications."""

    def __init__(self, database: Database, *, output: TextIO | None = None) -> None:
        self.database = database
        self.output = output or sys.stdout

    def emit(
        self,
        run_id: str,
        state: RunState | str,
        message: str,
        *,
        action: str = "inspect run status",
    ) -> NotificationResult:
        canonical_run_id = _normalized(run_id, field="run_id")
        canonical_message = _normalized(message, field="message")
        canonical_action = _normalized(action, field="action")
        try:
            requested_state = state if isinstance(state, RunState) else RunState(state)
        except (TypeError, ValueError) as error:
            raise ValueError("state is invalid") from error
        canonical = json.dumps(
            {
                "message": canonical_message,
                "run_id": canonical_run_id,
                "state": requested_state.value,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        notification_key = payload_hash
        emitted = False
        with self.database.transaction() as connection:
            run = connection.execute(
                "SELECT project_id, state, pr_number FROM runs WHERE run_id = ?",
                (canonical_run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(canonical_run_id)
            if run["state"] != requested_state.value:
                raise ValueError("notification state does not match authoritative run state")
            existing = connection.execute(
                "SELECT run_id, payload_hash FROM notifications WHERE notification_key = ?",
                (notification_key,),
            ).fetchone()
            if existing is not None:
                if existing["run_id"] != canonical_run_id or existing["payload_hash"] != payload_hash:
                    raise ValueError("notification record is corrupt")
            else:
                connection.execute(
                    """
                    INSERT INTO notifications (
                        notification_key, run_id, payload_hash, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        notification_key,
                        canonical_run_id,
                        payload_hash,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                emitted = True
            pr = f"#{run['pr_number']}" if run["pr_number"] is not None else "none"
            text = (
                f"run={canonical_run_id} project={run['project_id']} pr={pr} "
                f"status={requested_state.value} reason={canonical_message} "
                f"action={canonical_action}"
            )
        if emitted:
            print(text, file=self.output, flush=True)
        return NotificationResult(
            notification_key=notification_key,
            emitted=emitted,
            text=text,
        )
