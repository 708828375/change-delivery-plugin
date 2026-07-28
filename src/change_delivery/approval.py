"""Immutable approval snapshots and their on-disk representation."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
from typing import Any, TypeVar

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

_Value = TypeVar("_Value")


class FrozenDict(dict[str, _Value]):
    """A JSON-friendly mapping that cannot be mutated after construction."""

    def _immutable(self, *args: object, **kwargs: object) -> None:
        raise TypeError("approval artifact hashes are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


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
    artifact_hashes: FrozenDict[str]
    approved_user: str = Field(min_length=1)
    approved_at: datetime
    allowed_side_effects: tuple[str, ...] = DEFAULT_ALLOWED_SIDE_EFFECTS

    @field_validator("run_id", "project_id", "config_hash", "base_branch",
                     "test_branch", "approved_user")
    @classmethod
    def reject_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("approval identifiers cannot be blank")
        return value

    @field_validator("artifact_hashes", mode="before")
    @classmethod
    def freeze_and_validate_hashes(
        cls,
        value: object,
    ) -> FrozenDict[str]:
        if not isinstance(value, Mapping):
            raise ValueError("artifact_hashes must be a mapping")
        frozen = FrozenDict(
            {
                str(name): str(digest)
                for name, digest in value.items()
            }
        )
        if not frozen:
            raise ValueError("artifact_hashes cannot be empty")
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
            return value.replace(tzinfo=timezone.utc)
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
        value: FrozenDict[str],
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


def _normalize_files(files: Iterable[str | os.PathLike[str]]) -> tuple[Path, ...]:
    paths = tuple(Path(file) for file in files)
    if not paths:
        raise ValueError("artifact files cannot be empty")
    names = [path.name for path in paths]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise ValueError("artifact file names must be non-empty and unique")
    return paths


def _hash_files(
    files: Iterable[str | os.PathLike[str]],
) -> FrozenDict[str]:
    paths = _normalize_files(files)
    hashes: dict[str, str] = {}
    for path in paths:
        try:
            if not path.is_file():
                raise ValueError(f"artifact file is missing or not a file: {path.name}")
            contents = path.read_bytes()
        except OSError as error:
            raise OSError(f"cannot read artifact file: {path.name}") from error
        hashes[path.name] = hashlib.sha256(contents).hexdigest()
    return FrozenDict(hashes)


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
    clock: Callable[[], datetime] | None = None,
) -> ApprovalSnapshot:
    """Hash every artifact before constructing one complete approval snapshot."""

    artifact_hashes = _hash_files(files)
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    return ApprovalSnapshot(
        run_id=run_id,
        project_id=project_id,
        config_hash=config_hash,
        base_branch=base_branch,
        base_sha=base_sha,
        test_branch=test_branch,
        artifact_hashes=artifact_hashes,
        approved_user=approved_user or getpass.getuser(),
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
    except (OSError, ValueError, TypeError):
        return False
    if not hmac.compare_digest(approval.config_hash, config_hash):
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


def _atomic_replace(source: Path, target: Path) -> None:
    source.replace(target)


def save_approval(path: str | os.PathLike[str], approval: ApprovalSnapshot) -> None:
    """Durably write stable JSON, then atomically replace the destination."""

    destination = Path(path)
    payload = approval.model_dump(mode="json")
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    temporary_path: Path | None = None
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
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def load_approval(path: str | os.PathLike[str]) -> ApprovalSnapshot:
    """Load and strictly validate a persisted approval snapshot."""

    source = Path(path)
    try:
        raw: Any = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"approval file contains invalid JSON: {source.name}") from error
    return ApprovalSnapshot.model_validate(raw)
