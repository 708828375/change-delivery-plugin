from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from change_delivery.adapters.git import (
    GitAdapter,
    GitError,
    LeaseConflict,
    RemoteIdentityChanged,
)
from change_delivery.domain import CommandSpec
from change_delivery.process import (
    ProcessResult,
    ProcessRunner,
    ProcessStartError,
)


def _git_environment() -> dict[str, str]:
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_git_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=check,
    )


def configure_identity(repo: Path) -> None:
    git(repo, "config", "user.name", "Delivery Integration Test")
    git(repo, "config", "user.email", "delivery-test@example.invalid")


def commit_file(repo: Path, relative: str, content: str, message: str) -> str:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD").stdout.strip().lower()


@pytest.fixture
def local_remote(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    remote = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "repo"
    remote.mkdir()
    git(remote, "init", "--bare")
    seed.mkdir()
    git(seed, "init", "-b", "develop")
    configure_identity(seed)
    base_sha = commit_file(seed, "README.md", "base\n", "initial")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-u", "origin", "develop")
    git(tmp_path, "clone", "--branch", "develop", str(remote), str(repo))
    configure_identity(repo)
    return remote, seed, repo, base_sha


class RecordingRunner:
    def __init__(self) -> None:
        self.specs: list[CommandSpec] = []
        self.delegate = ProcessRunner(
            lambda names: {},
            fixed_environment_provider=lambda names: {
                "GIT_CONFIG_NOSYSTEM": "true",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "false",
                "GCM_INTERACTIVE": "never",
                "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
            }
        )

    def run(self, spec: CommandSpec) -> ProcessResult:
        self.specs.append(spec)
        return self.delegate.run(spec)


def adapter_for(
    repo: Path,
    *,
    runner: RecordingRunner | None = None,
    **overrides: object,
) -> GitAdapter:
    arguments: dict[str, object] = {
        "remote": "origin",
        "base_branch": "develop",
        "test_branch": "test",
    }
    arguments.update(overrides)
    return GitAdapter(repo, runner=runner, **arguments)


def test_fetch_remote_sha_and_worktree_use_fetched_remote_base(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    _, seed, repo, initial_sha = local_remote
    latest_sha = commit_file(seed, "server.txt", "latest\n", "remote advance")
    git(seed, "push", "origin", "develop")
    (repo / "dirty.txt").write_text("dirty main checkout\n", encoding="utf-8")
    colliding = tmp_path / "colliding-local-branch"
    git(
        repo,
        "worktree",
        "add",
        "-b",
        "origin/develop",
        str(colliding),
        initial_sha,
    )
    commit_file(colliding, "local-only.txt", "wrong base\n", "local collision")
    git(repo, "worktree", "remove", str(colliding))
    worktree = tmp_path / "worktrees" / "feature-one"
    runner = RecordingRunner()
    adapter = adapter_for(repo, runner=runner)

    adapter.fetch("origin")
    assert adapter.remote_sha("origin", "develop") == latest_sha
    result = adapter.create_worktree("origin/develop", "feature/one", worktree)

    assert result.path == worktree.resolve()
    assert result.branch == "feature/one"
    assert result.base_sha == latest_sha
    assert git(worktree, "rev-parse", "HEAD").stdout.strip().lower() == latest_sha
    assert (worktree / "server.txt").read_text(encoding="utf-8") == "latest\n"
    assert not (worktree / "local-only.txt").exists()
    assert git(repo, "rev-parse", "HEAD").stdout.strip().lower() == initial_sha
    assert (repo / "dirty.txt").exists()
    assert any(spec.args == ("fetch", "--prune", "origin") for spec in runner.specs)
    assert any(
        spec.args
        == (
            "rev-parse",
            "--verify",
            "refs/remotes/origin/develop^{commit}",
        )
        for spec in runner.specs
    )
    assert any(
        spec.args
        == (
            "worktree",
            "add",
            "-b",
            "feature/one",
            str(worktree.resolve()),
            "refs/remotes/origin/develop",
        )
        for spec in runner.specs
    )


def test_commit_and_feature_push_are_direct_non_force_argv(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    _, _, repo, _ = local_remote
    runner = RecordingRunner()
    adapter = adapter_for(repo, runner=runner)
    adapter.fetch("origin")
    worktree = tmp_path / "feature"
    adapter.create_worktree("origin/develop", "feature/push", worktree)
    (worktree / "feature.txt").write_text("feature\n", encoding="utf-8")

    commit_sha = adapter.commit_all(worktree, "feat: local integration")
    pushed_sha = adapter.push_feature(worktree, "origin", "feature/push")

    assert pushed_sha == commit_sha
    assert git(repo, "ls-remote", "origin", "refs/heads/feature/push").stdout.split()[0] == commit_sha
    push_specs = [spec for spec in runner.specs if spec.args and spec.args[0] == "push"]
    assert push_specs[-1].args == (
        "push",
        "-u",
        "origin",
        "HEAD:refs/heads/feature/push",
    )
    assert not any("force" in argument for argument in push_specs[-1].args)


def test_commit_all_rejects_empty_commit_and_unsafe_message(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    _, _, repo, _ = local_remote
    adapter = adapter_for(repo)
    adapter.fetch("origin")
    worktree = tmp_path / "feature"
    adapter.create_worktree("origin/develop", "feature/empty", worktree)

    with pytest.raises(GitError, match="no changes"):
        adapter.commit_all(worktree, "feat: nothing")
    for message in ("", " \t", "subject\nbody", "subject\x00body"):
        with pytest.raises(ValueError):
            adapter.commit_all(worktree, message)


def test_test_push_uses_exact_lease_and_returns_verified_sha(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    _, seed, repo, base_sha = local_remote
    git(seed, "push", "origin", f"{base_sha}:refs/heads/test")
    runner = RecordingRunner()
    adapter = adapter_for(repo, runner=runner)
    adapter.fetch("origin")
    worktree = tmp_path / "feature"
    adapter.create_worktree("origin/develop", "feature/test-deploy", worktree)
    source_sha = commit_file(worktree, "deploy.txt", "candidate\n", "candidate")
    observed_sha = adapter.remote_sha("origin", "test")

    result = adapter.push_test_with_lease("origin", "test", source_sha, observed_sha)

    assert result == source_sha
    push_spec = [spec for spec in runner.specs if spec.args and spec.args[0] == "push"][-1]
    assert push_spec.args == (
        "push",
        "origin",
        f"{source_sha}:refs/heads/test",
        f"--force-with-lease=refs/heads/test:{observed_sha}",
    )
    assert "-f" not in push_spec.args
    assert "--force" not in push_spec.args


def test_stale_test_lease_never_overwrites_remote_or_retries(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    remote, seed, repo, base_sha = local_remote
    git(seed, "push", "origin", f"{base_sha}:refs/heads/test")
    runner = RecordingRunner()
    adapter = adapter_for(repo, runner=runner)
    adapter.fetch("origin")
    observed_sha = adapter.remote_sha("origin", "test")
    worktree = tmp_path / "candidate"
    adapter.create_worktree("origin/develop", "feature/stale", worktree)
    source_sha = commit_file(worktree, "candidate.txt", "ours\n", "ours")

    competing = tmp_path / "competing"
    git(tmp_path, "clone", "--branch", "test", str(remote), str(competing))
    configure_identity(competing)
    competing_sha = commit_file(competing, "competing.txt", "theirs\n", "theirs")
    git(competing, "push", "origin", "HEAD:refs/heads/test")

    with pytest.raises(LeaseConflict):
        adapter.push_test_with_lease("origin", "test", source_sha, observed_sha)

    assert git(repo, "ls-remote", "origin", "refs/heads/test").stdout.split()[0] == competing_sha
    push_specs = [spec for spec in runner.specs if spec.args and spec.args[0] == "push"]
    lease_pushes = [spec for spec in push_specs if any("force-with-lease" in arg for arg in spec.args)]
    assert len(lease_pushes) == 1
    assert all("-f" not in spec.args and "--force" not in spec.args for spec in push_specs)


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("fetch", ("-p",)),
        ("remote_sha", ("origin", "-develop")),
        ("create_worktree", ("origin/develop", "-feature", Path("unused"))),
        ("push_feature", (Path("unused"), "origin", "bad branch")),
        ("push_test_with_lease", ("origin", "test\nbranch", "a" * 40, "b" * 40)),
        ("push_test_with_lease", ("origin", "test", "-c" + "a" * 38, "b" * 40)),
    ],
)
def test_option_and_whitespace_injection_is_rejected_before_git(
    local_remote: tuple[Path, Path, Path, str],
    method: str,
    arguments: tuple[object, ...],
) -> None:
    _, _, repo, _ = local_remote
    adapter = adapter_for(repo)

    with pytest.raises((ValueError, GitError)):
        getattr(adapter, method)(*arguments)


def test_remote_must_be_a_registered_name_not_a_url(
    local_remote: tuple[Path, Path, Path, str],
) -> None:
    remote, _, repo, _ = local_remote
    adapter = adapter_for(repo)

    with pytest.raises(ValueError):
        adapter.fetch(str(remote))
    with pytest.raises(ValueError, match="bound remote"):
        adapter.fetch("upstream")


def test_adapter_binds_remote_and_protects_base_test_and_standard_branches(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    _, _, repo, _ = local_remote

    with pytest.raises(ValueError, match="different"):
        adapter_for(repo, base_branch="develop", test_branch="develop")
    adapter = adapter_for(repo)
    adapter.fetch("origin")
    worktree = tmp_path / "protected-push"
    adapter.create_worktree("origin/develop", "feature/protected", worktree)

    with pytest.raises(ValueError, match="bound remote"):
        adapter.fetch("upstream")
    with pytest.raises(ValueError, match="protected"):
        adapter.push_feature(worktree, "origin", "develop")
    with pytest.raises(ValueError, match="bound test"):
        adapter.push_test_with_lease("origin", "main", "a" * 40, "b" * 40)
    with pytest.raises(ValueError, match="protected"):
        adapter.delete_merged_local_branch("develop")


def test_adapter_can_only_be_constructed_from_primary_worktree(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    _, _, repo, _ = local_remote
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-b", "feature/linked-init", str(linked), "develop")

    with pytest.raises(GitError, match="primary"):
        adapter_for(linked)

    primary_adapter = adapter_for(repo)
    (repo / "must-not-commit.txt").write_text("primary dirty\n", encoding="utf-8")
    with pytest.raises(GitError, match="secondary"):
        primary_adapter.commit_all(repo, "must not commit primary")
    assert git(repo, "status", "--short").stdout.strip() == "?? must-not-commit.txt"


def test_remote_identity_change_blocks_fetch_and_push_without_writing(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    _, _, repo, _ = local_remote
    replacement = tmp_path / "replacement.git"
    replacement.mkdir()
    git(replacement, "init", "--bare")
    runner = RecordingRunner()
    adapter = adapter_for(repo, runner=runner)
    adapter.fetch("origin")
    worktree = tmp_path / "identity-worktree"
    adapter.create_worktree("origin/develop", "feature/identity", worktree)
    commit_file(worktree, "identity.txt", "candidate\n", "candidate")
    fetch_count = sum(spec.args[:1] == ("fetch",) for spec in runner.specs)
    push_count = sum(spec.args[:1] == ("push",) for spec in runner.specs)

    git(repo, "remote", "set-url", "origin", str(replacement))

    with pytest.raises(RemoteIdentityChanged):
        adapter.fetch("origin")
    with pytest.raises(RemoteIdentityChanged):
        adapter.push_feature(worktree, "origin", "feature/identity")

    assert sum(spec.args[:1] == ("fetch",) for spec in runner.specs) == fetch_count
    assert sum(spec.args[:1] == ("push",) for spec in runner.specs) == push_count
    assert git(repo, "ls-remote", str(replacement), "refs/heads/feature/identity").stdout == ""


def test_expected_remote_identity_must_match_at_construction(
    local_remote: tuple[Path, Path, Path, str],
) -> None:
    remote, _, repo, _ = local_remote

    adapter_for(
        repo,
        expected_remote_url=str(remote),
        expected_push_url=str(remote),
    )
    with pytest.raises(RemoteIdentityChanged):
        adapter_for(repo, expected_remote_url="C:/unexpected/origin.git")


def test_worktree_base_must_name_a_registered_remote_ref(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    _, _, repo, _ = local_remote
    adapter = adapter_for(repo)
    adapter.fetch("origin")

    with pytest.raises(ValueError, match="remote base"):
        adapter.create_worktree("develop", "feature/local-base", tmp_path / "local")
    with pytest.raises(ValueError, match="bound remote"):
        adapter.create_worktree(
            "upstream/develop",
            "feature/unregistered-base",
            tmp_path / "unregistered",
        )
    with pytest.raises(ValueError, match="remote base"):
        adapter.create_worktree(
            "refs/heads/develop",
            "feature/local-head-base",
            tmp_path / "local-head",
        )
    exact = adapter.create_worktree(
        "refs/remotes/origin/develop",
        "feature/exact-remote-base",
        tmp_path / "exact",
    )
    assert exact.base_sha == adapter.remote_sha("origin", "develop")


def test_remove_worktree_refuses_dirty_and_removes_only_registered_clean_path(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    _, _, repo, _ = local_remote
    adapter = adapter_for(repo)
    adapter.fetch("origin")
    worktree = tmp_path / "feature"
    adapter.create_worktree("origin/develop", "feature/remove", worktree)
    (worktree / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(GitError, match="dirty"):
        adapter.remove_clean_worktree(worktree)
    assert worktree.exists()
    (worktree / "dirty.txt").unlink()
    adapter.remove_clean_worktree(worktree)

    assert not worktree.exists()
    with pytest.raises(GitError, match="registered"):
        adapter.remove_clean_worktree(tmp_path / "not-a-worktree")
    assert tmp_path.exists()


def test_delete_local_branch_uses_safe_d_and_refuses_unmerged_or_protected(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    _, _, repo, _ = local_remote
    runner = RecordingRunner()
    adapter = adapter_for(repo, runner=runner)
    adapter.fetch("origin")
    worktree = tmp_path / "feature"
    adapter.create_worktree("origin/develop", "feature/unmerged", worktree)
    commit_file(worktree, "unmerged.txt", "unmerged\n", "unmerged")
    adapter.remove_clean_worktree(worktree)

    with pytest.raises(GitError):
        adapter.delete_merged_local_branch("feature/unmerged")
    for protected in ("develop", "main", "master", "test"):
        with pytest.raises(ValueError):
            adapter.delete_merged_local_branch(protected)
    branch_specs = [spec for spec in runner.specs if spec.args[:2] == ("branch", "-d")]
    assert branch_specs[-1].args == ("branch", "-d", "feature/unmerged")
    assert all("-D" not in spec.args for spec in branch_specs)


def test_delete_merged_local_branch_succeeds_without_force(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    _, _, repo, _ = local_remote
    adapter = adapter_for(repo)
    adapter.fetch("origin")
    worktree = tmp_path / "merged"
    adapter.create_worktree("origin/develop", "feature/merged", worktree)
    adapter.remove_clean_worktree(worktree)

    adapter.delete_merged_local_branch("feature/merged")

    branches = git(repo, "branch", "--format=%(refname:short)").stdout.splitlines()
    assert "feature/merged" not in branches


def test_git_failure_redacts_credentials_from_exception(
    local_remote: tuple[Path, Path, Path, str],
) -> None:
    _, _, repo, _ = local_remote
    git(repo, "remote", "set-url", "origin", "https://delivery:super-secret@example.invalid/repo.git")
    adapter = adapter_for(repo)

    with pytest.raises(GitError) as caught:
        adapter.fetch("origin")

    error = caught.value
    assert "super-secret" not in str(error)
    assert "super-secret" not in repr(error)
    assert "super-secret" not in repr(error.__dict__)
    assert error.operation == "fetch"
    assert error.exit_code != 0


def test_process_start_failure_is_wrapped_as_structured_git_error(
    local_remote: tuple[Path, Path, Path, str],
) -> None:
    _, _, repo, _ = local_remote

    class StartFailingRunner:
        def run(self, spec: CommandSpec) -> ProcessResult:
            raise ProcessStartError("0123456789abcdef")

    with pytest.raises(GitError) as caught:
        adapter_for(repo, runner=StartFailingRunner())  # type: ignore[arg-type]

    assert caught.value.operation == "list_worktrees"
    assert caught.value.__cause__ is None
