from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path

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
    rendered = output.getvalue()
    assert "run=CD-1" in rendered and "project=project" in rendered
    assert "pr=none" in rendered and "status=DRAFT" in rendered
    assert "reason=changed" in rendered and "action=inspect" in rendered


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
