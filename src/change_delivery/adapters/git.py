from __future__ import annotations

from collections.abc import Sequence
import ctypes
from functools import wraps
import hmac
import os
from pathlib import Path
import re
import threading
from typing import Protocol

from pydantic import Field
from filelock import FileLock, Timeout

from change_delivery.domain import CommandSpec, FrozenModel
from change_delivery.errors import BlockedError, DeliveryError
from change_delivery.locking import project_lock_path, project_read_lock
from change_delivery.process import ProcessResult, ProcessRunner
from change_delivery.redaction import redact_text


_REMOTE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_SHA = re.compile(r"^[0-9A-Fa-f]{40}$")
_URL_CREDENTIALS = re.compile(
    r"(?i)\b((?:https?|ssh|git)://)(?:[^/@\s]+)@"
)
_STANDARD_PROTECTED_BRANCHES = frozenset({"develop", "main", "master"})
_GIT_ENV_NAMES = (
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_GLOBAL",
    "GIT_TERMINAL_PROMPT",
    "GCM_INTERACTIVE",
    "GIT_SSH_COMMAND",
)


class Runner(Protocol):
    def run(self, spec: CommandSpec) -> ProcessResult: ...


class WorktreeResult(FrozenModel):
    path: Path
    branch: str
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class RepositoryIdentity(FrozenModel):
    canonical_path: Path
    device: int
    inode: int


class GitDiscoveryAttestation(FrozenModel):
    repository: RepositoryIdentity
    remote: str
    remote_fetch_url: str
    remote_push_url: str
    branch_shas: dict[str, str]


class _StableDirectoryHandle:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: object | None = None
        self.identity: RepositoryIdentity | None = None

    def __enter__(self) -> _StableDirectoryHandle:
        if os.name == "nt":
            self._open_windows()
        else:
            self._open_posix()
        assert self.identity is not None
        if self.identity.device <= 0 or self.identity.inode <= 0:
            self.__exit__()
            raise RemoteIdentityChanged(
                "repository_identity",
                reason="repository has no reliable stable file identity",
            )
        return self

    def __exit__(self, *args: object) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        if os.name == "nt":
            ctypes.windll.kernel32.CloseHandle(handle)
        else:
            os.close(int(handle))

    def _open_posix(self) -> None:
        descriptor = os.open(self._path, os.O_RDONLY)
        details = os.fstat(descriptor)
        self._handle = descriptor
        self.identity = RepositoryIdentity(
            canonical_path=self._path.resolve(strict=True),
            device=details.st_dev,
            inode=details.st_ino,
        )

    def _open_windows(self) -> None:
        if str(self._path).startswith("\\\\"):
            raise RemoteIdentityChanged(
                "repository_identity",
                reason="UNC repository identity is not supported",
            )

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

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateFileW.restype = ctypes.c_void_p
        handle = kernel32.CreateFileW(
            str(self._path),
            0x0001,
            0x00000001 | 0x00000002,
            None,
            3,
            0x02000000 | 0x00200000,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise RemoteIdentityChanged(
                "repository_identity",
                reason="stable repository handle is unavailable",
            )
        information = FileInformation()
        if not kernel32.GetFileInformationByHandle(
            handle,
            ctypes.byref(information),
        ):
            kernel32.CloseHandle(handle)
            raise RemoteIdentityChanged(
                "repository_identity",
                reason="stable repository identity is unavailable",
            )
        if information.attributes & 0x400:
            kernel32.CloseHandle(handle)
            raise RemoteIdentityChanged(
                "repository_identity",
                reason="repository handle targets a reparse point",
            )
        self._handle = handle
        self.identity = RepositoryIdentity(
            canonical_path=self._path.resolve(strict=True),
            device=information.volume_serial,
            inode=(
                (information.file_index_high << 32)
                | information.file_index_low
            ),
        )


def _safe_output(value: str) -> str:
    conventional = redact_text(value, {})
    return _URL_CREDENTIALS.sub(r"\1***@", conventional)


class GitError(DeliveryError):
    """A Git operation failed without exposing command arguments or credentials."""

    def __init__(
        self,
        operation: str,
        *,
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
        reason: str = "git command failed",
    ) -> None:
        self.operation = operation
        self.exit_code = exit_code
        self.stdout = _safe_output(stdout)
        self.stderr = _safe_output(stderr)
        self.reason = _safe_output(reason)
        suffix = "" if exit_code is None else f" (exit {exit_code})"
        super().__init__(f"git {operation} failed{suffix}: {self.reason}")


class LeaseConflict(GitError):
    """The protected test branch changed after it was observed."""


class RemoteIdentityChanged(GitError):
    """The configured remote no longer has its registered fetch/push identity."""


def _project_write(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        self.discovery_lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._project_file_lock, self._attestation_lock:
                return method(self, *args, **kwargs)
        except Timeout as error:
            raise BlockedError("project delivery lock is held") from error

    return locked


def _git_environment(names: tuple[str, ...]) -> dict[str, str]:
    values = {
        "GIT_CONFIG_NOSYSTEM": "true",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "false",
        "GCM_INTERACTIVE": "never",
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
    }
    return {name: values[name] for name in names if name in values}


class GitAdapter:
    def __init__(
        self,
        repo_path: Path,
        *,
        remote: str,
        base_branch: str,
        test_branch: str,
        expected_fetch_url: str,
        expected_push_url: str,
        runner: Runner | None = None,
        git_executable: str = "git",
        lock_root: Path | None = None,
    ) -> None:
        unresolved_repo = Path(repo_path).absolute()
        self._reject_reparse_components(unresolved_repo)
        resolved_repo = unresolved_repo.resolve()
        if not resolved_repo.is_dir():
            raise NotADirectoryError("Git repository path must be an existing directory")
        if (
            not git_executable
            or git_executable.startswith("-")
            or any(character in git_executable for character in "\r\n\x00")
        ):
            raise ValueError("invalid Git executable")
        self.repo_path = resolved_repo
        self._lock_root = lock_root
        self.discovery_lock_path = project_lock_path(
            self.repo_path,
            lock_root=lock_root,
        )
        self._project_file_lock = FileLock(
            self.discovery_lock_path,
            timeout=0,
        )
        self._attestation_lock = threading.RLock()
        with self._open_repository_handle() as repository_handle:
            assert repository_handle.identity is not None
            self._repository_identity = repository_handle.identity
        self._runner = runner or ProcessRunner(
            lambda names: {},
            fixed_environment_provider=_git_environment,
        )
        self._git_executable = git_executable
        self._require_primary_repo()
        self._validate_remote_name(remote)
        self._require_registered_remote(remote)
        self._validate_branch(base_branch)
        self._validate_branch(test_branch)
        if base_branch.casefold() == test_branch.casefold():
            raise ValueError("base and test branches must be different")
        self.remote = remote
        self.base_branch = base_branch
        self.test_branch = test_branch
        self._protected_branches = (
            _STANDARD_PROTECTED_BRANCHES
            | {base_branch.casefold(), test_branch.casefold()}
        )
        self._validate_expected_identity(expected_fetch_url)
        self._validate_expected_identity(expected_push_url)
        fetch_url, push_url = self._read_remote_identity()
        if not hmac.compare_digest(fetch_url, expected_fetch_url):
            raise RemoteIdentityChanged(
                "initialize",
                reason="registered fetch URL does not match expected identity",
            )
        if not hmac.compare_digest(push_url, expected_push_url):
            raise RemoteIdentityChanged(
                "initialize",
                reason="registered push URL does not match expected identity",
            )
        self._fetch_url = expected_fetch_url
        self._push_url = expected_push_url
        with self._open_repository_handle() as repository_handle:
            if repository_handle.identity != self._repository_identity:
                raise RemoteIdentityChanged(
                    "initialize",
                    reason="repository identity changed during initialization",
                )

    @_project_write
    def fetch(self, remote: str) -> None:
        self._require_bound_remote(remote)
        self._assert_remote_identity()
        self._run(
            "fetch",
            self._fetch_scoped(
                (
                    "fetch",
                    "--prune",
                    self._fetch_url,
                    (
                        "+refs/heads/*:"
                        f"refs/remotes/{self.remote}/*"
                    ),
                )
            ),
        )

    def remote_sha(self, remote: str, branch: str) -> str:
        self._require_bound_remote(remote)
        self._assert_remote_identity()
        self._validate_branch(branch)
        result = self._run(
            "remote_sha",
            (
                "rev-parse",
                "--verify",
                f"refs/remotes/{remote}/{branch}^{{commit}}",
            ),
        )
        return self._parse_single_sha(result.stdout, "remote_sha")

    def discovery_attestation(
        self,
        remote: str,
        branches: tuple[str, ...],
    ) -> GitDiscoveryAttestation:
        self._require_bound_remote(remote)
        if (
            not isinstance(branches, tuple)
            or not branches
            or len(set(branches)) != len(branches)
        ):
            raise ValueError("discovery branches must be a non-empty unique tuple")
        for branch in branches:
            self._validate_branch(branch)
        with project_read_lock(
            self.repo_path,
            lock_root=self._lock_root,
        ), self._attestation_lock, self._open_repository_handle() as handle:
            if (
                handle.identity is None
                or handle.identity.device <= 0
                or handle.identity.inode <= 0
            ):
                raise RemoteIdentityChanged(
                    "discovery_attestation",
                    reason="repository has no reliable stable file identity",
                )
            if handle.identity != self._repository_identity:
                raise RemoteIdentityChanged(
                    "discovery_attestation",
                    reason="repository identity changed before discovery",
                )
            initial_remote = self._read_remote_snapshot()
            branch_shas = self._read_branch_snapshot(remote, branches)
            final_remote = self._read_remote_snapshot()
            with self._open_repository_handle() as current:
                if (
                    current.identity != handle.identity
                    or current.identity != self._repository_identity
                ):
                    raise RemoteIdentityChanged(
                        "discovery_attestation",
                        reason="repository identity changed during discovery",
                    )
            expected_remote = (self._fetch_url, self._push_url)
            if initial_remote != expected_remote or final_remote != expected_remote:
                raise RemoteIdentityChanged(
                    "discovery_attestation",
                    reason="remote identity changed during discovery",
                )
            return GitDiscoveryAttestation(
                repository=self._repository_identity,
                remote=remote,
                remote_fetch_url=self._fetch_url,
                remote_push_url=self._push_url,
                branch_shas=branch_shas,
            )

    @_project_write
    def create_worktree(
        self,
        base_ref: str,
        branch: str,
        worktree_path: Path,
        *,
        expected_base_sha: str | None = None,
    ) -> WorktreeResult:
        canonical_base_ref = self._canonical_remote_base_ref(base_ref)
        self._validate_branch(branch)
        target = Path(worktree_path)
        if not target.is_absolute():
            raise ValueError("worktree path must be absolute")
        target = target.resolve()
        if target.exists():
            raise FileExistsError("worktree path must not already exist")
        target.parent.mkdir(parents=True, exist_ok=True)
        base_result = self._run(
            "resolve_base",
            (
                "rev-parse",
                "--verify",
                f"{canonical_base_ref}^{{commit}}",
            ),
        )
        base_sha = self._parse_single_sha(base_result.stdout, "resolve_base")
        frozen_base = (
            base_sha
            if expected_base_sha is None
            else self._normalize_sha(expected_base_sha)
        )
        if base_sha != frozen_base:
            raise GitError(
                "create_worktree",
                reason="remote base does not match expected SHA",
            )
        self._run(
            "create_worktree",
            (
                "worktree",
                "add",
                "-b",
                branch,
                str(target),
                frozen_base if expected_base_sha is not None else canonical_base_ref,
            ),
        )
        return WorktreeResult(path=target, branch=branch, base_sha=frozen_base)

    def status_short(self, workdir: Path) -> str:
        resolved = self._existing_directory(workdir)
        return self._run(
            "status",
            ("status", "--short", "--untracked-files=all"),
            workdir=resolved,
        ).stdout

    @_project_write
    def commit_all(self, workdir: Path, message: str) -> str:
        resolved = self._require_secondary_worktree(workdir)
        self._validate_commit_message(message)
        self._run("add", ("add", "-A"), workdir=resolved)
        if not self.status_short(resolved).strip():
            raise GitError("commit", reason="no changes to commit")
        self._run("commit", ("commit", "-m", message), workdir=resolved)
        return self._head_sha(resolved)

    def worktree_head(self, workdir: Path) -> str:
        return self._head_sha(self._require_secondary_worktree(workdir))

    def worktree_branch(self, workdir: Path) -> str:
        resolved = self._require_secondary_worktree(workdir)
        branch = self._single_line(
            self._run(
                "worktree_branch",
                ("symbolic-ref", "--quiet", "--short", "HEAD"),
                workdir=resolved,
            ).stdout,
            "worktree_branch",
        )
        self._validate_branch(branch)
        return branch

    @_project_write
    def staged_tree(self, workdir: Path, *, stage_all: bool) -> str:
        resolved = self._require_secondary_worktree(workdir)
        if not isinstance(stage_all, bool):
            raise TypeError("stage_all must be a boolean")
        if stage_all:
            self._run("add", ("add", "-A"), workdir=resolved)
        result = self._run("write_tree", ("write-tree",), workdir=resolved)
        return self._parse_single_sha(result.stdout, "write_tree")

    @_project_write
    def commit_tree_cas(
        self,
        workdir: Path,
        message: str,
        *,
        expected_parent: str,
        expected_tree: str,
    ) -> str:
        resolved = self._require_secondary_worktree(workdir)
        self._validate_commit_message(message)
        parent = self._normalize_sha(expected_parent)
        tree = self._normalize_sha(expected_tree)
        if self._head_sha(resolved) != parent:
            raise GitError("commit_tree_cas", reason="worktree head changed")
        if self.staged_tree(resolved, stage_all=False) != tree:
            raise GitError("commit_tree_cas", reason="staged tree changed")
        branch = self.worktree_branch(resolved)
        commit = self._parse_single_sha(
            self._run(
                "commit_tree",
                (
                    "commit-tree",
                    tree,
                    "-p",
                    parent,
                    "-m",
                    message,
                ),
                workdir=resolved,
            ).stdout,
            "commit_tree",
        )
        self._run(
            "update_ref",
            (
                "update-ref",
                f"refs/heads/{branch}",
                commit,
                parent,
            ),
            workdir=resolved,
        )
        fact = self.head_commit_fact(resolved)
        if fact != {
            "new_head_sha": commit,
            "parent_sha": parent,
            "commit_tree_sha": tree,
            "message": message,
        }:
            raise GitError(
                "commit_tree_cas",
                reason="created commit fact verification failed",
            )
        return commit

    def head_commit_fact(self, workdir: Path) -> dict[str, str]:
        resolved = self._require_secondary_worktree(workdir)
        head = self._head_sha(resolved)
        ancestry = self._run(
            "head_commit_parents",
            ("rev-list", "--parents", "--max-count=1", head),
            workdir=resolved,
        ).stdout.split()
        if len(ancestry) != 2 or self._normalize_sha(ancestry[0]) != head:
            raise GitError(
                "head_commit_fact",
                reason="head commit must have exactly one parent",
            )
        parent = self._normalize_sha(ancestry[1])
        tree = self._parse_single_sha(
            self._run(
                "head_commit_tree",
                ("rev-parse", "--verify", f"{head}^{{tree}}"),
                workdir=resolved,
            ).stdout,
            "head_commit_tree",
        )
        message = self._run(
            "head_commit_message",
            ("show", "-s", "--format=%B", head),
            workdir=resolved,
        ).stdout.rstrip("\r\n")
        return {
            "new_head_sha": head,
            "parent_sha": parent,
            "commit_tree_sha": tree,
            "message": message,
        }

    @_project_write
    def push_feature(
        self,
        workdir: Path,
        remote: str,
        branch: str,
        expected_sha: str,
    ) -> str:
        self._require_bound_remote(remote)
        self._validate_branch(branch)
        if branch.casefold() in self._protected_branches:
            raise ValueError("protected branch cannot receive a feature push")
        resolved = self._require_secondary_worktree(workdir)
        expected = self._normalize_sha(expected_sha)
        if self._head_sha(resolved) != expected:
            raise GitError(
                "push_feature",
                reason="worktree head changed before push",
            )
        self._assert_remote_identity()
        self._run(
            "push_feature",
            self._push_scoped(
                (
                    "push",
                    "-u",
                    self._push_url,
                    f"{expected}:refs/heads/{branch}",
                )
            ),
            workdir=resolved,
        )
        pushed_sha = self._remote_head_sha(remote, branch)
        if pushed_sha != expected:
            raise GitError("push_feature", reason="remote SHA verification failed")
        return expected

    @_project_write
    def push_test_with_lease(
        self,
        remote: str,
        test_branch: str,
        source_sha: str,
        observed_sha: str,
    ) -> str:
        self._require_bound_remote(remote)
        self._validate_branch(test_branch)
        if test_branch != self.test_branch:
            raise ValueError("test branch must equal the bound test branch")
        source = self._normalize_sha(source_sha)
        observed = self._normalize_sha(observed_sha)
        self._assert_remote_identity()
        args = self._push_scoped(
            (
                "push",
                self._push_url,
                f"{source}:refs/heads/{test_branch}",
                f"--force-with-lease=refs/heads/{test_branch}:{observed}",
            )
        )
        result = self._execute(
            args,
            workdir=self.repo_path,
            operation="push_test_with_lease",
        )
        if result.exit_code != 0:
            combined = f"{result.stdout}\n{result.stderr}".casefold()
            if (
                "stale info" in combined
                or "fetch first" in combined
                or "[rejected]" in combined
            ):
                raise LeaseConflict(
                    "push_test_with_lease",
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    reason="lease rejected",
                )
            self._raise_result("push_test_with_lease", result)
        verified = self._remote_head_sha(remote, test_branch)
        if verified != source:
            raise LeaseConflict(
                "push_test_with_lease",
                reason="remote SHA changed during verification",
            )
        return source

    @_project_write
    def remove_clean_worktree(self, path: Path) -> None:
        target = Path(path)
        if not target.is_absolute():
            raise ValueError("worktree path must be absolute")
        target = target.resolve()
        registered = self._registered_worktrees()
        key = self._path_key(target)
        if key not in registered or target == self.repo_path:
            raise GitError("remove_worktree", reason="path is not a registered secondary worktree")
        if self.status_short(target).strip():
            raise GitError("remove_worktree", reason="worktree is dirty")
        self._run(
            "remove_worktree",
            ("worktree", "remove", str(target)),
        )

    @_project_write
    def delete_merged_local_branch(self, branch: str) -> None:
        self._validate_branch(branch)
        if branch.casefold() in self._protected_branches:
            raise ValueError("protected branch cannot be deleted")
        current_result = self._run(
            "current_branch",
            ("symbolic-ref", "--quiet", "--short", "HEAD"),
        )
        if current_result.stdout.strip() == branch:
            raise ValueError("current branch cannot be deleted")
        self._run("delete_branch", ("branch", "-d", branch))

    def _run(
        self,
        operation: str,
        args: Sequence[str],
        *,
        workdir: Path | None = None,
    ) -> ProcessResult:
        result = self._execute(
            args,
            workdir=workdir or self.repo_path,
            operation=operation,
        )
        if result.exit_code != 0:
            self._raise_result(operation, result)
        return result

    def _execute(
        self,
        args: Sequence[str],
        *,
        workdir: Path,
        operation: str,
    ) -> ProcessResult:
        try:
            return self._runner.run(
                CommandSpec(
                    executable=self._git_executable,
                    args=tuple(args),
                    workdir=workdir,
                    timeout_seconds=60,
                    env_names=_GIT_ENV_NAMES,
                )
            )
        except GitError:
            raise
        except DeliveryError:
            raise GitError(
                operation,
                reason="Git process could not complete",
            ) from None

    @staticmethod
    def _raise_result(operation: str, result: ProcessResult) -> None:
        raise GitError(
            operation,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def _require_registered_remote(self, remote: str) -> None:
        self._validate_remote_name(remote)
        result = self._run("list_remotes", ("remote",))
        if remote not in result.stdout.splitlines():
            raise GitError("remote", reason="remote is not registered")

    @staticmethod
    def _validate_remote_name(remote: str) -> None:
        if (
            not isinstance(remote, str)
            or not _REMOTE_NAME.fullmatch(remote)
            or remote.startswith("-")
        ):
            raise ValueError("remote must be a safe registered name")

    def _require_bound_remote(self, remote: str) -> None:
        self._validate_remote_name(remote)
        if remote != self.remote:
            raise ValueError("remote must equal the bound remote")

    def _read_remote_identity(self) -> tuple[str, str]:
        fetch_url = self._single_line(
            self._run(
                "remote_fetch_url",
                ("remote", "get-url", self.remote),
            ).stdout,
            "remote_fetch_url",
        )
        push_url = self._single_line(
            self._run(
                "remote_push_url",
                ("remote", "get-url", "--push", self.remote),
            ).stdout,
            "remote_push_url",
        )
        return fetch_url, push_url

    def _assert_remote_identity(self) -> None:
        # Task 12's single-writer lock must enclose this recheck and its operation.
        try:
            fetch_url, push_url = self._read_remote_identity()
        except GitError:
            raise RemoteIdentityChanged(
                "remote_identity",
                reason="registered remote identity is unavailable",
            ) from None
        if not (
            hmac.compare_digest(fetch_url, self._fetch_url)
            and hmac.compare_digest(push_url, self._push_url)
        ):
            raise RemoteIdentityChanged(
                "remote_identity",
                reason="registered remote identity changed",
            )

    @staticmethod
    def _validate_expected_identity(identity: str) -> None:
        if (
            not isinstance(identity, str)
            or not identity
            or identity.startswith("-")
            or any(character in identity for character in "\r\n\x00")
        ):
            raise ValueError("expected remote identity is invalid")

    def _fetch_scoped(self, operation: Sequence[str]) -> tuple[str, ...]:
        return (
            "-c",
            f"remote.{self.remote}.url={self._fetch_url}",
            "-c",
            (
                f"remote.{self.remote}.fetch="
                f"+refs/heads/*:refs/remotes/{self.remote}/*"
            ),
            *operation,
        )

    def _push_scoped(self, operation: Sequence[str]) -> tuple[str, ...]:
        return (
            "-c",
            f"remote.{self.remote}.url={self._fetch_url}",
            "-c",
            f"remote.{self.remote}.pushurl={self._push_url}",
            *operation,
        )

    def _validate_branch(self, branch: str) -> None:
        if (
            not isinstance(branch, str)
            or not branch
            or branch.startswith("-")
            or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in branch)
        ):
            raise ValueError("invalid branch or ref name")
        result = self._execute(
            ("check-ref-format", "--branch", branch),
            workdir=self.repo_path,
            operation="validate_ref",
        )
        if result.exit_code != 0:
            raise ValueError("invalid branch or ref name")

    def _canonical_remote_base_ref(self, base_ref: str) -> str:
        if not isinstance(base_ref, str):
            raise ValueError("worktree base must be a remote base ref")
        prefix = "refs/remotes/"
        if base_ref.startswith(prefix):
            remote_branch = base_ref[len(prefix) :]
        elif base_ref.startswith("refs/"):
            raise ValueError("worktree base must be a remote base ref")
        else:
            remote_branch = base_ref
        if "/" not in remote_branch:
            raise ValueError("worktree base must be a remote base ref")
        remote, branch = remote_branch.split("/", 1)
        if not remote or not branch:
            raise ValueError("worktree base must be a remote base ref")
        self._require_bound_remote(remote)
        self._validate_branch(branch)
        if branch != self.base_branch:
            raise ValueError("worktree base must equal the bound base branch")
        return f"refs/remotes/{remote}/{branch}"

    def _head_sha(self, workdir: Path) -> str:
        result = self._run(
            "head_sha",
            ("rev-parse", "--verify", "HEAD^{commit}"),
            workdir=workdir,
        )
        return self._parse_single_sha(result.stdout, "head_sha")

    @staticmethod
    def _validate_commit_message(message: str) -> None:
        if (
            not isinstance(message, str)
            or not message.strip()
            or any(character in message for character in "\r\n\x00")
        ):
            raise ValueError("commit message must be non-empty and single-line")

    def _remote_head_sha(self, remote: str, branch: str) -> str:
        self._require_bound_remote(remote)
        result = self._run(
            "verify_remote",
            self._push_scoped(
                (
                    "ls-remote",
                    "--heads",
                    self._push_url,
                    f"refs/heads/{branch}",
                )
            ),
        )
        fields = result.stdout.split()
        if len(fields) != 2 or fields[1] != f"refs/heads/{branch}":
            raise GitError("verify_remote", reason="remote branch was not found")
        return self._normalize_sha(fields[0])

    @classmethod
    def _parse_single_sha(cls, stdout: str, operation: str) -> str:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            raise GitError(operation, reason="Git did not return exactly one SHA")
        return cls._normalize_sha(lines[0])

    @staticmethod
    def _normalize_sha(value: str) -> str:
        if not isinstance(value, str) or not _SHA.fullmatch(value):
            raise ValueError("SHA must contain exactly 40 hexadecimal characters")
        return value.lower()

    @staticmethod
    def _existing_directory(path: Path) -> Path:
        resolved = Path(path).resolve()
        if not resolved.is_dir():
            raise NotADirectoryError("Git workdir must be an existing directory")
        return resolved

    def _capture_repository_identity(self) -> RepositoryIdentity:
        self._reject_reparse_components(self.repo_path)
        try:
            details = self.repo_path.stat()
        except OSError as error:
            raise RemoteIdentityChanged(
                "repository_identity",
                reason="repository identity is unavailable",
            ) from error
        if not self.repo_path.is_dir():
            raise RemoteIdentityChanged(
                "repository_identity",
                reason="repository path is not a directory",
            )
        return RepositoryIdentity(
            canonical_path=self.repo_path.resolve(strict=True),
            device=details.st_dev,
            inode=details.st_ino,
        )

    def _open_repository_handle(self) -> _StableDirectoryHandle:
        self._reject_reparse_components(self.repo_path)
        return _StableDirectoryHandle(self.repo_path)

    def _read_remote_snapshot(self) -> tuple[str, str]:
        escaped_remote = re.escape(self.remote)
        result = self._run(
            "remote_snapshot",
            (
                "config",
                "--get-regexp",
                f"^remote\\.{escaped_remote}\\.(url|pushurl)$",
            ),
        )
        fetch_urls: list[str] = []
        push_urls: list[str] = []
        for line in result.stdout.splitlines():
            fields = line.split(maxsplit=1)
            if len(fields) != 2:
                raise GitError(
                    "remote_snapshot",
                    reason="remote config snapshot is malformed",
                )
            key, value = fields
            if key == f"remote.{self.remote}.url":
                fetch_urls.append(value)
            elif key == f"remote.{self.remote}.pushurl":
                push_urls.append(value)
            else:
                raise GitError(
                    "remote_snapshot",
                    reason="remote config snapshot contains an unexpected key",
                )
        if len(fetch_urls) != 1 or len(push_urls) > 1:
            raise GitError(
                "remote_snapshot",
                reason="remote config must have one frozen fetch/push identity",
            )
        return fetch_urls[0], push_urls[0] if push_urls else fetch_urls[0]

    def _read_branch_snapshot(
        self,
        remote: str,
        branches: tuple[str, ...],
    ) -> dict[str, str]:
        refs = tuple(f"refs/remotes/{remote}/{branch}" for branch in branches)
        result = self._run(
            "discovery_refs_snapshot",
            (
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                *refs,
            ),
        )
        expected = dict(zip(refs, branches, strict=True))
        observed: dict[str, str] = {}
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 2 or fields[0] not in expected:
                raise GitError(
                    "discovery_refs_snapshot",
                    reason="remote refs snapshot is malformed",
                )
            branch = expected[fields[0]]
            if branch in observed:
                raise GitError(
                    "discovery_refs_snapshot",
                    reason="remote refs snapshot contains duplicates",
                )
            observed[branch] = self._normalize_sha(fields[1])
        if set(observed) != set(branches):
            raise GitError(
                "discovery_refs_snapshot",
                reason="remote refs snapshot is incomplete",
            )
        return observed

    @staticmethod
    def _reject_reparse_components(path: Path) -> None:
        current = Path(path.anchor)
        for component in path.parts[1:]:
            current /= component
            try:
                details = current.lstat()
            except OSError:
                continue
            attributes = getattr(details, "st_file_attributes", 0)
            if current.is_symlink() or attributes & 0x400:
                raise RemoteIdentityChanged(
                    "repository_identity",
                    reason="repository path contains a reparse point",
                )

    def _registered_worktrees(self) -> dict[str, Path]:
        return {
            self._path_key(path): path
            for path in self._worktrees_in_order()
        }

    def _worktrees_in_order(self) -> list[Path]:
        result = self._run(
            "list_worktrees",
            ("worktree", "list", "--porcelain"),
        )
        paths: list[Path] = []
        for line in result.stdout.splitlines():
            if not line.startswith("worktree "):
                continue
            path = Path(line[len("worktree ") :]).resolve()
            paths.append(path)
        return paths

    def _require_primary_repo(self) -> None:
        worktrees = self._worktrees_in_order()
        if (
            not worktrees
            or self._path_key(worktrees[0]) != self._path_key(self.repo_path)
        ):
            raise GitError(
                "initialize",
                reason="adapter repository must be the primary worktree",
            )

    @staticmethod
    def _single_line(value: str, operation: str) -> str:
        lines = value.splitlines()
        if len(lines) != 1 or not lines[0]:
            raise GitError(operation, reason="Git did not return one identity value")
        return lines[0]

    def _require_secondary_worktree(self, path: Path) -> Path:
        resolved = self._existing_directory(path)
        if (
            resolved == self.repo_path
            or self._path_key(resolved) not in self._registered_worktrees()
        ):
            raise GitError("worktree", reason="path is not a registered secondary worktree")
        return resolved

    @staticmethod
    def _path_key(path: Path) -> str:
        return os.path.normcase(str(path.resolve()))
