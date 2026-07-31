from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import stat

from change_delivery.repository import RunRepository, TERMINAL_STATES


RAW_LOG_GLOBS = (
    "logs/*.stdout.log",
    "logs/*.stderr.log",
    "evidence/ci-raw-*.log",
)
REPORT_GLOBS = ("reports/*.md", "sanitized-summary.md")
_RAW_RETENTION = timedelta(days=30)
_REPORT_RETENTION = timedelta(days=180)


@dataclass(frozen=True)
class RetentionResult:
    deleted_raw_logs: int = 0
    deleted_reports: int = 0
    skipped_unknown: tuple[str, ...] = ()
    skipped_unsafe: tuple[str, ...] = ()


class RetentionService:
    """Delete only expired, explicitly allowlisted files of terminal runs."""

    def __init__(
        self,
        runtime_root: Path,
        repository: RunRepository,
        *,
        pinned_run_ids: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        self.runtime_root = runtime_root.absolute()
        self.repository = repository
        self.pinned_run_ids = frozenset(pinned_run_ids)

    @staticmethod
    def _is_reparse(metadata: os.stat_result) -> bool:
        return bool(
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )

    def clean(self, *, now: datetime | None = None) -> RetentionResult:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("retention clock must be timezone-aware")
        current = current.astimezone(UTC)
        root = self.runtime_root.resolve(strict=True)
        deleted_raw = 0
        deleted_reports = 0
        unknown: list[str] = []
        unsafe: list[str] = []
        with self.repository.database.connection() as connection:
            rows = connection.execute(
                "SELECT run_id, state FROM runs ORDER BY run_id"
            ).fetchall()
        terminal_values = {state.value for state in TERMINAL_STATES}
        for row in rows:
            if row["state"] not in terminal_values:
                continue
            run_dir = self.runtime_root / "runs" / row["run_id"]
            if not self._safe_directory(run_dir, root):
                unsafe.append(str(run_dir))
                continue
            known = self._known_paths(run_dir)
            unknown.extend(self._unknown_files(run_dir, known, unsafe))
            for path in sorted(known):
                category = known[path]
                if (
                    category == "report"
                    and path.name == "sanitized-summary.md"
                    and row["run_id"] in self.pinned_run_ids
                ):
                    continue
                age = self._age(path, current, run_dir, unsafe)
                threshold = _RAW_RETENTION if category == "raw" else _REPORT_RETENTION
                if age is None or age <= threshold:
                    continue
                if not self._safe_unlink(path, run_dir):
                    unsafe.append(str(path))
                    continue
                if category == "raw":
                    deleted_raw += 1
                else:
                    deleted_reports += 1
        return RetentionResult(
            deleted_raw_logs=deleted_raw,
            deleted_reports=deleted_reports,
            skipped_unknown=tuple(sorted(set(unknown))),
            skipped_unsafe=tuple(sorted(set(unsafe))),
        )

    def _safe_directory(self, directory: Path, root: Path) -> bool:
        try:
            metadata = directory.lstat()
            resolved = directory.resolve(strict=True)
            resolved.relative_to(root)
            return (
                not directory.is_symlink()
                and not self._is_reparse(metadata)
                and stat.S_ISDIR(metadata.st_mode)
                and resolved.parent == (root / "runs").resolve(strict=True)
            )
        except (OSError, ValueError):
            return False

    def _known_paths(self, run_dir: Path) -> dict[Path, str]:
        known: dict[Path, str] = {}
        for pattern in RAW_LOG_GLOBS:
            for path in run_dir.glob(pattern):
                known[path] = "raw"
        for pattern in REPORT_GLOBS:
            for path in run_dir.glob(pattern):
                known[path] = "report"
        return known

    def _unknown_files(
        self,
        run_dir: Path,
        known: dict[Path, str],
        unsafe: list[str],
    ) -> list[str]:
        results: list[str] = []
        known_names = {str(path.absolute()) for path in known}
        allowed_containers = {"logs", "evidence", "reports"}
        try:
            entries = list(run_dir.iterdir())
        except OSError:
            unsafe.append(str(run_dir))
            return results
        for entry in entries:
            try:
                metadata = entry.lstat()
            except OSError:
                unsafe.append(str(entry))
                continue
            if entry.is_symlink() or self._is_reparse(metadata):
                unsafe.append(str(entry))
                continue
            if stat.S_ISREG(metadata.st_mode):
                if str(entry.absolute()) not in known_names:
                    results.append(str(entry))
                continue
            if not stat.S_ISDIR(metadata.st_mode) or entry.name not in allowed_containers:
                results.append(str(entry))
                continue
            try:
                children = list(entry.iterdir())
            except OSError:
                unsafe.append(str(entry))
                continue
            for child in children:
                if str(child.absolute()) not in known_names:
                    results.append(str(child))
        return results

    def _age(
        self,
        path: Path,
        now: datetime,
        run_dir: Path,
        unsafe: list[str],
    ) -> timedelta | None:
        try:
            metadata = path.lstat()
            resolved = path.resolve(strict=True)
            resolved.relative_to(run_dir.resolve(strict=True))
            if (
                path.is_symlink()
                or self._is_reparse(metadata)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                unsafe.append(str(path))
                return None
            modified = datetime.fromtimestamp(metadata.st_mtime, tz=UTC)
            return max(timedelta(0), now - modified)
        except (OSError, ValueError):
            unsafe.append(str(path))
            return None

    def _safe_unlink(self, path: Path, run_dir: Path) -> bool:
        descriptor: int | None = None
        try:
            before = path.lstat()
            if (
                path.is_symlink()
                or self._is_reparse(before)
                or not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or path.resolve(strict=True).parent not in {
                    run_dir.resolve(strict=True),
                    (run_dir / "logs").resolve(strict=True),
                    (run_dir / "evidence").resolve(strict=True),
                    (run_dir / "reports").resolve(strict=True),
                }
            ):
                return False
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(descriptor)
            after = path.lstat()
            identity = (before.st_dev, before.st_ino)
            if (
                (opened.st_dev, opened.st_ino) != identity
                or (after.st_dev, after.st_ino) != identity
                or opened.st_nlink != 1
                or after.st_nlink != 1
            ):
                return False
            # Windows refuses to unlink while this descriptor is held.
            os.close(descriptor)
            descriptor = None
            final = path.lstat()
            if (
                (final.st_dev, final.st_ino) != identity
                or final.st_nlink != 1
                or self._is_reparse(final)
            ):
                return False
            path.unlink()
            return True
        except (OSError, ValueError):
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)
