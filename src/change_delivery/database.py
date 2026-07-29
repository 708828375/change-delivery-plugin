from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        config_hash TEXT NOT NULL,
        state TEXT NOT NULL,
        head_sha TEXT,
        pr_number INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        event_type TEXT NOT NULL,
        from_state TEXT NOT NULL,
        to_state TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS approvals (
        run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
        approval_json TEXT NOT NULL,
        approved_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence (
        evidence_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS remote_operations (
        operation_key TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        operation_type TEXT NOT NULL,
        remote_identity TEXT,
        status TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notifications (
        notification_key TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        payload_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scheduler_polls (
        run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
        polled_at TEXT NOT NULL,
        effective_at TEXT NOT NULL,
        generation INTEGER NOT NULL DEFAULT 0
    )
    """,
)


class Database:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms

    def _connect(self, *, query_only: bool = False) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms:d}")
        if query_only:
            connection.execute("PRAGMA query_only = ON")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect(query_only=True)
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")
        finally:
            connection.close()

    def migrate(self) -> None:
        if self.path.exists() and self.path.is_dir():
            raise IsADirectoryError(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as connection:
            for statement in MIGRATIONS:
                connection.execute(statement)
            scheduler_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(scheduler_polls)"
                ).fetchall()
            }
            if "effective_at" not in scheduler_columns:
                connection.execute(
                    "ALTER TABLE scheduler_polls ADD COLUMN effective_at TEXT"
                )
                connection.execute(
                    "UPDATE scheduler_polls SET effective_at = polled_at "
                    "WHERE effective_at IS NULL"
                )
            if "generation" not in scheduler_columns:
                connection.execute(
                    "ALTER TABLE scheduler_polls "
                    "ADD COLUMN generation INTEGER NOT NULL DEFAULT 0"
                )
