"""Immutable approval snapshots and their on-disk representation."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

from pydantic import (
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
)

from change_delivery.domain import FrozenModel, RunState


DEFAULT_ALLOWED_SIDE_EFFECTS = (
    "git_commit",
    "git_push_feature",
    "create_or_update_pr",
    "force_with_lease_test",
    "tagged_test_api_data",
    "review_fix_loop",
)

REQUIRED_ARTIFACT_NAMES = (
    "requirement.md",
    "design.md",
    "implementation-plan.md",
)


class FrozenMapping(Mapping[str, str]):
    """A small immutable mapping backed only by an ordered tuple."""

    __slots__ = ("_items",)

    def __init__(self, values: Mapping[str, str]) -> None:
        object.__setattr__(self, "_items", tuple(sorted(values.items())))

    def __getitem__(self, key: str) -> str:
        for name, digest in self._items:
            if name == key:
                return digest
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (name for name, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __setattr__(self, name: str, value: object) -> None:
        raise TypeError("approval artifact hashes are immutable")


class ApprovalSnapshot(FrozenModel):
    """The exact files, configuration and authority approved for a run."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        arbitrary_types_allowed=True,
    )

    run_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    config_hash: str = Field(min_length=1)
    base_branch: str = Field(min_length=1)
    base_sha: str = Field(pattern=r"^[0-9a-fA-F]{40}$")
    test_branch: str = Field(min_length=1)
    automation_limit: RunState = RunState.READY_TO_MERGE
    artifact_hashes: FrozenMapping
    approved_user: str = Field(min_length=1)
    approved_at: datetime
    allowed_side_effects: tuple[str, ...] = DEFAULT_ALLOWED_SIDE_EFFECTS

    @field_validator("run_id", "project_id", "base_branch",
                     "test_branch", "approved_user")
    @classmethod
    def reject_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("approval identifiers cannot be blank")
        return value

    @field_validator("config_hash")
    @classmethod
    def validate_config_hash(cls, value: str) -> str:
        if not value.strip() or any(ord(character) < 32 or ord(character) == 127
                                    for character in value):
            raise ValueError("config_hash cannot be blank or contain controls")
        return value

    @field_validator("artifact_hashes", mode="before")
    @classmethod
    def freeze_and_validate_hashes(
        cls,
        value: object,
    ) -> FrozenMapping:
        if not isinstance(value, Mapping):
            raise ValueError("artifact_hashes must be a mapping")
        frozen = FrozenMapping(
            {
                str(name): str(digest)
                for name, digest in value.items()
            }
        )
        if not frozen:
            raise ValueError("artifact_hashes cannot be empty")
        if set(frozen) != set(REQUIRED_ARTIFACT_NAMES):
            raise ValueError("artifact_hashes must contain the canonical artifacts")
        if any(
            not name
            or Path(name).name != name
            or not _is_sha256(digest)
            for name, digest in frozen.items()
        ):
            raise ValueError("artifact names or SHA-256 hashes are invalid")
        return frozen

    @field_validator("approved_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approved_at must include a timezone")
        return value.astimezone(timezone.utc)

    @field_validator("allowed_side_effects")
    @classmethod
    def keep_authority_fixed(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != DEFAULT_ALLOWED_SIDE_EFFECTS:
            raise ValueError("allowed_side_effects must match the approved profile")
        return value

    @field_serializer("artifact_hashes")
    def serialize_artifact_hashes(
        self,
        value: FrozenMapping,
    ) -> dict[str, str]:
        return dict(value)

    @field_serializer("approved_at", when_used="json")
    def serialize_approved_at(self, value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_sha256(value: str) -> bool:
    return (
        len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_reparse_point(path: Path) -> bool:
    metadata = path.lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _windows_name_key(name: str) -> str:
    return name.rstrip(" .").casefold()


def _stat_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _stable_signature(
    metadata: os.stat_result,
) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _normalize_files(
    files: Iterable[str | os.PathLike[str]],
) -> tuple[tuple[str, Path, os.stat_result], ...]:
    paths = tuple(Path(file) for file in files)
    if len(paths) != len(REQUIRED_ARTIFACT_NAMES):
        raise ValueError("approval requires exactly three artifact files")

    names = [path.name for path in paths]
    normalized_names = [_windows_name_key(name) for name in names]
    if len(normalized_names) != len(set(normalized_names)):
        raise ValueError("artifact file names collide under Windows rules")
    if set(names) != set(REQUIRED_ARTIFACT_NAMES):
        raise ValueError("artifact files must use the three canonical names")

    normalized: dict[str, tuple[Path, os.stat_result]] = {}
    parent: Path | None = None
    identities: set[tuple[int, int]] = set()
    for path in paths:
        try:
            current = Path(path.anchor)
            for component in path.absolute().parts[1:-1]:
                current /= component
                if current.exists() and _is_reparse_point(current):
                    raise ValueError(
                        f"artifact ancestor cannot be a reparse point: {path.name}"
                    )
            if _is_reparse_point(path):
                raise ValueError(f"artifact cannot be a link or reparse point: {path.name}")
            resolved = path.resolve(strict=True)
            metadata = resolved.stat()
            source_metadata = path.lstat()
        except FileNotFoundError as error:
            raise ValueError(f"artifact file is missing: {path.name}") from error
        except OSError as error:
            raise OSError(f"cannot inspect artifact file: {path.name}") from error
        if _stat_identity(source_metadata) != _stat_identity(metadata):
            raise ValueError(f"artifact identity changed or resolves through a link: {path.name}")
        if source_metadata.st_nlink > 1 or metadata.st_nlink > 1:
            raise ValueError(f"artifact cannot be a hardlink: {path.name}")
        resolved_parent = resolved.parent
        if parent is None:
            parent = resolved_parent
        elif resolved_parent != parent:
            raise ValueError("artifact files must be in the same directory")
        identity = _stat_identity(metadata)
        if identity in identities:
            raise ValueError("artifact files resolve to the same identity")
        identities.add(identity)
        normalized[path.name] = (resolved, metadata)
    return tuple(
        (name, *normalized[name])
        for name in REQUIRED_ARTIFACT_NAMES
    )


def _read_stable_bytes(path: Path, expected: os.stat_result) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            before = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or _stat_identity(before) != _stat_identity(expected)
            ):
                raise OSError(f"artifact identity changed before read: {path.name}")
            contents = handle.read()
            after = os.fstat(handle.fileno())
    except OSError as error:
        if "artifact" in str(error):
            raise
        raise OSError(f"cannot read artifact file: {path.name}") from error

    try:
        current = path.stat()
    except OSError as error:
        raise OSError(f"artifact changed during read: {path.name}") from error
    if (
        _stable_signature(before) != _stable_signature(after)
        or _stable_signature(after) != _stable_signature(current)
        or len(contents) != after.st_size
    ):
        raise OSError(f"artifact changed during read: {path.name}")
    return contents


def _hash_files(
    files: Iterable[str | os.PathLike[str]],
) -> FrozenMapping:
    artifacts = _normalize_files(files)
    hashes: dict[str, str] = {}
    for name, path, metadata in artifacts:
        contents = _read_stable_bytes(path, metadata)
        hashes[name] = hashlib.sha256(contents).hexdigest()
    return FrozenMapping(hashes)


def _trusted_username() -> str:
    if os.name == "nt":
        buffer = ctypes.create_unicode_buffer(257)
        size = wintypes.DWORD(len(buffer))
        get_username = ctypes.windll.advapi32.GetUserNameW
        if not get_username(buffer, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "GetUserNameW failed")
        return buffer.value
    return getpass.getuser()


def build_approval(
    *,
    run_id: str,
    project_id: str,
    config_hash: str,
    base_branch: str,
    base_sha: str,
    test_branch: str,
    files: Iterable[str | os.PathLike[str]],
    approved_user: str | None = None,
    user_provider: Callable[[], str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ApprovalSnapshot:
    """Hash every artifact before constructing one complete approval snapshot."""

    artifact_hashes = _hash_files(files)
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    trusted_user = (user_provider or _trusted_username)()
    if not trusted_user.strip():
        raise ValueError("trusted approved_user cannot be blank")
    if approved_user is not None and approved_user != trusted_user:
        raise ValueError("approved_user does not match the trusted current user")
    return ApprovalSnapshot(
        run_id=run_id,
        project_id=project_id,
        config_hash=config_hash,
        base_branch=base_branch,
        base_sha=base_sha,
        test_branch=test_branch,
        artifact_hashes=artifact_hashes,
        approved_user=trusted_user,
        approved_at=now,
    )


def verify_approval(
    approval: ApprovalSnapshot,
    files: Iterable[str | os.PathLike[str]],
    config_hash: str,
) -> bool:
    """Conservatively verify the current files and config against a snapshot."""

    try:
        current_hashes = _hash_files(files)
        if not isinstance(config_hash, str):
            return False
        if not hmac.compare_digest(
            approval.config_hash.encode("utf-8"),
            config_hash.encode("utf-8"),
        ):
            return False
        if set(current_hashes) != set(approval.artifact_hashes):
            return False
        return all(
            hmac.compare_digest(
                approval.artifact_hashes[name],
                current_hashes[name],
            )
            for name in approval.artifact_hashes
        )
    except (OSError, ValueError, TypeError, UnicodeError):
        return False


def capture_approval_artifacts(
    approval: ApprovalSnapshot,
    files: Iterable[str | os.PathLike[str]],
    config_hash: str,
) -> dict[str, bytes]:
    """Capture the exact approved bytes after stable identity and digest checks."""

    artifacts = _normalize_files(files)
    if not hmac.compare_digest(approval.config_hash, config_hash):
        raise ValueError("approval config hash changed")
    captured: dict[str, bytes] = {}
    for name, path, metadata in artifacts:
        contents = _read_stable_bytes(path, metadata)
        digest = hashlib.sha256(contents).hexdigest()
        if not hmac.compare_digest(approval.artifact_hashes[name], digest):
            raise ValueError(f"approval artifact changed: {name}")
        captured[name] = contents
    return captured


def _atomic_replace(source: Path, target: Path) -> None:
    source.replace(target)


def _remove_temporary(path: Path) -> None:
    path.unlink()


def save_approval(path: str | os.PathLike[str], approval: ApprovalSnapshot) -> None:
    """Durably write stable JSON, then atomically replace the destination."""

    destination = Path(path)
    payload = approval.model_dump(mode="json")
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    temporary_path: Path | None = None
    primary_error: BaseException | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
        _atomic_replace(temporary_path, destination)
        temporary_path = None
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if temporary_path is not None:
            try:
                _remove_temporary(temporary_path)
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                if primary_error is None:
                    raise
                primary_error.add_note(
                    f"temporary approval cleanup failed: {cleanup_error}"
                )


def load_approval(path: str | os.PathLike[str]) -> ApprovalSnapshot:
    """Load and strictly validate a persisted approval snapshot."""

    source = Path(path)
    try:
        raw: Any = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"approval file contains invalid JSON: {source.name}") from error
    return ApprovalSnapshot.model_validate(raw)
