from __future__ import annotations

from collections import deque
import json
from pathlib import Path

from pydantic import ValidationError
import pytest

from change_delivery.adapters.github import (
    GitHubAdapter,
    GitHubError,
    HeadChanged,
    PullRequestRef,
    RemoteOperationUncertain,
    ReviewThread,
    SnapshotChanged,
)
from change_delivery.domain import CommandSpec, ProjectConfig
from change_delivery.process import ProcessResult, ProcessTimeoutError


SHA = "a" * 40
OTHER_SHA = "b" * 40
REPOSITORY = "owner/repo"
PR_URL = "https://github.com/owner/repo/pull/152"


def result(
    payload: object = "",
    *,
    exit_code: int = 0,
    stderr: str = "",
) -> ProcessResult:
    stdout = payload if isinstance(payload, str) else json.dumps(payload)
    return ProcessResult(
        command_id="0123456789abcdef",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=1,
    )


class FakeRunner:
    def __init__(self, *outcomes: ProcessResult | Exception) -> None:
        self.outcomes = deque(outcomes)
        self.commands: list[CommandSpec] = []

    def run(self, spec: CommandSpec) -> ProcessResult:
        self.commands.append(spec)
        if not self.outcomes:
            raise AssertionError(f"unexpected command: {spec.args!r}")
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def project_config(tmp_path: Path, **overrides: object) -> ProjectConfig:
    data: dict[str, object] = {
        "project_id": "github-adapter-test",
        "repo_path": tmp_path,
        "remote": "origin",
        "remote_fetch_url": "https://github.com/owner/repo.git",
        "remote_push_url": "https://github.com/owner/repo.git",
        "base_branch": "develop",
        "test_branch": "test",
        "github_repository": REPOSITORY,
        "version_endpoint": "https://example.invalid/version",
        "version_sha_field": "sha",
        "required_pre_pr_checks": [
            {
                "executable": "python",
                "args": ["-m", "pytest"],
                "workdir": tmp_path,
            }
        ],
        "credentials": ["GH_TOKEN", "DATABASE_URL", "GITHUB_TOKEN"],
    }
    data.update(overrides)
    return ProjectConfig.model_validate(data)


def pr_payload(
    *,
    is_draft: bool = True,
    head: str = "feature/cd-test",
    base: str = "develop",
    sha: str = SHA,
) -> dict[str, object]:
    return {
        "number": 152,
        "url": PR_URL,
        "baseRefName": base,
        "headRefName": head,
        "isDraft": is_draft,
        "headRefOid": sha,
    }


def snapshot_payload(
    *,
    sha: str = SHA,
    mergeable: str = "MERGEABLE",
    review_decision: str = "APPROVED",
    conclusions: tuple[str, ...] = ("SUCCESS",),
    is_draft: bool = False,
) -> dict[str, object]:
    return {
        "headRefOid": sha,
        "mergeable": mergeable,
        "reviewDecision": review_decision,
        "statusCheckRollup": [
            {
                "__typename": "CheckRun",
                "name": f"check-{index}",
                "conclusion": conclusion,
                "status": "COMPLETED",
            }
            for index, conclusion in enumerate(conclusions)
        ],
        "isDraft": is_draft,
    }


def thread_payload(
    *,
    thread_id: str = "PRRT_kwDOAA",
    resolved: bool = False,
    outdated: bool = False,
) -> dict[str, object]:
    return {
        "id": thread_id,
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": "src/app.py",
        "line": 42,
        "comments": {
            "nodes": [{"body": "please fix"}],
            "pageInfo": {
                "hasNextPage": False,
                "endCursor": None,
            },
        },
    }


def graphql_payload(*threads: dict[str, object]) -> dict[str, object]:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": list(threads),
                        "pageInfo": {
                            "hasNextPage": False,
                            "endCursor": None,
                        },
                    }
                }
            }
        }
    }


def timeout() -> ProcessTimeoutError:
    return ProcessTimeoutError("0123456789abcdef", "", "")


def test_models_are_frozen_extra_forbid_and_validate_identity() -> None:
    ref = PullRequestRef.model_validate(
        {
            "number": 152,
            "url": PR_URL,
            "base": "develop",
            "head": "feature/cd-test",
            "is_draft": True,
            "head_sha": SHA,
        }
    )
    with pytest.raises(ValidationError):
        ref.number = 153
    with pytest.raises(ValidationError):
        PullRequestRef.model_validate({**ref.model_dump(), "unexpected": True})
    for invalid in (
        {**ref.model_dump(), "number": 0},
        {**ref.model_dump(), "url": "https://example.com/pull/152"},
        {**ref.model_dump(), "head": "-danger"},
        {**ref.model_dump(), "head_sha": "abc"},
    ):
        with pytest.raises(ValidationError):
            PullRequestRef.model_validate(invalid)


def test_review_thread_requires_stable_id_and_is_immutable() -> None:
    review = ReviewThread(
        thread_id="PRRT_kwDOAA",
        resolved=False,
        outdated=False,
        path="src/app.py",
        line=42,
        comments=("please fix",),
    )
    assert review.comments == ("please fix",)
    with pytest.raises(ValidationError):
        review.resolved = True
    with pytest.raises(ValidationError):
        ReviewThread(
            thread_id="bad thread",
            resolved=False,
            outdated=False,
            path=None,
            line=None,
            comments=(),
        )
    with pytest.raises(ValidationError):
        ReviewThread(
            thread_id="PRRT_strict_line",
            resolved=False,
            outdated=False,
            path="a.py",
            line="42",
            comments=(),
        )


def test_ensure_draft_pr_reuses_existing_ready_pr_with_safe_command(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(result(pr_payload(is_draft=False)))
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    ref = adapter.ensure_draft_pr(
        head="feature/cd-test",
        base="develop",
        title="feat: delivery",
        body="**run:** CD-1",
    )

    assert ref.number == 152
    assert not ref.is_draft
    assert len(runner.commands) == 1
    command = runner.commands[0]
    assert command.executable == "gh"
    assert command.args == (
        "pr",
        "view",
        "feature/cd-test",
        "--repo",
        REPOSITORY,
        "--json",
        "number,url,baseRefName,headRefName,isDraft,headRefOid",
    )
    assert command.workdir == tmp_path
    assert command.env_names == ("GH_TOKEN", "GITHUB_TOKEN")


def test_view_same_head_wrong_base_falls_back_to_exact_list(tmp_path: Path) -> None:
    runner = FakeRunner(
        result(pr_payload(base="main")),
        result([pr_payload(base="develop")]),
    )
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    ref = adapter.ensure_draft_pr("feature/cd-test", "develop", "title", "body")

    assert ref.base == "develop"
    assert len(runner.commands) == 2
    assert runner.commands[1].args[:7] == (
        "pr",
        "list",
        "--head",
        "feature/cd-test",
        "--base",
        "develop",
        "--state",
    )


def test_wrong_base_view_and_empty_exact_list_may_create(tmp_path: Path) -> None:
    runner = FakeRunner(
        result(pr_payload(base="main")),
        result([]),
        result("created"),
        result(pr_payload(base="develop")),
    )
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    ref = adapter.ensure_draft_pr("feature/cd-test", "develop", "title", "body")

    assert ref.base == "develop"
    assert sum(command.args[:2] == ("pr", "create") for command in runner.commands) == 1


def test_pr_parser_does_not_coerce_numeric_sha_to_text(tmp_path: Path) -> None:
    payload = pr_payload()
    payload["headRefOid"] = int("1" * 40)
    runner = FakeRunner(result(payload))
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    with pytest.raises(GitHubError):
        adapter.ensure_draft_pr("feature/cd-test", "develop", "title", "body")


def test_create_requires_successful_empty_list_then_authoritative_view(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        result("", exit_code=1),
        result([]),
        result("created"),
        result(pr_payload()),
    )
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    ref = adapter.ensure_draft_pr(
        "feature/cd-test",
        "develop",
        "feat: safe title",
        "body with `markdown`",
    )

    assert ref.head_sha == SHA
    assert [command.args[:3] for command in runner.commands] == [
        ("pr", "view", "feature/cd-test"),
        ("pr", "list", "--head"),
        ("pr", "create", "--draft"),
        ("pr", "view", "feature/cd-test"),
    ]
    create = runner.commands[2]
    assert create.args == (
        "pr",
        "create",
        "--draft",
        "--head",
        "feature/cd-test",
        "--base",
        "develop",
        "--title",
        "feat: safe title",
        "--body",
        "body with `markdown`",
        "--repo",
        REPOSITORY,
    )


@pytest.mark.parametrize(
    "outcomes",
    [
        (result("", exit_code=1), result("", exit_code=1)),
        (result("", exit_code=1), result("{not-json")),
        (result("{not-json"),),
    ],
)
def test_view_list_or_json_failure_never_creates(
    tmp_path: Path,
    outcomes: tuple[ProcessResult, ...],
) -> None:
    runner = FakeRunner(*outcomes)
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    with pytest.raises(GitHubError):
        adapter.ensure_draft_pr("feature/cd-test", "develop", "title", "body")

    assert not any(command.args[:2] == ("pr", "create") for command in runner.commands)


@pytest.mark.parametrize(
    "outcomes",
    [
        (timeout(),),
        (result("", exit_code=1), timeout()),
    ],
)
def test_view_or_list_timeout_is_safe_github_error_and_never_creates(
    tmp_path: Path,
    outcomes: tuple[ProcessResult | Exception, ...],
) -> None:
    runner = FakeRunner(*outcomes)
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    with pytest.raises(GitHubError):
        adapter.ensure_draft_pr("feature/cd-test", "develop", "title", "body")

    assert not any(command.args[:2] == ("pr", "create") for command in runner.commands)


def test_view_failure_list_existing_reuses_without_create(tmp_path: Path) -> None:
    runner = FakeRunner(result("", exit_code=1), result([pr_payload()]))
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    ref = adapter.ensure_draft_pr("feature/cd-test", "develop", "title", "body")

    assert ref.number == 152
    assert not any(command.args[:2] == ("pr", "create") for command in runner.commands)


def test_create_timeout_reconciles_by_view_and_never_replays(tmp_path: Path) -> None:
    runner = FakeRunner(
        result("", exit_code=1),
        result([]),
        timeout(),
        result(pr_payload()),
    )
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    ref = adapter.ensure_draft_pr("feature/cd-test", "develop", "title", "body")

    assert ref.number == 152
    assert sum(command.args[:2] == ("pr", "create") for command in runner.commands) == 1


def test_create_timeout_with_still_empty_list_is_uncertain_and_not_replayed(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        result("", exit_code=1),
        result([]),
        timeout(),
        result("", exit_code=1),
        result([]),
    )
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    with pytest.raises(RemoteOperationUncertain):
        adapter.ensure_draft_pr("feature/cd-test", "develop", "title", "body")

    assert sum(command.args[:2] == ("pr", "create") for command in runner.commands) == 1


def test_mark_ready_is_idempotent_when_already_ready(tmp_path: Path) -> None:
    runner = FakeRunner(result(pr_payload(is_draft=False)))
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    ref = adapter.mark_ready(152)

    assert not ref.is_draft
    assert len(runner.commands) == 1


def test_mark_ready_timeout_reconciles_without_replay(tmp_path: Path) -> None:
    runner = FakeRunner(
        result(pr_payload(is_draft=True)),
        timeout(),
        result(pr_payload(is_draft=False)),
    )
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    ref = adapter.mark_ready(152)

    assert not ref.is_draft
    assert sum(command.args[:2] == ("pr", "ready") for command in runner.commands) == 1


def test_mark_ready_timeout_remaining_draft_is_uncertain(tmp_path: Path) -> None:
    runner = FakeRunner(
        result(pr_payload(is_draft=True)),
        timeout(),
        result(pr_payload(is_draft=True)),
    )
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    with pytest.raises(RemoteOperationUncertain):
        adapter.mark_ready(152)


@pytest.mark.parametrize(
    ("changes", "attribute"),
    [
        ({"conclusions": ()}, "checks_passed"),
        ({"conclusions": ("FAILURE",)}, "checks_passed"),
        ({"conclusions": ("NEUTRAL",)}, "checks_passed"),
        ({"mergeable": "CONFLICTING"}, "mergeable"),
        ({"review_decision": "REVIEW_REQUIRED"}, "review_decision"),
    ],
)
def test_parse_snapshot_fails_closed_for_unready_status(
    tmp_path: Path,
    changes: dict[str, object],
    attribute: str,
) -> None:
    adapter = GitHubAdapter(FakeRunner(), project_config(tmp_path))

    snapshot = adapter.parse_snapshot(snapshot_payload(**changes), ())

    assert not getattr(snapshot, attribute)
    assert not snapshot.ready_to_merge


def test_parse_snapshot_rejects_unknown_status_and_missing_fields(
    tmp_path: Path,
) -> None:
    adapter = GitHubAdapter(FakeRunner(), project_config(tmp_path))

    with pytest.raises(GitHubError):
        adapter.parse_snapshot(snapshot_payload(mergeable="MYSTERY"), ())
    malformed = snapshot_payload()
    malformed.pop("headRefOid")
    with pytest.raises(GitHubError):
        adapter.parse_snapshot(malformed, ())


def test_check_rollup_accepts_gh_metadata_but_still_requires_success(
    tmp_path: Path,
) -> None:
    adapter = GitHubAdapter(FakeRunner(), project_config(tmp_path))
    payload = snapshot_payload()
    payload["statusCheckRollup"] = [
        {
            "__typename": "CheckRun",
            "name": "tests",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "startedAt": "2026-07-29T00:00:00Z",
            "completedAt": "2026-07-29T00:01:00Z",
            "detailsUrl": "https://github.com/owner/repo/actions/runs/1",
            "workflowName": "CI",
        },
        {
            "__typename": "StatusContext",
            "context": "required/external",
            "state": "SUCCESS",
            "startedAt": "2026-07-29T00:00:00Z",
            "targetUrl": "https://example.invalid/check/1",
        },
    ]

    snapshot = adapter.parse_snapshot(payload, ())

    assert snapshot.checks_passed


def test_snapshot_models_and_parser_reject_coerced_boolean_values(
    tmp_path: Path,
) -> None:
    adapter = GitHubAdapter(FakeRunner(), project_config(tmp_path))
    malformed = snapshot_payload()
    malformed["isDraft"] = "false"

    with pytest.raises(GitHubError):
        adapter.parse_snapshot(malformed, ())
    with pytest.raises(ValidationError):
        ReviewThread(
            thread_id="PRRT_strict",
            resolved="false",
            outdated=False,
            path=None,
            line=None,
            comments=(),
        )


def test_unresolved_actionable_thread_blocks_but_outdated_does_not(
    tmp_path: Path,
) -> None:
    adapter = GitHubAdapter(FakeRunner(), project_config(tmp_path))
    unresolved = ReviewThread(
        thread_id="PRRT_unresolved",
        resolved=False,
        outdated=False,
        path="a.py",
        line=1,
        comments=("fix",),
    )
    outdated = ReviewThread(
        thread_id="PRRT_outdated",
        resolved=False,
        outdated=True,
        path="old.py",
        line=2,
        comments=("old",),
    )

    snapshot = adapter.parse_snapshot(snapshot_payload(), (outdated, unresolved))
    only_outdated = adapter.parse_snapshot(snapshot_payload(), (outdated,))

    assert not snapshot.threads_resolved
    assert snapshot.unresolved_thread_ids == ("PRRT_unresolved",)
    assert only_outdated.threads_resolved


def test_snapshot_binds_pr_and_threads_to_unchanged_head(tmp_path: Path) -> None:
    runner = FakeRunner(
        result(snapshot_payload()),
        result(graphql_payload(thread_payload(resolved=True))),
        result(snapshot_payload()),
        result(graphql_payload(thread_payload(resolved=True))),
    )
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    snapshot = adapter.snapshot(152)

    assert snapshot.head_sha == SHA
    assert snapshot.threads_resolved
    assert len(runner.commands) == 4
    assert runner.commands[1].args[:2] == ("api", "graphql")
    assert "--repo" in runner.commands[0].args
    assert "--repo" not in runner.commands[1].args
    assert "--repo" in runner.commands[2].args
    assert "--repo" not in runner.commands[3].args


def test_snapshot_rejects_head_change_instead_of_mixing_evidence(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        result(snapshot_payload()),
        result(graphql_payload()),
        result(snapshot_payload(sha=OTHER_SHA)),
        result(graphql_payload()),
    )
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    with pytest.raises(HeadChanged):
        adapter.snapshot(152)


@pytest.mark.parametrize(
    "second_payload",
    [
        snapshot_payload(review_decision="CHANGES_REQUESTED"),
        snapshot_payload(conclusions=("FAILURE",)),
        snapshot_payload(mergeable="CONFLICTING"),
        snapshot_payload(is_draft=True),
    ],
    ids=["review", "check", "mergeable", "draft"],
)
def test_snapshot_rejects_same_sha_gate_change(
    tmp_path: Path,
    second_payload: dict[str, object],
) -> None:
    runner = FakeRunner(
        result(snapshot_payload()),
        result(graphql_payload()),
        result(second_payload),
        result(graphql_payload()),
    )
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    with pytest.raises(SnapshotChanged):
        adapter.snapshot(152)


def test_snapshot_rejects_same_sha_thread_state_change(tmp_path: Path) -> None:
    runner = FakeRunner(
        result(snapshot_payload()),
        result(graphql_payload(thread_payload(resolved=False))),
        result(snapshot_payload()),
        result(graphql_payload(thread_payload(resolved=True))),
    )
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    with pytest.raises(SnapshotChanged):
        adapter.snapshot(152)


def test_snapshot_ignores_nonsemantic_check_metadata_change(tmp_path: Path) -> None:
    first = snapshot_payload()
    second = snapshot_payload()
    first_check = first["statusCheckRollup"][0]
    second_check = second["statusCheckRollup"][0]
    assert isinstance(first_check, dict)
    assert isinstance(second_check, dict)
    first_check["detailsUrl"] = "https://example.invalid/old"
    first_check["completedAt"] = "2026-07-29T00:00:00Z"
    second_check["detailsUrl"] = "https://example.invalid/new"
    second_check["completedAt"] = "2026-07-29T00:00:01Z"
    runner = FakeRunner(
        result(first),
        result(graphql_payload()),
        result(second),
        result(graphql_payload()),
    )
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    snapshot = adapter.snapshot(152)

    assert snapshot.ready_to_merge


def test_list_actionable_reviews_filters_and_sorts_by_stable_id(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        result(
            graphql_payload(
                thread_payload(thread_id="PRRT_z"),
                thread_payload(thread_id="PRRT_old", outdated=True),
                thread_payload(thread_id="PRRT_done", resolved=True),
                thread_payload(thread_id="PRRT_a"),
            )
        )
    )
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    reviews = adapter.list_actionable_reviews(152)

    assert tuple(review.thread_id for review in reviews) == ("PRRT_a", "PRRT_z")
    assert reviews[0].comments == ("please fix",)


def test_graphql_parser_rejects_missing_fields_and_unknown_shape(
    tmp_path: Path,
) -> None:
    malformed = thread_payload()
    malformed.pop("id")
    runner = FakeRunner(result(graphql_payload(malformed)))
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    with pytest.raises(GitHubError):
        adapter.list_actionable_reviews(152)


def test_complete_comment_page_returns_every_comment(tmp_path: Path) -> None:
    thread = thread_payload()
    thread["comments"] = {
        "nodes": [{"body": "first"}, {"body": "second"}],
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }
    runner = FakeRunner(result(graphql_payload(thread)))
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    reviews = adapter.list_actionable_reviews(152)

    assert reviews[0].comments == ("first", "second")


@pytest.mark.parametrize(
    "comments",
    [
        {"nodes": [{"body": "first page only"}]},
        {
            "nodes": [{"body": "first page only"}],
            "pageInfo": {"hasNextPage": True, "endCursor": "COMMENT_CURSOR"},
        },
        {
            "nodes": [{"body": "invalid flag"}],
            "pageInfo": {"hasNextPage": "false", "endCursor": None},
        },
    ],
)
def test_incomplete_or_invalid_comment_pagination_fails_closed(
    tmp_path: Path,
    comments: dict[str, object],
) -> None:
    thread = thread_payload()
    thread["comments"] = comments
    runner = FakeRunner(result(graphql_payload(thread)))
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    with pytest.raises(GitHubError):
        adapter.list_actionable_reviews(152)


@pytest.mark.parametrize(
    "payload",
    [
        {
            **graphql_payload(),
            "errors": [{"message": "partial result"}],
        },
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [],
                            "pageInfo": {
                                "hasNextPage": True,
                                "endCursor": "CURSOR",
                            },
                        }
                    }
                }
            }
        },
    ],
)
def test_graphql_errors_or_incomplete_pagination_fail_closed(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    runner = FakeRunner(result(payload))
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    with pytest.raises(GitHubError):
        adapter.list_actionable_reviews(152)


def test_graphql_command_contract_uses_variables_without_unsupported_repo_flag(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(result(graphql_payload()))
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    assert adapter.list_actionable_reviews(152) == ()

    args = runner.commands[0].args
    assert args[:2] == ("api", "graphql")
    assert "--repo" not in args
    assert "owner=owner" in args
    assert "name=repo" in args
    assert "number=152" in args


def test_reply_to_thread_uses_stable_id_and_direct_argv(tmp_path: Path) -> None:
    runner = FakeRunner(
        result(graphql_payload(thread_payload())),
        result({"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "C1"}}}})
    )
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    adapter.reply_to_thread(152, "PRRT_kwDOAA", "fixed `value`")

    command = runner.commands[1]
    assert command.args[:2] == ("api", "graphql")
    assert "--repo" not in command.args
    assert "threadId=PRRT_kwDOAA" in command.args
    assert "body=fixed `value`" in command.args
    membership_args = runner.commands[0].args
    assert "owner=owner" in membership_args
    assert "name=repo" in membership_args
    assert "number=152" in membership_args
    assert command.env_names == ("GH_TOKEN", "GITHUB_TOKEN")


def test_reply_timeout_is_uncertain_and_never_retried(tmp_path: Path) -> None:
    runner = FakeRunner(result(graphql_payload(thread_payload())), timeout())
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    with pytest.raises(RemoteOperationUncertain):
        adapter.reply_to_thread(152, "PRRT_kwDOAA", "fixed")

    assert len(runner.commands) == 2


def test_reply_rejects_thread_not_bound_to_requested_pr(tmp_path: Path) -> None:
    runner = FakeRunner(result(graphql_payload(thread_payload(thread_id="PRRT_other"))))
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    with pytest.raises(ValueError, match="does not belong"):
        adapter.reply_to_thread(152, "PRRT_kwDOAA", "fixed")

    assert len(runner.commands) == 1


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("ensure_draft_pr", ("-danger", "develop", "title", "body")),
        ("ensure_draft_pr", ("feature/ok", "bad branch", "title", "body")),
        ("ensure_draft_pr", ("feature/ok", "develop", "bad\nsubject", "body")),
        ("ensure_draft_pr", ("feature/ok", "develop", "bad\x1bsubject", "body")),
        ("ensure_draft_pr", ("feature/ok", "develop", "title", "bad\r\nbody")),
        ("ensure_draft_pr", ("feature/ok", "develop", "title", "bad\x07body")),
        ("mark_ready", (0,)),
        ("reply_to_thread", (152, "-bad", "body")),
        ("reply_to_thread", (152, "bad thread", "body")),
        ("reply_to_thread", (152, "PRRT_ok", "bad\x00body")),
    ],
)
def test_untrusted_inputs_are_rejected_before_process(
    tmp_path: Path,
    method: str,
    arguments: tuple[object, ...],
) -> None:
    runner = FakeRunner()
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    with pytest.raises((ValueError, ValidationError)):
        getattr(adapter, method)(*arguments)

    assert runner.commands == []


@pytest.mark.parametrize(
    "repository",
    ["-owner/repo", "owner/-repo", "owner/repo;evil", "owner/repo\x00x"],
)
def test_adapter_rejects_malicious_repository_binding(
    tmp_path: Path,
    repository: str,
) -> None:
    runner = FakeRunner()
    with pytest.raises((ValueError, ValidationError)):
        GitHubAdapter(runner, project_config(tmp_path, github_repository=repository))
    assert runner.commands == []


def test_json_and_process_errors_do_not_leak_tokens(tmp_path: Path) -> None:
    secret = "ghp_super_secret_value"
    runner = FakeRunner(
        result(
            f'{{"token":"{secret}"',
            stderr=f"Authorization: Bearer {secret}",
        )
    )
    adapter = GitHubAdapter(runner, project_config(tmp_path))

    with pytest.raises(GitHubError) as caught:
        adapter.ensure_draft_pr("feature/cd-test", "develop", "title", "body")

    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert secret not in repr(caught.value.__dict__)
