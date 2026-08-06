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
import re
import stat
import shutil
import sys
import tempfile
from typing import Any, Iterator, TextIO
from uuid import uuid4

from filelock import FileLock, Timeout

from change_delivery.artifacts import ArtifactStore, validate_runtime_root
from change_delivery.config import load_project_config, load_project_config_bytes
from change_delivery.adapters.git import GitAdapter, GitError
from change_delivery.adapters.github import GitHubAdapter
from change_delivery.adapters.http import ReadOnlyHttpClient
from change_delivery.adapters.project import (
    ExampleProjectAdapter,
    enforce_project_test_endpoint,
)
from change_delivery.approval import (
    ApprovalSnapshot,
    REQUIRED_ARTIFACT_NAMES,
    build_approval,
    capture_approval_artifacts,
    save_approval,
    verify_approval,
)
from change_delivery.database import Database
from change_delivery.domain import LoadedProjectConfig, RunState
from change_delivery.repository import RunRepository
from change_delivery.retention import maintenance_lock
from change_delivery.redaction import redact_text
from change_delivery.orchestrator import Orchestrator
from change_delivery.process import ProcessRunner
from change_delivery.runtime import (
    RuntimeAcceptancePort,
    RuntimeCleanupPort,
    RuntimeCommitPort,
    RuntimeGitWorkspacePort,
    RuntimeStageExecutor,
    RuntimeTestEnvironmentPort,
)
from change_delivery.scheduler import Scheduler
from change_delivery.release import ReleaseService


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
        or metadata.st_nlink > 1
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
            for executable in ("git", "codex"):
                if shutil.which(executable) is None:
                    return False, f"required tool is unavailable: {executable}"
            _github_port(config)
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
            expected_sha,
        )
        return pushed


def _environment_secrets(names: tuple[str, ...]) -> dict[str, str]:
    return {
        name: value
        for name in names
        if name in {"GH_TOKEN", "GITHUB_TOKEN"}
        and (value := os.environ.get(name)) is not None
    }


def _github_fixed_environment(names: tuple[str, ...]) -> dict[str, str]:
    if "GH_CONFIG_DIR" not in names:
        return {}
    profile_value = os.environ.get("USERPROFILE")
    if not profile_value:
        raise CliUsageError("USERPROFILE is unavailable for GitHub authentication")
    profile = Path(profile_value).resolve(strict=True)
    if not profile.is_dir():
        raise CliUsageError("USERPROFILE is not a trusted directory")
    expected_appdata = (profile / "AppData" / "Roaming").resolve(strict=True)
    appdata_value = os.environ.get("APPDATA")
    if appdata_value is not None:
        appdata = Path(appdata_value).resolve(strict=True)
        if appdata != expected_appdata:
            raise CliUsageError("APPDATA is not bound to USERPROFILE")
    config_dir = (expected_appdata / "GitHub CLI").resolve(strict=True)
    if not config_dir.is_dir() or config_dir.parent != expected_appdata:
        raise CliUsageError("GitHub CLI configuration directory is unavailable")
    return {"GH_CONFIG_DIR": str(config_dir)}


def _github_executable_binding() -> tuple[Path, str]:
    path_value = os.environ.get("CHANGE_DELIVERY_GH_EXECUTABLE")
    digest = os.environ.get("CHANGE_DELIVERY_GH_SHA256")
    if not path_value:
        if os.name != "nt":
            raise CliUsageError("trusted GitHub CLI executable is not configured")
        path_value = r"C:\Program Files\GitHub CLI\gh.exe"
        try:
            with Path(path_value).open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
        except OSError:
            raise CliUsageError(
                "trusted GitHub CLI executable is not configured"
            ) from None
    path = Path(path_value)
    if not path.is_absolute():
        raise CliUsageError("trusted GitHub CLI executable must be absolute")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise CliUsageError("trusted GitHub CLI SHA-256 is not configured")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise CliUsageError("trusted GitHub CLI executable is unavailable") from None
    return resolved, digest


def _github_port(config: Any) -> GitHubAdapter:
    executable, digest = _github_executable_binding()
    return GitHubAdapter(
        ProcessRunner(
            _environment_secrets,
            fixed_environment_provider=_github_fixed_environment,
        ),
        config,
        gh_executable=executable,
        gh_sha256=digest,
    )


@dataclass
class CliServices:
    plugin_root: Path
    repository: RunRepository | None = None
    project_loader: Callable[[str], tuple[Any, str]] | None = None
    registration_verifier: (
        Callable[[LoadedProjectConfig], tuple[bool, str]] | None
    ) = None
    orchestrator: Any | None = None
    scheduler: Any | None = None
    base_sha_provider: Callable[[Any], str] | None = None
    release_service: Any | None = None
    discovery_factory: Callable[[Any], Any] | None = None
    discovery_http_factory: Callable[[], Any] | None = None
    discovery_git_runner: Any | None = None
    discovery_github_factory: Callable[[Any], Any] | None = None

    def __post_init__(self) -> None:
        self.plugin_root = validate_runtime_root(
            self.plugin_root, require_exists=False
        )
        if self.registration_verifier is None:
            self.registration_verifier = LocalRegistrationVerifier()
        if self.base_sha_provider is None:
            self.base_sha_provider = _registered_base_sha
        if self.discovery_http_factory is None:
            self.discovery_http_factory = ReadOnlyHttpClient
        if self.discovery_factory is None:
            http_factory = self.discovery_http_factory
            runner = self.discovery_git_runner
            github_factory = self.discovery_github_factory or _github_port
            self.discovery_factory = lambda config: _project_discovery(
                config,
                http=http_factory(),
                runner=runner,
                github=github_factory(config),
            )

    def load_project(self, project_id: str) -> tuple[Any, str]:
        if self.project_loader is not None:
            config, digest = self.project_loader(project_id)
        else:
            loaded = load_project_config(
                _child(self.plugin_root, "projects", f"{project_id}.yaml")
            )
            config, digest = loaded.config, loaded.config_hash
        if config.project_id != project_id:
            raise CliUsageError(
                "requested project identity does not match loaded configuration"
            )
        return config, digest

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
            stage_executor = RuntimeStageExecutor(
                repository,
                self.plugin_root,
            )
            workspace = RuntimeGitWorkspacePort(
                repository,
                self.plugin_root,
            )
            self.orchestrator = Orchestrator(
                repository=repository,
                plugin_root=self.plugin_root,
                config_loader=self.load_project,
                git_factory=lambda config: RuntimeGitPort(
                    config,
                    self.plugin_root,
                ),
                github_factory=_github_port,
                stage_executor=stage_executor,
                test_environment=RuntimeTestEnvironmentPort(repository),
                acceptance_executor=RuntimeAcceptancePort(
                    repository,
                    self.plugin_root,
                    stage_executor,
                ),
                worktree_port=workspace,
                commit_port=RuntimeCommitPort(workspace),
                cleanup_port=RuntimeCleanupPort(workspace),
            )
        return self.orchestrator

    def get_scheduler(self) -> Any:
        if self.scheduler is None:
            self.scheduler = Scheduler(
                runtime_root=self.plugin_root,
                repository=self.get_repository(),
                orchestrator=self.get_orchestrator(),
            )
        return self.scheduler

    def get_release_service(self) -> Any:
        if self.release_service is None:
            self.release_service = ReleaseService(
                repository=self.get_repository(),
                config_loader=self.load_project,
                github_factory=_github_port,
            )
        return self.release_service


def _default_services() -> CliServices:
    profile = os.environ.get("USERPROFILE")
    if not profile:
        raise CliUsageError("USERPROFILE is unavailable")
    return CliServices(Path(profile) / ".codex" / "change-delivery")


def _registered_base_sha(config: Any) -> str:
    adapter = GitAdapter(
        config.repo_path,
        remote=config.remote,
        base_branch=config.base_branch,
        test_branch=config.test_branch,
        expected_fetch_url=config.remote_fetch_url,
        expected_push_url=config.remote_push_url,
    )
    adapter.fetch(config.remote)
    return adapter.remote_sha(config.remote, config.base_branch)


def _project_discovery(
    config: Any,
    *,
    http: Any,
    runner: Any | None = None,
    github: Any | None = None,
) -> ExampleProjectAdapter:
    if github is None:
        github = _github_port(config)
    return ExampleProjectAdapter(
        config,
        git=GitAdapter(
            config.repo_path,
            remote=config.remote,
            base_branch=config.base_branch,
            test_branch=config.test_branch,
            expected_fetch_url=config.remote_fetch_url,
            expected_push_url=config.remote_push_url,
            runner=runner,
            discovery_only=True,
        ),
        github=github,
        http=http,
    )


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
    if metadata.st_nlink > 1:
        raise CliUsageError("hardlinked input files are not allowed")
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


def _require_safe_directory_chain(root: Path, target: Path) -> None:
    base = root.resolve(strict=True)
    destination = target.resolve(strict=True)
    try:
        relative = destination.relative_to(base)
    except ValueError as error:
        raise CliUsageError("run path escapes runtime root") from error
    current = base
    for part in relative.parts:
        current = current / part
        metadata = current.lstat()
        attributes = getattr(metadata, "st_file_attributes", 0)
        if (
            current.is_symlink()
            or attributes
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise CliUsageError("run path must use non-reparse directories")


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
    approve.add_argument("--confirm-hash", required=True)
    prepare_approval = command("prepare-approval")
    prepare_approval.add_argument("run_id")
    status = command("status")
    status.add_argument("--run-id")
    resume = command("resume")
    resume.add_argument("run_id")
    run = command("run")
    run.add_argument("run_id")
    cancel = command("cancel")
    cancel.add_argument("run_id")
    pin = command("pin")
    pin.add_argument("run_id")
    pin.add_argument("--off", action="store_true")
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
    if args.dry_run:
        return _start_dry_run(project, services)
    run_id = f"run-{uuid4().hex}"
    store = ArtifactStore(services.plugin_root)
    repository = services.get_repository(create=True)
    paths = store.create_run(run_id)
    try:
        _atomic_write(paths.requirement, contents)
        _atomic_write(paths.design, b"# Design\n\nPending explicit design approval.\n")
        _atomic_write(paths.plan, b"# Implementation Plan\n\nPending explicit design approval.\n")
        run = repository.create_run(run_id, project.project_id, digest)
    except BaseException as primary_error:
        try:
            repository.discard_unstarted_run(run_id)
            if repository.run_exists(run_id):
                raise ValueError(
                    "run changed while start compensation was in progress"
                )
            store.discard_new_run(run_id)
        except BaseException as compensation_error:
            primary_error.add_note(
                f"start compensation preserved artifacts: {compensation_error}"
            )
        raise
    return {
        "ok": True,
        "run_id": run.run_id,
        "state": run.state.value,
        "dry_run": False,
    }


def _start_dry_run(project: Any, services: CliServices) -> dict[str, Any]:
    report: dict[str, Any] = {
        "ok": False,
        "dry_run": True,
        "status": "blocked",
        "project_id": project.project_id,
        "repo_path": str(project.repo_path),
        "remote": project.remote,
        "base_branch": project.base_branch,
        "base_sha": None,
        "test_branch": project.test_branch,
        "test_sha": None,
        "version_endpoint": project.version_endpoint,
        "version_sha_field": project.version_sha_field,
        "version_probe": {
            "status": "blocked",
            "reason": "read-only discovery did not complete",
            "reason_code": "discovery_incomplete",
            "deployed_sha": None,
        },
    }
    try:
        enforce_project_test_endpoint(project)
    except Exception:
        report["version_probe"] = {
            "status": "blocked",
            "reason": "configured endpoint is not an approved test endpoint",
            "reason_code": "endpoint_not_allowed",
            "deployed_sha": None,
        }
        return report
    if project.production_enabled:
        report["version_probe"] = {
            "status": "blocked",
            "reason": "production-enabled project discovery is forbidden",
            "reason_code": "production_access_forbidden",
            "deployed_sha": None,
        }
        return report
    factory = services.discovery_factory
    if factory is None:
        report["version_probe"] = {
            "status": "blocked",
            "reason": "discovery adapter is unavailable",
            "reason_code": "dependency_unavailable",
            "deployed_sha": None,
        }
        return report
    try:
        adapter = factory(project)
        discovery = adapter.discover(dry_run=True)
        base_ref = f"{discovery.remote}/{discovery.base_branch}"
        test_ref = f"{discovery.remote}/{discovery.test_branch}"
        base_sha = discovery.remote_shas[base_ref]
        test_sha = discovery.remote_shas[test_ref]
        report.update(
            {
                "repo_path": str(discovery.repo_path),
                "remote": discovery.remote,
                "base_branch": discovery.base_branch,
                "base_sha": base_sha,
                "test_branch": discovery.test_branch,
                "test_sha": test_sha,
            }
        )
    except Exception:
        report["version_probe"] = {
            "status": "blocked",
            "reason": "read-only dependency failed",
            "reason_code": "dependency_failed",
            "deployed_sha": None,
        }
        return report
    try:
        deployed_sha = adapter.deployed_sha()
    except ValueError:
        report["version_probe"] = {
            "status": "blocked",
            "reason": "version response lacks a valid deployment SHA",
            "reason_code": "version_evidence_invalid",
            "deployed_sha": None,
        }
        return report
    except Exception:
        report["version_probe"] = {
            "status": "blocked",
            "reason": "version endpoint probe failed",
            "reason_code": "version_probe_failed",
            "deployed_sha": None,
        }
        return report
    try:
        final_discovery = adapter.discover(dry_run=True)
    except Exception:
        report["version_probe"] = {
            "status": "blocked",
            "reason": "remote branch heads could not be resampled",
            "reason_code": "remote_resample_failed",
            "deployed_sha": deployed_sha,
        }
        return report
    initial_facts = (
        str(discovery.repo_path),
        discovery.remote,
        discovery.base_branch,
        discovery.test_branch,
        discovery.remote_shas,
    )
    final_facts = (
        str(final_discovery.repo_path),
        final_discovery.remote,
        final_discovery.base_branch,
        final_discovery.test_branch,
        final_discovery.remote_shas,
    )
    if initial_facts != final_facts:
        report["version_probe"] = {
            "status": "blocked",
            "reason": "remote branch heads changed during version probe",
            "reason_code": "remote_changed_during_probe",
            "deployed_sha": deployed_sha,
        }
        return report
    expected_ref = f"{report['remote']}/{report['test_branch']}"
    if not hmac.compare_digest(deployed_sha, test_sha):
        report["version_probe"] = {
            "status": "blocked",
            "reason": f"deployed SHA does not match {expected_ref}",
            "reason_code": "deployed_sha_mismatch",
            "deployed_sha": deployed_sha,
        }
        return report
    report.update(
        {
            "ok": True,
            "status": "passed",
            "version_probe": {
                "status": "passed",
                "reason": f"deployed SHA matches {expected_ref}",
                "reason_code": "deployed_sha_matches",
                "deployed_sha": deployed_sha,
            },
        }
    )
    return report


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


def _approval_identity_matches(
    snapshot: ApprovalSnapshot,
    run: Any,
    project: Any,
) -> bool:
    return (
        snapshot.run_id == run.run_id
        and snapshot.project_id == run.project_id
        and snapshot.base_branch == project.base_branch
        and snapshot.test_branch == project.test_branch
        and hmac.compare_digest(snapshot.config_hash, run.config_hash)
    )


@contextmanager
def _approval_run_lock(
    services: CliServices,
    run_id: str,
) -> Iterator[None]:
    lock_root = _child(services.plugin_root, "locks")
    lock_root.mkdir(parents=True, exist_ok=True)
    _require_safe_directory_chain(services.plugin_root, lock_root)
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    try:
        with FileLock(lock_root / f"approval-{digest}.lock", timeout=30):
            yield
    except Timeout as error:
        raise CliUsageError("approval operation is already in progress") from error


def _prepare_approval(
    args: argparse.Namespace,
    services: CliServices,
) -> dict[str, Any]:
    with _approval_run_lock(services, args.run_id):
        return _prepare_approval_locked(args, services)


def _prepare_approval_locked(
    args: argparse.Namespace,
    services: CliServices,
) -> dict[str, Any]:
    repository = services.get_repository()
    run = repository.get_run(args.run_id)
    if run.state is not RunState.AWAITING_DESIGN_APPROVAL:
        raise CliUsageError("run is not awaiting design approval")
    run_root = _child(services.plugin_root, "runs", run.run_id)
    _require_safe_directory_chain(services.plugin_root, run_root)
    project, digest = services.load_project(run.project_id)
    if not hmac.compare_digest(digest, run.config_hash):
        raise CliUsageError("project configuration hash changed")
    artifacts = tuple(run_root / name for name in REQUIRED_ARTIFACT_NAMES)
    destination = run_root / "approval.json"
    if destination.exists() and destination.stat().st_size > 0:
        contents = _stable_read(destination, limit=MAX_REQUIREMENT_BYTES)
        try:
            snapshot = ApprovalSnapshot.model_validate(json.loads(contents))
        except (UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise CliUsageError(
                "existing approval candidate is invalid; refusing overwrite"
            ) from error
        if (
            not _approval_identity_matches(snapshot, run, project)
            or not verify_approval(snapshot, artifacts, run.config_hash)
        ):
            raise CliUsageError(
                "existing approval candidate is stale; refusing overwrite"
            )
    else:
        assert services.base_sha_provider is not None
        base_sha = services.base_sha_provider(project)
        if (
            not isinstance(base_sha, str)
            or re.fullmatch(r"[0-9a-fA-F]{40}", base_sha) is None
        ):
            raise CliUsageError("registered base SHA is invalid")
        snapshot = build_approval(
            run_id=run.run_id,
            project_id=run.project_id,
            config_hash=run.config_hash,
            base_branch=project.base_branch,
            base_sha=base_sha.lower(),
            test_branch=project.test_branch,
            files=artifacts,
        )
        if not verify_approval(snapshot, artifacts, run.config_hash):
            raise CliUsageError("approval artifacts changed during preparation")
        save_approval(destination, snapshot)
        contents = _stable_read(destination, limit=MAX_REQUIREMENT_BYTES)
        persisted = ApprovalSnapshot.model_validate(json.loads(contents))
        if (
            persisted != snapshot
            or not verify_approval(persisted, artifacts, run.config_hash)
        ):
            raise CliUsageError("persisted approval candidate is not stable")
    file_sha = hashlib.sha256(contents).hexdigest()
    return {
        "ok": True,
        "run_id": run.run_id,
        "state": run.state.value,
        "approval_file": str(destination.resolve(strict=True)),
        "approval_sha256": file_sha,
        "approved": False,
    }


def _approve(args: argparse.Namespace, services: CliServices) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{64}", args.confirm_hash) is None:
        raise CliUsageError("confirmation hash must be 64 lowercase hex characters")
    with _approval_run_lock(services, args.run_id):
        return _approve_locked(args, services)


def _approve_locked(
    args: argparse.Namespace,
    services: CliServices,
) -> dict[str, Any]:
    repository = services.get_repository()
    run = repository.get_run(args.run_id)
    if run.state in {
        RunState.DRAFT,
        RunState.COMPLETED,
        RunState.CANCELLED,
        RunState.FAILED_TERMINAL,
    }:
        raise CliUsageError("run cannot approve or backfill approval artifacts")
    backfill_hint = (
        "legacy approval backfill refused; restore exact approved content "
        "and rerun approve with the same confirmation hash"
    )
    is_backfill = run.state is not RunState.AWAITING_DESIGN_APPROVAL
    run_root = _child(services.plugin_root, "runs", run.run_id)
    _require_safe_directory_chain(services.plugin_root, run_root)
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
    contents = _stable_read(expected, limit=MAX_REQUIREMENT_BYTES)
    file_sha = hashlib.sha256(contents).hexdigest()
    if not hmac.compare_digest(args.confirm_hash, file_sha):
        if is_backfill:
            raise CliUsageError(backfill_hint)
        raise CliUsageError("confirmation hash does not match approval candidate")
    snapshot = ApprovalSnapshot.model_validate(json.loads(contents))
    if not _approval_identity_matches(snapshot, run, project):
        raise CliUsageError("approval identity does not match run")
    artifacts = tuple(run_root / name for name in REQUIRED_ARTIFACT_NAMES)
    try:
        approved_artifacts = capture_approval_artifacts(
            snapshot,
            artifacts,
            run.config_hash,
        )
    except (OSError, ValueError) as error:
        if is_backfill:
            raise CliUsageError(backfill_hint) from error
        raise
    payload = json.loads(contents)
    evidence_id = f"explicit-approval:{run.run_id}:{file_sha}"
    backfilled = False
    if run.state is RunState.AWAITING_DESIGN_APPROVAL:
        repository.approve_with_evidence(
            run.run_id,
            approval_payload=payload,
            approval_sha256=file_sha,
            evidence_id=evidence_id,
            approved_artifacts=approved_artifacts,
        )
    else:
        try:
            backfilled = repository.backfill_approved_artifacts(
                run.run_id,
                approval_payload=payload,
                approval_sha256=file_sha,
                evidence_id=evidence_id,
                approved_artifacts=approved_artifacts,
            )
        except ValueError as error:
            raise CliUsageError(backfill_hint) from error
    return {
        "ok": True,
        "run_id": run.run_id,
        "state": repository.get_run(run.run_id).state.value,
        "approval_sha256": file_sha,
        "idempotent": run.state is not RunState.AWAITING_DESIGN_APPROVAL,
        "backfilled": backfilled,
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
    if args.command == "prepare-approval":
        return _prepare_approval(args, services)
    if args.command == "approve":
        return _approve(args, services)
    if args.command == "resume":
        return _orchestrator_action("resume", args.run_id, services)
    if args.command == "run":
        return _orchestrator_action("run", args.run_id, services)
    if args.command == "cancel":
        return _orchestrator_action("cancel", args.run_id, services)
    if args.command == "pin":
        with maintenance_lock(services.plugin_root, timeout=30):
            if not args.off:
                ArtifactStore(services.plugin_root).require_pinnable_summary(
                    args.run_id
                )
            run = services.get_repository().set_pinned(
                args.run_id, pinned=not args.off
            )
        return {"ok": True, "run_id": run.run_id, "pinned": run.pinned}
    if args.command == "doctor":
        return _doctor(args, services)
    if args.command == "tick":
        if args.plugin_root is not None:
            candidate = validate_runtime_root(
                args.plugin_root, require_exists=True
            )
            if services.scheduler is not None and candidate != services.plugin_root:
                raise CliUsageError(
                    "tick runtime root cannot replace a configured scheduler root"
                )
            services.plugin_root = candidate
        result = services.get_scheduler().tick()
        return {"ok": True, **result.model_dump(mode="json")}
    if args.command == "release":
        report = services.get_release_service().preflight(args.run_id)
        return {"ok": True, **report.model_dump(mode="json")}
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
    if result.get("ok") is False:
        errors.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
        return 2
    output.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return 0
