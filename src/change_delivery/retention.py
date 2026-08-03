from __future__ import annotations

from contextlib import ExitStack, contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import stat
from typing import Iterator

from filelock import FileLock

from change_delivery.artifacts import validate_runtime_root
from change_delivery.repository import RunRepository, TERMINAL_STATES


RAW_LOG_GLOBS = (
    "logs/*.stdout.log",
    "logs/*.stderr.log",
    "evidence/ci-raw-*.log",
)
REPORT_GLOBS = ("reports/*.md", "sanitized-summary.md")
_RAW_RETENTION = timedelta(days=30)
_REPORT_RETENTION = timedelta(days=180)


class _StableDirectoryHandle:
    """Hold a directory identity and, on Windows, deny rename/delete sharing."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.canonical_path: Path | None = None
        self._handle: int | None = None

    def __enter__(self) -> _StableDirectoryHandle:
        if os.name == "nt":
            self._open_windows()
        else:
            self._open_posix()
        return self

    def __exit__(self, *args: object) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        if os.name == "nt":
            _windows_kernel32().CloseHandle(handle)
        else:
            os.close(handle)

    def _open_posix(self) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            os.close(descriptor)
            raise ValueError("stable handle target is not a directory")
        self._handle = descriptor
        self.canonical_path = self.path.resolve(strict=True)

    def _open_windows(self) -> None:
        class FileInformation(ctypes.Structure):
            _fields_ = [
                ("attributes", ctypes.c_ulong),
                ("creation_time_low", ctypes.c_ulong),
                ("creation_time_high", ctypes.c_ulong),
                ("access_time_low", ctypes.c_ulong),
                ("access_time_high", ctypes.c_ulong),
                ("write_time_low", ctypes.c_ulong),
                ("write_time_high", ctypes.c_ulong),
                ("volume_serial", ctypes.c_ulong),
                ("file_size_high", ctypes.c_ulong),
                ("file_size_low", ctypes.c_ulong),
                ("link_count", ctypes.c_ulong),
                ("file_index_high", ctypes.c_ulong),
                ("file_index_low", ctypes.c_ulong),
            ]

        kernel32 = _windows_kernel32()
        handle = kernel32.CreateFileW(
            str(self.path),
            0x0080,  # FILE_READ_ATTRIBUTES
            0x00000001 | 0x00000002,  # exclude FILE_SHARE_DELETE
            None,
            3,  # OPEN_EXISTING
            0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            if error in {2, 3}:
                raise FileNotFoundError(error, "directory no longer exists", self.path)
            raise ctypes.WinError(error)
        information = FileInformation()
        if not kernel32.GetFileInformationByHandle(
            handle, ctypes.byref(information)
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise ctypes.WinError(error)
        if not information.attributes & 0x10 or information.attributes & 0x400:
            kernel32.CloseHandle(handle)
            raise ValueError("stable handle target is unsafe")
        try:
            self.canonical_path = _windows_final_path(kernel32, handle)
        except (OSError, ValueError):
            kernel32.CloseHandle(handle)
            raise
        self._handle = int(handle)


def _windows_final_path(kernel32: object, handle: int) -> Path:
    buffer = ctypes.create_unicode_buffer(32_768)
    length = kernel32.GetFinalPathNameByHandleW(
        handle, buffer, len(buffer), 0
    )
    if length == 0 or length >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        raise ValueError("UNC retention paths are not supported")
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _windows_kernel32() -> object:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.c_void_p,
    )
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.GetFinalPathNameByHandleW.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel32.SetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


@contextmanager
def maintenance_lock(
    runtime_root: Path, *, timeout: float = 30,
) -> Iterator[None]:
    root = validate_runtime_root(runtime_root, require_exists=True)
    lock_path = root / "retention.lock"
    with FileLock(lock_path, timeout=timeout):
        validate_runtime_root(root, require_exists=True)
        metadata = lock_path.lstat()
        if (
            lock_path.is_symlink()
            or bool(
                getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            )
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or lock_path.resolve(strict=True).parent != root.resolve(strict=True)
        ):
            raise ValueError("maintenance lock is unsafe")
        yield


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
        lock_timeout: float = 30,
    ) -> None:
        self.runtime_root = validate_runtime_root(
            runtime_root, require_exists=True
        )
        self.repository = repository
        self.lock_timeout = lock_timeout

    @staticmethod
    def _is_reparse(metadata: os.stat_result) -> bool:
        return bool(
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )

    def clean(self, *, now: datetime | None = None) -> RetentionResult:
        with maintenance_lock(
            self.runtime_root, timeout=self.lock_timeout
        ):
            return self._clean_locked(now=now)

    def _clean_locked(self, *, now: datetime | None = None) -> RetentionResult:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("retention clock must be timezone-aware")
        current = current.astimezone(UTC)
        root = self._validated_runtime_root()
        deleted_raw = 0
        deleted_reports = 0
        unknown: list[str] = []
        unsafe: list[str] = []
        with self.repository.database.connection() as connection:
            rows = connection.execute(
                "SELECT run_id, state, pinned FROM runs ORDER BY run_id"
            ).fetchall()
        terminal_values = {state.value for state in TERMINAL_STATES}
        terminal_rows = [
            row for row in rows if row["state"] in terminal_values
        ]
        if terminal_rows:
            runs_dir = root / "runs"
            try:
                with self._stable_directory(root) as stable_root:
                    assert stable_root.canonical_path is not None
                    with self._stable_directory(
                        runs_dir,
                        expected_parent=stable_root.canonical_path,
                    ) as stable_runs:
                        assert stable_runs.canonical_path is not None
                        for row in terminal_rows:
                            run_dir = runs_dir / row["run_id"]
                            try:
                                with self._stable_directory(
                                    run_dir,
                                    expected_parent=stable_runs.canonical_path,
                                ) as stable_run:
                                    assert stable_run.canonical_path is not None
                                    if not self._safe_directory(run_dir, root):
                                        unsafe.append(str(run_dir))
                                        continue
                                    with ExitStack() as containers:
                                        locked_containers: dict[
                                            str, _StableDirectoryHandle
                                        ] = {}
                                        container_unsafe = False
                                        for name in ("logs", "evidence", "reports"):
                                            container = run_dir / name
                                            try:
                                                stable_container = (
                                                    containers.enter_context(
                                                        self._stable_directory(
                                                            container,
                                                            expected_parent=(
                                                                stable_run.canonical_path
                                                            ),
                                                        )
                                                    )
                                                )
                                            except FileNotFoundError:
                                                continue
                                            except (OSError, ValueError):
                                                unsafe.append(str(container))
                                                container_unsafe = True
                                                break
                                            locked_containers[name] = stable_container
                                        if container_unsafe:
                                            continue
                                        known = self._known_paths(
                                            run_dir, locked_containers
                                        )
                                        unknown.extend(
                                            self._unknown_files(
                                                run_dir, known, unsafe
                                            )
                                        )
                                        for path in sorted(known):
                                            category = known[path]
                                            if (
                                                category == "report"
                                                and path.name == "sanitized-summary.md"
                                            ):
                                                authoritative = (
                                                    self.repository.get_run(
                                                        row["run_id"]
                                                    )
                                                )
                                                if authoritative.pinned:
                                                    continue
                                            age = self._age(
                                                path, current, run_dir, unsafe
                                            )
                                            threshold = (
                                                _RAW_RETENTION
                                                if category == "raw"
                                                else _REPORT_RETENTION
                                            )
                                            if age is None or age <= threshold:
                                                continue
                                            stable_parent = (
                                                stable_run
                                                if path.parent == run_dir
                                                else locked_containers.get(
                                                    path.parent.name
                                                )
                                            )
                                            if (
                                                stable_parent is None
                                                or stable_parent.canonical_path is None
                                                or not self._safe_unlink(
                                                    path,
                                                    stable_parent.canonical_path,
                                                )
                                            ):
                                                unsafe.append(str(path))
                                                continue
                                            if category == "raw":
                                                deleted_raw += 1
                                            else:
                                                deleted_reports += 1
                            except (OSError, ValueError):
                                unsafe.append(str(run_dir))
            except (OSError, ValueError) as exc:
                raise ValueError(
                    "runtime directory identity cannot be stabilized"
                ) from exc
        return RetentionResult(
            deleted_raw_logs=deleted_raw,
            deleted_reports=deleted_reports,
            skipped_unknown=tuple(sorted(set(unknown))),
            skipped_unsafe=tuple(sorted(set(unsafe))),
        )

    def _validated_runtime_root(self) -> Path:
        current = validate_runtime_root(
            self.runtime_root, require_exists=True
        )
        resolved = current.resolve(strict=True)
        if resolved != current:
            raise ValueError("runtime root identity changed during validation")
        return resolved

    @contextmanager
    def _stable_directory(
        self, directory: Path, *, expected_parent: Path | None = None,
    ) -> Iterator[_StableDirectoryHandle]:
        with _StableDirectoryHandle(directory) as stable:
            resolved = stable.canonical_path
            if resolved is None:
                raise ValueError("stable directory identity is unavailable")
            if expected_parent is not None:
                if resolved.parent != expected_parent:
                    raise ValueError("stable directory escaped its expected parent")
            yield stable

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

    def _known_paths(
        self,
        run_dir: Path,
        locked_containers: dict[str, _StableDirectoryHandle],
    ) -> dict[Path, str]:
        known: dict[Path, str] = {}
        for pattern in RAW_LOG_GLOBS:
            if pattern.partition("/")[0] not in locked_containers:
                continue
            for path in run_dir.glob(pattern):
                known[path] = "raw"
        for pattern in REPORT_GLOBS:
            container = pattern.partition("/")[0]
            if "/" in pattern and container not in locked_containers:
                continue
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

    def _safe_unlink(self, path: Path, expected_parent: Path) -> bool:
        if os.name == "nt":
            return self._safe_unlink_windows(path, expected_parent)
        descriptor: int | None = None
        try:
            before = path.lstat()
            if (
                path.is_symlink()
                or self._is_reparse(before)
                or not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or path.resolve(strict=True).parent != expected_parent
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

    def _safe_unlink_windows(
        self, path: Path, expected_parent: Path,
    ) -> bool:
        class FileInformation(ctypes.Structure):
            _fields_ = [
                ("attributes", ctypes.c_ulong),
                ("creation_time_low", ctypes.c_ulong),
                ("creation_time_high", ctypes.c_ulong),
                ("access_time_low", ctypes.c_ulong),
                ("access_time_high", ctypes.c_ulong),
                ("write_time_low", ctypes.c_ulong),
                ("write_time_high", ctypes.c_ulong),
                ("volume_serial", ctypes.c_ulong),
                ("file_size_high", ctypes.c_ulong),
                ("file_size_low", ctypes.c_ulong),
                ("link_count", ctypes.c_ulong),
                ("file_index_high", ctypes.c_ulong),
                ("file_index_low", ctypes.c_ulong),
            ]

        class FileDispositionInformation(ctypes.Structure):
            _fields_ = [("delete_file", ctypes.c_ubyte)]

        kernel32 = _windows_kernel32()
        handle = kernel32.CreateFileW(
            str(path),
            0x00010000 | 0x0080,  # DELETE | FILE_READ_ATTRIBUTES
            0x00000001 | 0x00000002,  # exclude FILE_SHARE_DELETE
            None,
            3,  # OPEN_EXISTING
            0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            return False
        try:
            information = FileInformation()
            if not kernel32.GetFileInformationByHandle(
                handle, ctypes.byref(information)
            ):
                return False
            if (
                information.attributes & 0x10
                or information.attributes & 0x400
                or information.link_count != 1
            ):
                return False
            final_path = _windows_final_path(kernel32, handle)
            if os.path.normcase(str(final_path.parent)) != os.path.normcase(
                str(expected_parent)
            ):
                return False
            disposition = FileDispositionInformation(delete_file=1)
            return bool(
                kernel32.SetFileInformationByHandle(
                    handle,
                    4,  # FileDispositionInfo
                    ctypes.byref(disposition),
                    ctypes.sizeof(disposition),
                )
            )
        except (OSError, ValueError):
            return False
        finally:
            kernel32.CloseHandle(handle)
