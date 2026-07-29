from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from change_delivery.adapters.git import (
    GitAdapter,
    GitDiscoveryAttestation,
    GitError,
    LeaseConflict,
    RemoteIdentityChanged,
)
from change_delivery.cli import RuntimeGitPort
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


def git_operation_args(args: tuple[str, ...]) -> tuple[str, ...]:
    index = 0
    while index + 1 < len(args) and args[index] == "-c":
        index += 2
    return args[index:]


class HookedRunner(RecordingRunner):
    def __init__(self) -> None:
        super().__init__()
        self._hook_command: str | None = None
        self._hook: Callable[[], None] | None = None

    def arm(self, command: str, hook: Callable[[], None]) -> None:
        self._hook_command = command
        self._hook = hook

    def run(self, spec: CommandSpec) -> ProcessResult:
        operation = git_operation_args(spec.args)
        if operation and operation[0] == self._hook_command:
            hook = self._hook
            self._hook_command = None
            self._hook = None
            assert hook is not None
            hook()
        return super().run(spec)


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
        "expected_fetch_url": git(repo, "remote", "get-url", "origin").stdout.strip(),
        "expected_push_url": git(
            repo,
            "remote",
            "get-url",
            "--push",
            "origin",
        ).stdout.strip(),
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
    assert any(
        git_operation_args(spec.args)
        == (
            "fetch",
            "--prune",
            str(local_remote[0]),
            "+refs/heads/*:refs/remotes/origin/*",
        )
        for spec in runner.specs
    )
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


def test_discovery_attestation_binds_repo_remote_and_branch_shas(
    local_remote: tuple[Path, Path, Path, str],
) -> None:
    remote, seed, repo, develop_sha = local_remote
    git(seed, "branch", "test")
    git(seed, "push", "origin", "test")
    git(repo, "fetch", "origin")
    adapter = adapter_for(repo)

    attestation = adapter.discovery_attestation(
        "origin",
        ("develop", "test"),
    )

    assert isinstance(attestation, GitDiscoveryAttestation)
    assert attestation.repository.canonical_path == repo.resolve()
    assert attestation.repository.inode > 0
    assert attestation.remote_fetch_url == str(remote)
    assert attestation.remote_push_url == str(remote)
    assert attestation.branch_shas == {
        "develop": develop_sha,
        "test": develop_sha,
    }


def test_discovery_attestation_rejects_repository_identity_replacement(
    local_remote: tuple[Path, Path, Path, str],
    monkeypatch,
) -> None:
    _, _, repo, _ = local_remote
    adapter = adapter_for(repo)
    original = adapter._repository_identity
    changed = original.model_copy(update={"inode": original.inode + 1})
    identities = iter((original, changed))
    monkeypatch.setattr(
        adapter,
        "_capture_repository_identity",
        lambda: next(identities),
    )

    with pytest.raises(RemoteIdentityChanged, match="repository identity"):
        adapter.discovery_attestation("origin", ("develop",))


def test_worktree_creation_uses_frozen_expected_base_sha(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    _, _, repo, initial_sha = local_remote
    runner = RecordingRunner()
    adapter = adapter_for(repo, runner=runner)
    worktree = tmp_path / "worktrees" / "frozen-base"

    adapter.fetch("origin")
    adapter.create_worktree(
        "origin/develop",
        "feature/frozen-base",
        worktree,
        expected_base_sha=initial_sha,
    )

    assert any(
        git_operation_args(spec.args)
        == (
            "worktree",
            "add",
            "-b",
            "feature/frozen-base",
            str(worktree.resolve()),
            initial_sha,
        )
        for spec in runner.specs
    )


def test_commit_and_feature_push_are_direct_non_force_argv(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    remote, _, repo, _ = local_remote
    runner = RecordingRunner()
    adapter = adapter_for(repo, runner=runner)
    adapter.fetch("origin")
    worktree = tmp_path / "feature"
    adapter.create_worktree("origin/develop", "feature/push", worktree)
    (worktree / "feature.txt").write_text("feature\n", encoding="utf-8")

    commit_sha = adapter.commit_all(worktree, "feat: local integration")
    pushed_sha = adapter.push_feature(
        worktree,
        "origin",
        "feature/push",
        commit_sha,
    )

    assert pushed_sha == commit_sha
    assert git(repo, "ls-remote", "origin", "refs/heads/feature/push").stdout.split()[0] == commit_sha
    push_specs = [
        spec
        for spec in runner.specs
        if git_operation_args(spec.args)[:1] == ("push",)
    ]
    assert git_operation_args(push_specs[-1].args) == (
        "push",
        "-u",
        str(remote),
        f"{commit_sha}:refs/heads/feature/push",
    )
    assert not any(
        "force" in argument
        for argument in git_operation_args(push_specs[-1].args)
    )


def test_runtime_feature_push_does_not_write_when_head_differs_from_expected(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    remote, _, repo, base_sha = local_remote
    plugin_root = tmp_path / "plugin"
    worktree = plugin_root / "worktrees" / "stale-runtime"
    adapter = adapter_for(repo)
    adapter.fetch("origin")
    adapter.create_worktree(
        "origin/develop",
        "feature/stale-runtime",
        worktree,
    )
    commit_file(worktree, "feature.txt", "changed\n", "changed")
    config = SimpleNamespace(
        repo_path=repo,
        remote="origin",
        base_branch="develop",
        test_branch="test",
        remote_fetch_url=str(remote),
        remote_push_url=str(remote),
    )
    runtime = RuntimeGitPort(config, plugin_root)

    with pytest.raises((GitError, ValueError), match="head changed"):
        runtime.push_feature("feature/stale-runtime", base_sha)

    assert (
        git(
            repo,
            "ls-remote",
            str(remote),
            "refs/heads/feature/stale-runtime",
        ).stdout
        == ""
    )


def test_feature_push_rejects_stale_expected_sha_before_any_push(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    remote, _, repo, base_sha = local_remote
    runner = RecordingRunner()
    adapter = adapter_for(repo, runner=runner)
    adapter.fetch("origin")
    worktree = tmp_path / "stale-feature"
    adapter.create_worktree(
        "origin/develop",
        "feature/stale-head",
        worktree,
    )
    commit_file(worktree, "feature.txt", "changed\n", "changed")

    with pytest.raises(GitError, match="head changed"):
        adapter.push_feature(
            worktree,
            "origin",
            "feature/stale-head",
            base_sha,
        )

    assert not any(
        git_operation_args(spec.args)[:1] == ("push",)
        for spec in runner.specs
    )
    assert (
        git(
            repo,
            "ls-remote",
            str(remote),
            "refs/heads/feature/stale-head",
        ).stdout
        == ""
    )


def test_feature_push_uses_frozen_expected_sha_when_head_changes_during_push(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    remote, _, repo, _ = local_remote
    runner = HookedRunner()
    adapter = adapter_for(repo, runner=runner)
    adapter.fetch("origin")
    worktree = tmp_path / "frozen-feature"
    adapter.create_worktree(
        "origin/develop",
        "feature/frozen-head",
        worktree,
    )
    expected_sha = commit_file(worktree, "feature.txt", "expected\n", "expected")
    runner.arm(
        "push",
        lambda: commit_file(worktree, "later.txt", "later\n", "later"),
    )

    pushed_sha = adapter.push_feature(
        worktree,
        "origin",
        "feature/frozen-head",
        expected_sha,
    )

    assert pushed_sha == expected_sha
    assert git(worktree, "rev-parse", "HEAD").stdout.strip() != expected_sha
    assert (
        git(
            repo,
            "ls-remote",
            str(remote),
            "refs/heads/feature/frozen-head",
        ).stdout.split()[0]
        == expected_sha
    )
    push_spec = [
        spec
        for spec in runner.specs
        if git_operation_args(spec.args)[:1] == ("push",)
    ][-1]
    assert git_operation_args(push_spec.args) == (
        "push",
        "-u",
        str(remote),
        f"{expected_sha}:refs/heads/feature/frozen-head",
    )


def test_feature_push_rejects_remote_sha_changed_before_post_write_verification(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    remote, _, repo, base_sha = local_remote
    runner = HookedRunner()
    adapter = adapter_for(repo, runner=runner)
    adapter.fetch("origin")
    worktree = tmp_path / "verified-feature"
    adapter.create_worktree(
        "origin/develop",
        "feature/verified-head",
        worktree,
    )
    expected_sha = commit_file(worktree, "feature.txt", "expected\n", "expected")
    runner.arm(
        "ls-remote",
        lambda: git(
            remote,
            "update-ref",
            "refs/heads/feature/verified-head",
            base_sha,
        ),
    )

    with pytest.raises(GitError, match="remote SHA verification failed"):
        adapter.push_feature(
            worktree,
            "origin",
            "feature/verified-head",
            expected_sha,
        )


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


def test_worktree_facts_and_commit_tree_cas_return_exact_commit_fact(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    _, _, repo, base_sha = local_remote
    adapter = adapter_for(repo)
    adapter.fetch("origin")
    worktree = tmp_path / "exact-commit"
    adapter.create_worktree(
        "origin/develop",
        "feature/exact-commit",
        worktree,
    )
    (worktree / "exact.txt").write_text("exact\n", encoding="utf-8")

    expected_tree = adapter.staged_tree(worktree, stage_all=True)
    new_sha = adapter.commit_tree_cas(
        worktree,
        "feat: exact commit",
        expected_parent=base_sha,
        expected_tree=expected_tree,
    )

    assert adapter.worktree_head(worktree) == new_sha
    assert adapter.worktree_branch(worktree) == "feature/exact-commit"
    assert adapter.staged_tree(worktree, stage_all=False) == expected_tree
    assert adapter.head_commit_fact(worktree) == {
        "new_head_sha": new_sha,
        "parent_sha": base_sha,
        "commit_tree_sha": expected_tree,
        "message": "feat: exact commit",
    }
    assert git(worktree, "status", "--short").stdout == ""


def test_commit_tree_cas_does_not_overwrite_a_concurrent_head_change(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    _, _, repo, base_sha = local_remote
    runner = HookedRunner()
    adapter = adapter_for(repo, runner=runner)
    adapter.fetch("origin")
    worktree = tmp_path / "commit-cas-race"
    adapter.create_worktree(
        "origin/develop",
        "feature/commit-cas-race",
        worktree,
    )
    (worktree / "ours.txt").write_text("ours\n", encoding="utf-8")
    expected_tree = adapter.staged_tree(worktree, stage_all=True)
    competing_sha: list[str] = []
    runner.arm(
        "update-ref",
        lambda: competing_sha.append(
            commit_file(worktree, "theirs.txt", "theirs\n", "theirs")
        ),
    )

    with pytest.raises(GitError):
        adapter.commit_tree_cas(
            worktree,
            "feat: ours",
            expected_parent=base_sha,
            expected_tree=expected_tree,
        )

    assert adapter.worktree_head(worktree) == competing_sha[0]


@pytest.mark.parametrize("parent_count", [0, 2])
def test_head_commit_fact_rejects_non_single_parent_commits(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
    parent_count: int,
) -> None:
    _, _, repo, base_sha = local_remote
    adapter = adapter_for(repo)
    adapter.fetch("origin")
    worktree = tmp_path / f"parent-count-{parent_count}"
    branch = f"feature/parent-count-{parent_count}"
    adapter.create_worktree("origin/develop", branch, worktree)
    tree_sha = git(worktree, "rev-parse", "HEAD^{tree}").stdout.strip()
    args = ["commit-tree", tree_sha, "-m", "invalid parent shape"]
    if parent_count == 2:
        second_parent = commit_file(worktree, "second.txt", "second\n", "second")
        args.extend(("-p", base_sha, "-p", second_parent))
    invalid_sha = git(worktree, *args).stdout.strip()
    git(worktree, "update-ref", f"refs/heads/{branch}", invalid_sha)

    with pytest.raises(GitError, match="exactly one parent"):
        adapter.head_commit_fact(worktree)


def test_test_push_uses_exact_lease_and_returns_verified_sha(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    remote, seed, repo, base_sha = local_remote
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
    push_spec = [
        spec
        for spec in runner.specs
        if git_operation_args(spec.args)[:1] == ("push",)
    ][-1]
    assert git_operation_args(push_spec.args) == (
        "push",
        str(remote),
        f"{source_sha}:refs/heads/test",
        f"--force-with-lease=refs/heads/test:{observed_sha}",
    )
    assert "-f" not in git_operation_args(push_spec.args)
    assert "--force" not in git_operation_args(push_spec.args)


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
    push_specs = [
        spec
        for spec in runner.specs
        if git_operation_args(spec.args)[:1] == ("push",)
    ]
    lease_pushes = [
        spec
        for spec in push_specs
        if any(
            "force-with-lease" in arg
            for arg in git_operation_args(spec.args)
        )
    ]
    assert len(lease_pushes) == 1
    assert all(
        "-f" not in git_operation_args(spec.args)
        and "--force" not in git_operation_args(spec.args)
        for spec in push_specs
    )


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("fetch", ("-p",)),
        ("remote_sha", ("origin", "-develop")),
        ("create_worktree", ("origin/develop", "-feature", Path("unused"))),
        (
            "push_feature",
            (Path("unused"), "origin", "bad branch", "a" * 40),
        ),
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
        adapter.push_feature(worktree, "origin", "develop", "a" * 40)
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
    expected_sha = commit_file(
        worktree,
        "identity.txt",
        "candidate\n",
        "candidate",
    )
    fetch_count = sum(
        git_operation_args(spec.args)[:1] == ("fetch",)
        for spec in runner.specs
    )
    push_count = sum(
        git_operation_args(spec.args)[:1] == ("push",)
        for spec in runner.specs
    )

    git(repo, "remote", "set-url", "origin", str(replacement))

    with pytest.raises(RemoteIdentityChanged):
        adapter.fetch("origin")
    with pytest.raises(RemoteIdentityChanged):
        adapter.push_feature(
            worktree,
            "origin",
            "feature/identity",
            expected_sha,
        )

    assert sum(
        git_operation_args(spec.args)[:1] == ("fetch",)
        for spec in runner.specs
    ) == fetch_count
    assert sum(
        git_operation_args(spec.args)[:1] == ("push",)
        for spec in runner.specs
    ) == push_count
    assert git(repo, "ls-remote", str(replacement), "refs/heads/feature/identity").stdout == ""


def test_expected_remote_identity_must_match_at_construction(
    local_remote: tuple[Path, Path, Path, str],
) -> None:
    remote, _, repo, _ = local_remote

    adapter_for(
        repo,
        expected_fetch_url=str(remote),
        expected_push_url=str(remote),
    )
    with pytest.raises(RemoteIdentityChanged):
        adapter_for(repo, expected_fetch_url="C:/unexpected/origin.git")


def test_command_scope_remote_identity_wins_fetch_and_push_races(
    local_remote: tuple[Path, Path, Path, str],
    tmp_path: Path,
) -> None:
    remote, seed, repo, _ = local_remote
    evil = tmp_path / "evil.git"
    evil.mkdir()
    git(evil, "init", "--bare")
    runner = HookedRunner()
    adapter = adapter_for(repo, runner=runner)
    latest_sha = commit_file(seed, "race.txt", "bound fetch\n", "bound fetch")
    git(seed, "push", "origin", "develop")
    runner.arm(
        "fetch",
        lambda: git(repo, "remote", "set-url", "origin", str(evil)),
    )

    adapter.fetch("origin")

    fetched = git(
        repo,
        "rev-parse",
        "refs/remotes/origin/develop^{commit}",
    ).stdout.strip()
    assert fetched == latest_sha
    assert git(repo, "ls-remote", str(evil)).stdout == ""

    git(repo, "remote", "set-url", "origin", str(remote))
    push_runner = HookedRunner()
    push_adapter = adapter_for(repo, runner=push_runner)
    worktree = tmp_path / "race-push"
    push_adapter.create_worktree(
        "origin/develop",
        "feature/race-proof",
        worktree,
    )
    source_sha = commit_file(worktree, "push.txt", "bound push\n", "bound push")
    push_runner.arm(
        "push",
        lambda: git(repo, "remote", "set-url", "origin", str(evil)),
    )

    assert (
        push_adapter.push_feature(
            worktree,
            "origin",
            "feature/race-proof",
            source_sha,
        )
        == source_sha
    )
    assert (
        git(
            repo,
            "ls-remote",
            str(remote),
            "refs/heads/feature/race-proof",
        ).stdout.split()[0]
        == source_sha
    )
    assert (
        git(
            repo,
            "ls-remote",
            str(evil),
            "refs/heads/feature/race-proof",
        ).stdout
        == ""
    )


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
