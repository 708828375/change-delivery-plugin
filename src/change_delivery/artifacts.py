from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
from uuid import uuid4


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FILES = (
    "requirement.md",
    "design.md",
    "implementation-plan.md",
    "approval.json",
    "sanitized-summary.md",
)
_DIRECTORIES = ("evidence", "reports")


def _path_is_reparse(path: Path) -> bool:
    metadata = path.lstat()
    return bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def validate_runtime_root(
    runtime_root: Path,
    *,
    require_exists: bool,
) -> Path:
    """Validate lexical root identity and every existing ancestor."""

    lexical = runtime_root.absolute()
    for candidate in (lexical, *lexical.parents):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if (
            candidate.is_symlink()
            or _path_is_reparse(candidate)
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise ValueError("runtime root ancestry contains a link, reparse point, or non-directory")
    if require_exists and not lexical.exists():
        raise FileNotFoundError(lexical)
    return lexical


@dataclass(frozen=True)
class RunArtifacts:
    run_dir: Path
    requirement: Path
    design: Path
    plan: Path
    approval: Path
    evidence_dir: Path
    reports_dir: Path
    sanitized_summary: Path


class ArtifactStore:
    """Owns the small, fixed filesystem surface of a delivery run."""

    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = validate_runtime_root(runtime_root, require_exists=False)

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        metadata = path.lstat()
        return bool(
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )

    def _require_directory(self, path: Path, *, create: bool = False) -> Path:
        if create:
            path.mkdir(exist_ok=True)
        metadata = path.lstat()
        if (
            path.is_symlink()
            or self._is_reparse(path)
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise ValueError(f"artifact path is not a regular non-reparse directory: {path.name}")
        return path.resolve(strict=True)

    def _root(self) -> Path:
        validate_runtime_root(self.runtime_root, require_exists=False)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        validate_runtime_root(self.runtime_root, require_exists=True)
        return self._require_directory(self.runtime_root)

    def _run_dir(self, run_id: str, *, create: bool = False) -> Path:
        if not _RUN_ID.fullmatch(run_id) or run_id in {".", ".."}:
            raise ValueError("run_id is not canonical")
        root = self._root()
        runs = self.runtime_root / "runs"
        runs_resolved = self._require_directory(runs, create=create)
        candidate = runs / run_id
        run_resolved = self._require_directory(candidate, create=create)
        if runs_resolved.parent != root:
            raise ValueError("runs directory escapes runtime root")
        try:
            run_resolved.relative_to(root)
        except ValueError as error:
            raise ValueError("run directory escapes runtime root") from error
        if run_resolved.parent != runs_resolved:
            raise ValueError("run directory has an unexpected parent")
        return candidate

    def _require_regular_file(self, path: Path, *, create: bool = False) -> None:
        if create and not path.exists():
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        metadata = path.lstat()
        if (
            path.is_symlink()
            or self._is_reparse(path)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or path.resolve(strict=True) != path.absolute()
        ):
            raise ValueError(f"artifact file is unsafe: {path.name}")

    def create_run(self, run_id: str) -> RunArtifacts:
        if not _RUN_ID.fullmatch(run_id) or run_id in {".", ".."}:
            raise ValueError("run_id is not canonical")
        self._root()
        runs = self.runtime_root / "runs"
        self._require_directory(runs, create=True)
        run_dir = runs / run_id
        run_dir.mkdir(exist_ok=False)
        try:
            self._run_dir(run_id)
            for name in _DIRECTORIES:
                directory = run_dir / name
                resolved = self._require_directory(directory, create=True)
                if resolved.parent != run_dir.resolve(strict=True):
                    raise ValueError("artifact directory escapes run directory")
            for name in _FILES:
                self._require_regular_file(run_dir / name, create=True)
            return self.paths(run_id)
        except BaseException:
            self.discard_new_run(run_id)
            raise

    def discard_new_run(self, run_id: str) -> None:
        run_dir = self._run_dir(run_id)
        expected = {*_FILES, *_DIRECTORIES}
        actual = {item.name for item in run_dir.iterdir()}
        if not actual <= expected:
            raise ValueError("refusing to remove run directory with unknown entries")
        for name in _FILES:
            path = run_dir / name
            if path.exists():
                self._require_regular_file(path)
                path.unlink()
        for name in _DIRECTORIES:
            directory = run_dir / name
            if directory.exists():
                self._require_directory(directory)
                if any(directory.iterdir()):
                    raise ValueError("refusing to remove non-empty artifact directory")
                directory.rmdir()
        run_dir.rmdir()

    def paths(self, run_id: str) -> RunArtifacts:
        run_dir = self._run_dir(run_id)
        return RunArtifacts(
            run_dir=run_dir,
            requirement=run_dir / "requirement.md",
            design=run_dir / "design.md",
            plan=run_dir / "implementation-plan.md",
            approval=run_dir / "approval.json",
            evidence_dir=run_dir / "evidence",
            reports_dir=run_dir / "reports",
            sanitized_summary=run_dir / "sanitized-summary.md",
        )

    def require_pinnable_summary(self, run_id: str) -> Path:
        """Return an existing summary only when its full path is safe to pin."""
        root = self._root()
        run_dir = self._run_dir(run_id)
        summary = run_dir / "sanitized-summary.md"
        self._require_regular_file(summary)
        resolved = summary.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise ValueError("sanitized summary escapes runtime root") from error
        if resolved.parent != run_dir.resolve(strict=True):
            raise ValueError("sanitized summary has an unexpected parent")
        return summary

    def write_resource_inventory(
        self,
        run_id: str,
        *,
        branch: str | None,
        worktree: Path | None,
        pr_url: str | None,
        test_sha: str | None,
        pr_number: int | None = None,
    ) -> Path:
        paths = self.paths(run_id)
        self._require_directory(paths.reports_dir, create=True)
        target = paths.reports_dir / "resource-inventory.md"
        if target.exists():
            self._require_regular_file(target)
        values = (
            ("Run", run_id),
            ("Branch", branch or "none"),
            ("Worktree", str(worktree) if worktree is not None else "none"),
            ("PR", pr_url or "none"),
            ("PR number", f"#{pr_number}" if pr_number is not None else "none"),
            ("Test SHA", test_sha or "none"),
        )
        content = "# Preserved resources\n\n" + "\n".join(
            f"- {label}: {value}" for label, value in values
        ) + "\n"
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            self._require_regular_file(temporary)
            os.replace(temporary, target)
            self._require_regular_file(target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return target
