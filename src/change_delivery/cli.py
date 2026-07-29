"""Deterministic JSON command line surface for Change Delivery."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
import hmac
import hashlib
import json
import os
from pathlib import Path
import stat
import shutil
import sys
import tempfile
from typing import Any, Iterator, TextIO
from uuid import uuid4

from change_delivery.config import load_project_config, load_project_config_bytes
from change_delivery.adapters.git import GitAdapter, GitError
from change_delivery.adapters.github import GitHubAdapter
from change_delivery.approval import (
    REQUIRED_ARTIFACT_NAMES,
    load_approval,
    save_approval,
    verify_approval,
)
from change_delivery.database import Database
from change_delivery.domain import LoadedProjectConfig, RunState
from change_delivery.repository import RunRepository
from change_delivery.redaction import redact_text
from change_delivery.orchestrator import Orchestrator
from change_delivery.process import ProcessRunner


MAX_REQUIREMENT_BYTES = 1_048_576


class CliUsageError(ValueError):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


@dataclass(frozen=True)
class StableFileSnapshot:
    data: bytes
    signature: tuple[int, int, int, int]


@contextmanager
def _candidate_lease(
    path: Path,
    *,
    limit: int,
) -> Iterator[tuple[StableFileSnapshot, Callable[[], None]]]:
    metadata = path.lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    if (
        path.is_symlink()
        or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise CliUsageError("input must be a regular non-reparse file")
    if os.name != "nt":
        snapshot = _stable_snapshot(path, limit=limit)
        yield snapshot, lambda: _assert_snapshot_unchanged(path, snapshot, limit)
        return
    import msvcrt

    handle = path.open("rb")
    opened = os.fstat(handle.fileno())
    if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
        handle.close()
        raise CliUsageError("candidate identity changed before validation")
    locked_length = max(1, opened.st_size)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, locked_length)
        data = handle.read(limit + 1)
        metadata = os.fstat(handle.fileno())
        if len(data) > limit:
            raise CliUsageError("input is too large")
        snapshot = StableFileSnapshot(
            data=data,
            signature=(
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
            ),
        )

        def verify() -> None:
            handle.seek(0)
            current = handle.read(limit + 1)
            current_metadata = os.fstat(handle.fileno())
            current_signature = (
                current_metadata.st_dev,
                current_metadata.st_ino,
                current_metadata.st_size,
                current_metadata.st_mtime_ns,
            )
            if (
                current_signature != snapshot.signature
                or not hmac.compare_digest(current, snapshot.data)
            ):
                raise CliUsageError("candidate changed while being validated")

        yield snapshot, verify
    finally:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, locked_length)
        finally:
            handle.close()


def _assert_snapshot_unchanged(
    path: Path,
    expected: StableFileSnapshot,
    limit: int,
) -> None:
    current = _stable_snapshot(path, limit=limit)
    if (
        current.signature != expected.signature
        or not hmac.compare_digest(current.data, expected.data)
    ):
        raise CliUsageError("candidate changed while being validated")


class LocalRegistrationVerifier:
    """Read-only local checks; Task11 adds the remote HTTP/version checks."""

    def __call__(self, loaded: LoadedProjectConfig) -> tuple[bool, str]:
        config = loaded.config
        try:
            if config.base_branch == config.test_branch:
                return False, "base and test branches must differ"
            for executable in ("git", "gh", "codex"):
                if shutil.which(executable) is None:
                    return False, f"required tool is unavailable: {executable}"
            adapter = GitAdapter(
                config.repo_path,
                remote=config.remote,
                base_branch=config.base_branch,
                test_branch=config.test_branch,
                expected_fetch_url=config.remote_fetch_url,
                expected_push_url=config.remote_push_url,
            )
            adapter.remote_sha(config.remote, config.base_branch)
            adapter.remote_sha(config.remote, config.test_branch)
        except Exception as error:
            return False, f"local registration verification failed: {type(error).__name__}"
        return True, "local repository and tool bindings verified"


class RuntimeGitPort:
    """Bind orchestrator feature-branch operations to the verified Git adapter."""

    def __init__(self, config: Any, plugin_root: Path) -> None:
        self._config = config
        self._plugin_root = plugin_root
        self._adapter = GitAdapter(
            config.repo_path,
            remote=config.remote,
            base_branch=config.base_branch,
            test_branch=config.test_branch,
            expected_fetch_url=config.remote_fetch_url,
            expected_push_url=config.remote_push_url,
        )

    def feature_sha(self, branch: str) -> str | None:
        self._adapter.fetch(self._config.remote)
        try:
            return self._adapter.remote_sha(self._config.remote, branch)
        except GitError as error:
            if error.operation == "remote_sha":
                return None
            raise

    def push_feature(self, branch: str, expected_sha: str) -> str:
        run_id = branch.removeprefix("feature/")
        if not run_id or branch != f"feature/{run_id}":
            raise ValueError("feature branch is not bound to a run")
        worktree = _child(self._plugin_root, "worktrees", run_id)
        pushed = self._adapter.push_feature(
            worktree,
            self._config.remote,
            branch,
        )
        if not hmac.compare_digest(pushed, expected_sha):
            raise ValueError("local feature head changed before push")
        return pushed


def _environment_secrets(names: tuple[str, ...]) -> dict[str, str]:
    return {
        name: value
        for name in names
        if (value := os.environ.get(name)) is not None
    }


def _github_port(config: Any) -> GitHubAdapter:
    return GitHubAdapter(ProcessRunner(_environment_secrets), config)


@dataclass
class CliServices:
    plugin_root: Path
    repository: RunRepository | None = None
    project_loader: Callable[[str], tuple[Any, str]] | None = None
    registration_verifier: (
        Callable[[LoadedProjectConfig], tuple[bool, str]] | None
    ) = None
    orchestrator: Any | None = None

    def __post_init__(self) -> None:
        self.plugin_root = self.plugin_root.resolve(strict=False)
        if self.registration_verifier is None:
            self.registration_verifier = LocalRegistrationVerifier()

    def load_project(self, project_id: str) -> tuple[Any, str]:
        if self.project_loader is not None:
            return self.project_loader(project_id)
        loaded = load_project_config(
            _child(self.plugin_root, "projects", f"{project_id}.yaml")
        )
        return loaded.config, loaded.config_hash

    def get_repository(self, *, create: bool = False) -> RunRepository:
        if self.repository is not None:
            return self.repository
        path = _child(self.plugin_root, "state.sqlite3")
        if not path.exists() and not create:
            raise CliUsageError("runtime database is not configured")
        database = Database(path)
        if create:
            database.migrate()
        self.repository = RunRepository(database)
        return self.repository

    def get_orchestrator(self) -> Any:
        if self.orchestrator is None:
            repository = self.get_repository()
            self.orchestrator = Orchestrator(
                repository=repository,
                plugin_root=self.plugin_root,
                config_loader=self.load_project,
                git_factory=lambda config: RuntimeGitPort(
                    config,
                    self.plugin_root,
                ),
                github_factory=_github_port,
            )
        return self.orchestrator


def _default_services() -> CliServices:
    profile = os.environ.get("USERPROFILE")
    if not profile:
        raise CliUsageError("USERPROFILE is unavailable")
    return CliServices(Path(profile) / ".codex" / "change-delivery")


def _child(root: Path, *parts: str) -> Path:
    base = root.resolve()
    candidate = base.joinpath(*parts)
    parent = candidate.parent.resolve(strict=False)
    try:
        parent.relative_to(base)
    except ValueError as error:
        raise CliUsageError("path escapes plugin root") from error
    if candidate.exists() and candidate.is_symlink():
        raise CliUsageError("links and reparse points are not allowed")
    return candidate


def _stable_read(path: Path, *, limit: int | None = None) -> bytes:
    return _stable_snapshot(path, limit=limit).data


def _stable_snapshot(
    path: Path,
    *,
    limit: int | None = None,
) -> StableFileSnapshot:
    if path.is_symlink():
        raise CliUsageError("links and reparse points are not allowed")
    metadata = path.lstat()
    attributes = getattr(metadata, "st_file_attributes", 0)
    if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        raise CliUsageError("links and reparse points are not allowed")
    if not stat.S_ISREG(metadata.st_mode):
        raise CliUsageError("input must be a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise CliUsageError("input identity changed before validation")
        data = handle.read((limit + 1) if limit is not None else -1)
        after = os.fstat(handle.fileno())
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise CliUsageError("input changed while being read")
    if limit is not None and len(data) > limit:
        raise CliUsageError("input is too large")
    return StableFileSnapshot(
        data=data,
        signature=(
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ),
    )


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(
        prog="change-delivery",
        description="Change Delivery local delivery orchestrator for Codex.",
    )
    parser.add_argument("--json", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str) -> argparse.ArgumentParser:
        child = commands.add_parser(name)
        child.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        return child

    start = command("start")
    start.add_argument("--project", required=True)
    start.add_argument("--requirement-file", type=Path, required=True)
    start.add_argument("--dry-run", action="store_true")
    register = command("register")
    register.add_argument("--candidate-file", type=Path, required=True)
    register.add_argument("--confirm-hash", required=True)
    approve = command("approve")
    approve.add_argument("run_id")
    approve.add_argument("--approval-file", type=Path, required=True)
    status = command("status")
    status.add_argument("--run-id")
    resume = command("resume")
    resume.add_argument("run_id")
    cancel = command("cancel")
    cancel.add_argument("run_id")
    doctor = command("doctor")
    doctor.add_argument("--project")
    tick = command("tick")
    tick.add_argument("--plugin-root", type=Path)
    release = command("release")
    release.add_argument("run_id")
    return parser


def _start(args: argparse.Namespace, services: CliServices) -> dict[str, Any]:
    project, digest = services.load_project(args.project)
    contents = _stable_read(args.requirement_file, limit=MAX_REQUIREMENT_BYTES)
    contents.decode("utf-8")
    run_id = f"run-{uuid4().hex}"
    run_root = _child(services.plugin_root, "runs", run_id)
    run_root.mkdir(parents=True, exist_ok=False)
    try:
        _atomic_write(run_root / "requirement.md", contents)
        _atomic_write(
            run_root / "design.md",
            b"# Design\n\nPending explicit design approval.\n",
        )
        _atomic_write(
            run_root / "implementation-plan.md",
            b"# Implementation Plan\n\nPending explicit design approval.\n",
        )
        run = services.get_repository(create=True).create_run(
            run_id,
            project.project_id,
            digest,
        )
    except BaseException:
        # Files remain as audit evidence if DB creation became uncertain.
        raise
    return {
        "ok": True,
        "run_id": run.run_id,
        "state": run.state.value,
        "dry_run": bool(args.dry_run),
    }


def _register(args: argparse.Namespace, services: CliServices) -> dict[str, Any]:
    with _candidate_lease(
        args.candidate_file,
        limit=MAX_REQUIREMENT_BYTES,
    ) as (initial, verify_unchanged):
        loaded = load_project_config_bytes(initial.data, args.candidate_file)
        verifier = services.registration_verifier
        if verifier is None:
            raise CliUsageError("registration verifier is unavailable until Task11")
        verified, reason = verifier(loaded)
        if not verified:
            raise CliUsageError(reason or "registration doctor failed")
        if not hmac.compare_digest(args.confirm_hash, loaded.config_hash):
            raise CliUsageError("confirmation hash does not match candidate")
        verify_unchanged()
    target = _child(
        services.plugin_root,
        "projects",
        f"{loaded.config.project_id}.yaml",
    )
    _atomic_write(target, initial.data)
    return {
        "ok": True,
        "project_id": loaded.config.project_id,
        "config_hash": loaded.config_hash,
    }


def _status(args: argparse.Namespace, services: CliServices) -> dict[str, Any]:
    repository = services.get_repository()
    if args.run_id:
        runs = [repository.get_run(args.run_id)]
    else:
        runs = repository.list_active_runs()
    return {
        "ok": True,
        "runs": [run.model_dump(mode="json") for run in runs],
    }


def _approve(args: argparse.Namespace, services: CliServices) -> dict[str, Any]:
    repository = services.get_repository()
    run = repository.get_run(args.run_id)
    if run.state is not RunState.AWAITING_DESIGN_APPROVAL:
        raise CliUsageError("run is not awaiting design approval")
    run_root = _child(services.plugin_root, "runs", run.run_id)
    expected = run_root / "approval.json"
    if (
        args.approval_file.is_symlink()
        or args.approval_file.resolve(strict=True)
        != expected.resolve(strict=True)
    ):
        raise CliUsageError("approval must be the exact run approval.json")
    project, digest = services.load_project(run.project_id)
    if not hmac.compare_digest(digest, run.config_hash):
        raise CliUsageError("project configuration hash changed")
    snapshot = load_approval(expected)
    if (
        snapshot.run_id != run.run_id
        or snapshot.project_id != run.project_id
        or snapshot.base_branch != project.base_branch
        or snapshot.test_branch != project.test_branch
    ):
        raise CliUsageError("approval identity does not match run")
    artifacts = tuple(run_root / name for name in REQUIRED_ARTIFACT_NAMES)
    if not verify_approval(snapshot, artifacts, run.config_hash):
        raise CliUsageError("approval snapshot or artifacts changed")
    save_approval(expected, snapshot)
    contents = _stable_read(expected, limit=MAX_REQUIREMENT_BYTES)
    file_sha = hashlib.sha256(contents).hexdigest()
    payload = json.loads(contents)
    repository.approve_with_evidence(
        run.run_id,
        approval_payload=payload,
        approval_sha256=file_sha,
        evidence_id=f"explicit-approval:{run.run_id}:{file_sha}",
    )
    return {
        "ok": True,
        "run_id": run.run_id,
        "state": RunState.APPROVED.value,
        "approval_sha256": file_sha,
    }


def _doctor(args: argparse.Namespace, services: CliServices) -> dict[str, Any]:
    if not services.plugin_root.exists():
        raise CliUsageError("runtime is not configured")
    registry = services.plugin_root / "projects"
    if not registry.is_dir():
        raise CliUsageError("project registry is not configured")
    paths = (
        [_child(registry, f"{args.project}.yaml")]
        if args.project
        else sorted(registry.glob("*.yaml"))
    )
    if not paths:
        raise CliUsageError("project registry is empty")
    evidence = []
    verifier = services.registration_verifier
    assert verifier is not None
    for path in paths:
        snapshot = _stable_snapshot(path, limit=MAX_REQUIREMENT_BYTES)
        loaded = load_project_config_bytes(snapshot.data, path)
        verified, reason = verifier(loaded)
        evidence.append(
            {
                "project_id": loaded.config.project_id,
                "config_hash": loaded.config_hash,
                "verified": verified,
                "reason": reason,
            }
        )
        if not verified:
            raise CliUsageError(reason)
    return {
        "ok": True,
        "project": args.project,
        "mode": "read_only",
        "evidence": evidence,
    }


def _orchestrator_action(
    action: str,
    run_id: str,
    services: CliServices,
) -> dict[str, Any]:
    orchestrator = services.get_orchestrator()
    outcome = getattr(orchestrator, action)(run_id)
    return {"ok": True, **outcome.model_dump(mode="json")}


def _execute(args: argparse.Namespace, services: CliServices) -> dict[str, Any]:
    if args.command == "start":
        return _start(args, services)
    if args.command == "register":
        return _register(args, services)
    if args.command == "status":
        return _status(args, services)
    if args.command == "approve":
        return _approve(args, services)
    if args.command == "resume":
        return _orchestrator_action("resume", args.run_id, services)
    if args.command == "cancel":
        return _orchestrator_action("cancel", args.run_id, services)
    if args.command == "doctor":
        return _doctor(args, services)
    if args.command == "tick":
        if args.plugin_root is not None:
            services.plugin_root = args.plugin_root
        repository = services.get_repository()
        runs = repository.list_active_runs()
        outcome = None
        if runs:
            outcome = _orchestrator_action("step", runs[0].run_id, services)
        return {"ok": True, "processed": 1 if outcome else 0, "outcome": outcome}
    if args.command == "release":
        raise CliUsageError("正式发布尚未启用/Task14")
    raise CliUsageError("unknown command")


def main(
    argv: Sequence[str] | None = None,
    *,
    services: CliServices | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    parser = _parser()
    try:
        with redirect_stdout(output), redirect_stderr(errors):
            args = parser.parse_args(argv)
        result = _execute(args, services or _default_services())
    except SystemExit as exit_error:
        return int(exit_error.code)
    except Exception as error:
        safe_error = redact_text(
            str(error) or type(error).__name__,
            {},
        )
        payload = {
            "ok": False,
            "error": safe_error,
            "error_type": type(error).__name__,
        }
        errors.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        return 2
    output.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return 0
