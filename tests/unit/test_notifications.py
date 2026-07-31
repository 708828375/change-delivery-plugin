from concurrent.futures import ThreadPoolExecutor
from io import StringIO
import json
from pathlib import Path
from datetime import UTC, datetime, timedelta

import pytest

from change_delivery.database import Database
from change_delivery.domain import RunState
from change_delivery.notifications import NotificationService
from change_delivery.repository import RunRepository


def make_service(tmp_path: Path) -> tuple[NotificationService, Database, StringIO]:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    RunRepository(database).create_run("CD-1", "project", "hash")
    output = StringIO()
    return NotificationService(database, output=output), database, output


def test_notification_is_atomic_and_emitted_only_when_payload_changes(tmp_path: Path) -> None:
    service, database, output = make_service(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: service.emit("CD-1", RunState.DRAFT, "waiting"), range(16)))
    changed = service.emit("CD-1", RunState.DRAFT, "changed", action="inspect")

    assert sum(result.emitted for result in results) == 1
    assert changed.emitted
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 2
    rendered = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rendered[-1] == {
        "action": "inspect", "pr": "none", "project": "project",
        "reason": "changed", "run": "CD-1", "status": "DRAFT",
    }


def test_notification_rejects_state_not_matching_authoritative_run(tmp_path: Path) -> None:
    service, _, output = make_service(tmp_path)
    with pytest.raises(ValueError, match="state"):
        service.emit("CD-1", RunState.BLOCKED, "forged")
    assert output.getvalue() == ""


def test_notification_fails_closed_for_forged_existing_hash(tmp_path: Path) -> None:
    service, database, _ = make_service(tmp_path)
    result = service.emit("CD-1", RunState.DRAFT, "waiting")
    with database.transaction() as connection:
        connection.execute(
            "UPDATE notifications SET payload_hash = ? WHERE notification_key = ?",
            ("f" * 64, result.notification_key),
        )
    with pytest.raises(ValueError, match="corrupt"):
        service.emit("CD-1", RunState.DRAFT, "waiting")
class BrokenOutput(StringIO):
    def write(self, value: str) -> int:
        raise BrokenPipeError("closed")


def test_failed_output_releases_claim_and_retries(tmp_path: Path) -> None:
    service, database, _ = make_service(tmp_path)
    service.output = BrokenOutput()
    with pytest.raises(BrokenPipeError):
        service.emit("CD-1", RunState.DRAFT, "waiting")
    with database.connection() as connection:
        row = connection.execute("SELECT delivery_status, delivery_token FROM notifications").fetchone()
        assert tuple(row) == ("PENDING", None)
    output = StringIO()
    service.output = output
    assert service.emit("CD-1", RunState.DRAFT, "waiting").emitted
    assert len(output.getvalue().splitlines()) == 1


def test_notification_rejects_log_injection_and_redacts_secrets(tmp_path: Path) -> None:
    service, _, output = make_service(tmp_path)
    service.secrets = {"api": "super-secret"}
    with pytest.raises(ValueError):
        service.emit("CD-1", RunState.DRAFT, "bad\nline")
    service.emit("CD-1", RunState.DRAFT, "token super-secret")
    assert "super-secret" not in output.getvalue()
    assert "***" in output.getvalue()

def test_stale_pending_claim_is_recovered_after_process_crash(tmp_path: Path) -> None:
    service, database, output = make_service(tmp_path)
    canonical = json.dumps(
        {"message": "waiting", "run_id": "CD-1", "state": "DRAFT"},
        ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )
    import hashlib
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    old = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO notifications VALUES (?, ?, ?, ?, ?, ?, ?)",
            (digest, "CD-1", digest, "PENDING", "dead-process", old, old),
        )
    assert service.emit("CD-1", RunState.DRAFT, "waiting").emitted
    assert len(output.getvalue().splitlines()) == 1