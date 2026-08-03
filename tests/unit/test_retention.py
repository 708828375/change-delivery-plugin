from datetime import UTC, datetime, timedelta
from io import StringIO
import os
from pathlib import Path
import subprocess
import threading

import pytest

from change_delivery.database import Database
from change_delivery.cli import CliServices, main
from change_delivery.domain import RunState
from change_delivery.repository import RunRepository
import change_delivery.retention as retention_module
from change_delivery.retention import RetentionService


NOW = datetime(2026, 7, 30, 12, tzinfo=UTC)


def fixture(tmp_path: Path, *, pinned: bool = False):
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    repository = RunRepository(database)
    repository.create_run("CD-1", "project", "hash")
    with database.transaction() as connection:
        connection.execute("UPDATE runs SET state = ? WHERE run_id = ?", (RunState.COMPLETED.value, "CD-1"))
    run = tmp_path / "runs" / "CD-1"
    for directory in (run / "logs", run / "evidence", run / "reports"):
        directory.mkdir(parents=True, exist_ok=True)
    if pinned:
        repository.set_pinned("CD-1", pinned=True)
    service = RetentionService(tmp_path, repository)
    return service, run


def old(path: Path, days: int) -> None:
    path.write_text("data", encoding="utf-8")
    timestamp = (NOW - timedelta(days=days)).timestamp()
    os.utime(path, (timestamp, timestamp))


def test_retention_deletes_only_whitelisted_expired_files(tmp_path: Path) -> None:
    service, run = fixture(tmp_path)
    old(run / "logs" / "cmd.stdout.log", 31)
    old(run / "logs" / "cmd.stderr.log", 29)
    old(run / "evidence" / "ci-raw-1.log", 31)
    old(run / "reports" / "summary.md", 181)
    old(run / "sanitized-summary.md", 179)
    unknown = run / "unknown.bin"
    old(unknown, 999)

    result = service.clean(now=NOW)

    assert result.deleted_raw_logs == 2
    assert result.deleted_reports == 1
    assert unknown.exists()
    assert (run / "logs" / "cmd.stderr.log").exists()
    assert (run / "sanitized-summary.md").exists()
    assert str(unknown) in result.skipped_unknown


def test_retention_keeps_nonterminal_and_pinned_sanitized_report(tmp_path: Path) -> None:
    service, run = fixture(tmp_path, pinned=True)
    old(run / "sanitized-summary.md", 200)
    with service.repository.database.transaction() as connection:
        connection.execute("UPDATE runs SET state = ? WHERE run_id = ?", (RunState.DRAFT.value, "CD-1"))
    assert service.clean(now=NOW).deleted_reports == 0
    with service.repository.database.transaction() as connection:
        connection.execute("UPDATE runs SET state = ? WHERE run_id = ?", (RunState.CANCELLED.value, "CD-1"))
    assert service.clean(now=NOW).deleted_reports == 0
    assert (run / "sanitized-summary.md").exists()


def test_retention_rejects_hardlinked_candidate(tmp_path: Path) -> None:
    service, run = fixture(tmp_path)
    candidate = run / "logs" / "cmd.stdout.log"
    old(candidate, 31)
    other = tmp_path / "other.log"
    os.link(candidate, other)

    result = service.clean(now=NOW)

    assert candidate.exists() and other.exists()
    assert str(candidate) in result.skipped_unsafe


def test_retention_is_idempotent(tmp_path: Path) -> None:
    service, run = fixture(tmp_path)
    old(run / "logs" / "cmd.stdout.log", 31)
    assert service.clean(now=NOW).deleted_raw_logs == 1
    assert service.clean(now=NOW).deleted_raw_logs == 0

def test_retention_rejects_symlink_or_reparse_runtime_root(
    tmp_path: Path, monkeypatch,
) -> None:
    service, _ = fixture(tmp_path)
    original = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == tmp_path or original(path),
    )
    with pytest.raises(ValueError, match="ancestry"):
        service.clean(now=NOW)

def test_retention_rejects_windows_reparse_runtime_root(tmp_path: Path, monkeypatch) -> None:
    service, _ = fixture(tmp_path)
    monkeypatch.setattr(
        "change_delivery.artifacts._path_is_reparse", lambda path: True
    )
    with pytest.raises(ValueError, match="ancestry"):
        service.clean(now=NOW)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction race")
def test_retention_fails_closed_after_run_directory_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, run = fixture(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-retention-outside"
    outside.mkdir()
    victim = outside / "logs" / "victim.stdout.log"
    victim.parent.mkdir()
    old(victim, 31)
    parked = run.with_name("CD-1-parked")
    original_safe_directory = service._safe_directory
    replacement_succeeded = False

    def replace_after_validation(directory: Path, root: Path) -> bool:
        nonlocal replacement_succeeded
        safe = original_safe_directory(directory, root)
        if not safe:
            return False
        try:
            directory.rename(parked)
        except OSError:
            return False
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(directory), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            parked.rename(directory)
            pytest.fail(f"could not create test junction: {result.stderr}")
        replacement_succeeded = True
        return True

    monkeypatch.setattr(service, "_safe_directory", replace_after_validation)
    try:
        result = service.clean(now=NOW)

        assert replacement_succeeded is True
        assert result.deleted_raw_logs == 0
        assert victim.exists()
        assert any(path.startswith(str(run)) for path in result.skipped_unsafe)
    finally:
        if replacement_succeeded:
            os.rmdir(run)
            parked.rename(run)
        if victim.exists():
            victim.unlink()
        if victim.parent.exists():
            victim.parent.rmdir()
        if outside.exists():
            outside.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle deletion")
def test_retention_keeps_file_handle_bound_through_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, run = fixture(tmp_path)
    candidate = run / "logs" / "cmd.stdout.log"
    old(candidate, 31)
    parked = candidate.with_suffix(".parked")
    original_final_path = retention_module._windows_final_path
    original_unlink = Path.unlink
    replacement_attempted = False
    replacement_blocked = False

    def race_after_handle_validation(kernel32, handle: int) -> Path:
        nonlocal replacement_attempted, replacement_blocked
        final_path = original_final_path(kernel32, handle)
        if final_path == candidate:
            replacement_attempted = True
            try:
                candidate.rename(parked)
            except OSError:
                replacement_blocked = True
        return final_path

    def reject_path_unlink(path: Path, *args, **kwargs) -> None:
        if path == candidate:
            pytest.fail("Windows retention deletion fell back to Path.unlink")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(retention_module, "_windows_final_path", race_after_handle_validation)
    monkeypatch.setattr(Path, "unlink", reject_path_unlink)

    result = service.clean(now=NOW)

    assert replacement_attempted is True
    assert replacement_blocked is True
    assert result.deleted_raw_logs == 1
    assert not candidate.exists()
    assert not parked.exists()


def test_successful_concurrent_pin_preserves_expired_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, run = fixture(tmp_path)
    summary = run / "sanitized-summary.md"
    old(summary, 200)
    entered_set_pinned = threading.Event()
    release_set_pinned = threading.Event()
    original_set_pinned = service.repository.set_pinned

    def delayed_set_pinned(run_id: str, *, pinned: bool):
        entered_set_pinned.set()
        assert release_set_pinned.wait(timeout=5)
        return original_set_pinned(run_id, pinned=pinned)

    monkeypatch.setattr(service.repository, "set_pinned", delayed_set_pinned)
    services = CliServices(plugin_root=tmp_path, repository=service.repository)
    pin_result: list[int] = []
    pin_thread = threading.Thread(
        target=lambda: pin_result.append(
            main(
                ["pin", "CD-1", "--json"], services=services,
                stdout=StringIO(), stderr=StringIO(),
            )
        )
    )
    pin_thread.start()
    assert entered_set_pinned.wait(timeout=2)

    retention_thread = threading.Thread(target=lambda: service.clean(now=NOW))
    retention_thread.start()
    retention_thread.join(timeout=2)
    release_set_pinned.set()
    pin_thread.join(timeout=5)
    retention_thread.join(timeout=5)

    assert pin_result == [0]
    assert service.repository.get_run("CD-1").pinned is True
    assert summary.exists()


def test_pin_fails_when_retention_deleted_summary_before_lock_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, run = fixture(tmp_path)
    summary = run / "sanitized-summary.md"
    old(summary, 200)
    entered_delete = threading.Event()
    release_delete = threading.Event()
    original_unlink = service._safe_unlink

    def delayed_unlink(path: Path, run_dir: Path) -> bool:
        if path == summary:
            entered_delete.set()
            assert release_delete.wait(timeout=5)
        return original_unlink(path, run_dir)

    monkeypatch.setattr(service, "_safe_unlink", delayed_unlink)
    services = CliServices(plugin_root=tmp_path, repository=service.repository)
    retention_thread = threading.Thread(target=lambda: service.clean(now=NOW))
    retention_thread.start()
    assert entered_delete.wait(timeout=2)

    pin_result: list[int] = []
    pin_thread = threading.Thread(
        target=lambda: pin_result.append(
            main(
                ["pin", "CD-1", "--json"], services=services,
                stdout=StringIO(), stderr=StringIO(),
            )
        )
    )
    pin_thread.start()
    assert pin_thread.is_alive()
    release_delete.set()
    retention_thread.join(timeout=5)
    pin_thread.join(timeout=5)

    assert pin_result and pin_result[0] != 0
    assert service.repository.get_run("CD-1").pinned is False
    assert not summary.exists()
