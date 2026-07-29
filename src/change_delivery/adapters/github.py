from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import re
from typing import Any, Literal, Protocol

from pydantic import Field, ValidationError, field_validator

from change_delivery.domain import CommandSpec, FrozenModel, ProjectConfig
from change_delivery.errors import DeliveryError
from change_delivery.process import ProcessResult, ProcessTimeoutError


_BRANCH_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?$"
)
_THREAD_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{2,255}={0,2}$")
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_GH_ENV_NAMES = ("GH_TOKEN", "GITHUB_TOKEN")
_PR_FIELDS = "number,url,baseRefName,headRefName,isDraft,headRefOid"
_SNAPSHOT_FIELDS = (
    "headRefOid,mergeable,reviewDecision,statusCheckRollup,isDraft"
)
_KNOWN_CHECK_CONCLUSIONS = frozenset(
    {
        "SUCCESS",
        "FAILURE",
        "CANCELLED",
        "TIMED_OUT",
        "ACTION_REQUIRED",
        "NEUTRAL",
        "SKIPPED",
        "STALE",
        "STARTUP_FAILURE",
    }
)
_KNOWN_CHECK_STATUSES = frozenset(
    {
        "COMPLETED",
        "QUEUED",
        "IN_PROGRESS",
        "WAITING",
        "PENDING",
        "REQUESTED",
    }
)
_THREAD_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){"
    "repository(owner:$owner,name:$name){pullRequest(number:$number){"
    "reviewThreads(first:100){nodes{id isResolved isOutdated path line "
    "comments(first:100){nodes{body}pageInfo{hasNextPage endCursor}}}"
    "pageInfo{hasNextPage endCursor}}}}}}"
)
_REPLY_MUTATION = (
    "mutation($threadId:ID!,$body:String!){"
    "addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$threadId,"
    "body:$body}){comment{id}}}"
)


class Runner(Protocol):
    def run(self, spec: CommandSpec) -> ProcessResult: ...


class GitHubError(DeliveryError):
    """A GitHub operation failed without retaining remote output or credentials."""

    def __init__(self, operation: str, reason: str = "GitHub operation failed") -> None:
        self.operation = operation
        self.reason = reason
        super().__init__(f"GitHub {operation} failed: {reason}")


class RemoteOperationUncertain(GitHubError):
    """A remote write may have succeeded and must not be blindly replayed."""


class SnapshotChanged(GitHubError):
    """Pull request gate evidence changed while it was being collected."""


# Backward-compatible name for callers that only distinguished head changes.
HeadChanged = SnapshotChanged


class PullRequestRef(FrozenModel):
    number: int = Field(gt=0, strict=True)
    url: str = Field(
        strict=True,
        pattern=(
            r"^https://github\.com/"
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*$"
        )
    )
    base: str
    head: str
    is_draft: bool = Field(strict=True)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$", strict=True)

    @field_validator("base", "head")
    @classmethod
    def validate_branch(cls, value: str) -> str:
        _validate_branch(value)
        return value

    @field_validator("head_sha")
    @classmethod
    def normalize_sha(cls, value: str) -> str:
        return value.lower()


class ReviewThread(FrozenModel):
    thread_id: str = Field(
        pattern=r"^[A-Za-z0-9_][A-Za-z0-9_-]{2,255}={0,2}$",
        strict=True,
    )
    resolved: bool = Field(strict=True)
    outdated: bool = Field(strict=True)
    path: str | None
    line: int | None = Field(default=None, gt=0, strict=True)
    comments: tuple[str, ...]

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        if value is not None and (
            not value
            or "\x00" in value
            or "\r" in value
            or "\n" in value
        ):
            raise ValueError("invalid review path")
        return value

    @field_validator("comments")
    @classmethod
    def validate_comments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any("\x00" in comment for comment in value):
            raise ValueError("invalid review comment")
        return value


class CheckSnapshot(FrozenModel):
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$", strict=True)
    mergeable: bool = Field(strict=True)
    review_decision: bool = Field(strict=True)
    checks_passed: bool = Field(strict=True)
    threads_resolved: bool = Field(strict=True)
    unresolved_thread_ids: tuple[str, ...]
    is_draft: bool = Field(strict=True)

    @property
    def ready_to_merge(self) -> bool:
        return (
            not self.is_draft
            and self.mergeable
            and self.review_decision
            and self.checks_passed
            and self.threads_resolved
        )


def _validate_branch(branch: str) -> None:
    if (
        not isinstance(branch, str)
        or not branch
        or branch.startswith("-")
        or branch.startswith(".")
        or branch.endswith("/")
        or branch.endswith(".")
        or branch == "@"
        or ".." in branch
        or "@{" in branch
        or "//" in branch
        or _BRANCH_FORBIDDEN.search(branch)
        or any(component.endswith(".lock") for component in branch.split("/"))
    ):
        raise ValueError("invalid branch name")


def _validate_number(number: int) -> None:
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise ValueError("pull request number must be positive")


def _validate_title(title: str) -> None:
    if (
        not isinstance(title, str)
        or not title.strip()
        or len(title) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in title)
    ):
        raise ValueError("pull request title is invalid")


def _validate_body(body: str, *, allow_empty: bool = True) -> None:
    if (
        not isinstance(body, str)
        or (not allow_empty and not body)
        or len(body) > 65_535
        or any(
            (ord(character) < 32 and character not in "\n\t")
            or ord(character) == 127
            for character in body
        )
    ):
        raise ValueError("body is invalid")


def _validate_thread_id(thread_id: str) -> None:
    if not isinstance(thread_id, str) or not _THREAD_ID.fullmatch(thread_id):
        raise ValueError("review thread ID is invalid")


def _json_object(result: ProcessResult, operation: str) -> dict[str, Any]:
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        raise GitHubError(operation, "response was not valid JSON") from None
    if not isinstance(payload, dict):
        raise GitHubError(operation, "response did not contain an object")
    return payload


def _json_list(result: ProcessResult, operation: str) -> list[Any]:
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        raise GitHubError(operation, "response was not valid JSON") from None
    if not isinstance(payload, list):
        raise GitHubError(operation, "response did not contain a list")
    return payload


def _exact_keys(
    payload: Mapping[str, Any],
    keys: set[str],
    operation: str,
) -> None:
    if set(payload) != keys:
        raise GitHubError(operation, "response fields did not match the contract")


class GitHubAdapter:
    def __init__(self, runner: Runner, project_config: ProjectConfig) -> None:
        repository = project_config.github_repository
        if not _REPOSITORY.fullmatch(repository):
            raise ValueError("GitHub repository binding is invalid")
        repo_path = Path(project_config.repo_path).resolve()
        if not repo_path.is_dir():
            raise NotADirectoryError("project repository path must exist")
        self._runner = runner
        self._repository = repository
        self._repo_path = repo_path
        self._owner, self._name = repository.split("/", 1)
        configured_credentials = set(project_config.credentials)
        self._env_names = tuple(
            name for name in _GH_ENV_NAMES if name in configured_credentials
        )

    def find_pr(self, head: str, base: str) -> PullRequestRef | None:
        """Return an existing exact PR without creating or mutating it."""
        _validate_branch(head)
        _validate_branch(base)
        try:
            return self._locate_pr(head, base)
        except ProcessTimeoutError:
            raise GitHubError(
                "locate_pr",
                "pull request existence is unknown",
            ) from None

    def get_pr(self, pr_number: int) -> PullRequestRef:
        """Return one PR snapshot without mutating it."""
        _validate_number(pr_number)
        return self._view_pr(pr_number)

    def ensure_draft_pr(
        self,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> PullRequestRef:
        _validate_branch(head)
        _validate_branch(base)
        _validate_title(title)
        _validate_body(body)

        try:
            existing = self._locate_pr(head, base)
        except ProcessTimeoutError:
            raise GitHubError(
                "locate_pr",
                "pull request existence is unknown",
            ) from None
        if existing is not None:
            return existing

        create_args = (
            "pr",
            "create",
            "--draft",
            "--head",
            head,
            "--base",
            base,
            "--title",
            title,
            "--body",
            body,
            "--repo",
            self._repository,
        )
        try:
            create_result = self._execute(create_args)
        except ProcessTimeoutError:
            return self._reconcile_uncertain_create(head, base)
        if create_result.exit_code != 0:
            return self._reconcile_uncertain_create(head, base)

        authoritative = self._locate_pr(head, base)
        if authoritative is None:
            raise GitHubError("create_pr", "created pull request was not observable")
        return authoritative

    def mark_ready(self, pr_number: int) -> PullRequestRef:
        _validate_number(pr_number)
        current = self._view_pr(pr_number)
        if not current.is_draft:
            return current
        try:
            ready_result = self._execute(
                ("pr", "ready", str(pr_number), "--repo", self._repository)
            )
        except ProcessTimeoutError:
            return self._reconcile_ready(pr_number)
        if ready_result.exit_code != 0:
            return self._reconcile_ready(pr_number)
        authoritative = self._view_pr(pr_number)
        if authoritative.is_draft:
            raise GitHubError("mark_ready", "pull request remained a draft")
        return authoritative

    def snapshot(self, pr_number: int) -> CheckSnapshot:
        _validate_number(pr_number)
        first_payload, first_threads = self._snapshot_sample(pr_number)
        second_payload, second_threads = self._snapshot_sample(pr_number)
        if (
            self._gate_fingerprint(first_payload)
            != self._gate_fingerprint(second_payload)
            or self._thread_fingerprint(first_threads)
            != self._thread_fingerprint(second_threads)
        ):
            raise SnapshotChanged(
                "snapshot",
                "pull request gate evidence changed",
            )
        return self.parse_snapshot(second_payload, second_threads)

    def parse_snapshot(
        self,
        payload: Mapping[str, Any],
        threads: Sequence[ReviewThread] = (),
    ) -> CheckSnapshot:
        if not isinstance(payload, Mapping):
            raise GitHubError("snapshot", "response did not contain an object")
        _exact_keys(
            payload,
            {
                "headRefOid",
                "mergeable",
                "reviewDecision",
                "statusCheckRollup",
                "isDraft",
            },
            "snapshot",
        )
        sha = self._payload_sha(payload, "snapshot")
        mergeable_raw = payload["mergeable"]
        if mergeable_raw not in {"MERGEABLE", "CONFLICTING", "UNKNOWN"}:
            raise GitHubError("snapshot", "unknown mergeability state")
        review_raw = payload["reviewDecision"]
        if review_raw not in {
            "APPROVED",
            "CHANGES_REQUESTED",
            "REVIEW_REQUIRED",
            None,
            "",
        }:
            raise GitHubError("snapshot", "unknown review decision")
        is_draft = payload["isDraft"]
        if not isinstance(is_draft, bool):
            raise GitHubError("snapshot", "draft state was invalid")
        checks_passed = self._checks_passed(payload["statusCheckRollup"])
        actionable_ids = tuple(
            sorted(
                thread.thread_id
                for thread in threads
                if not thread.resolved and not thread.outdated
            )
        )
        return CheckSnapshot(
            head_sha=sha,
            mergeable=mergeable_raw == "MERGEABLE",
            review_decision=review_raw == "APPROVED",
            checks_passed=checks_passed,
            threads_resolved=not actionable_ids,
            unresolved_thread_ids=actionable_ids,
            is_draft=is_draft,
        )

    def list_actionable_reviews(
        self,
        pr_number: int,
    ) -> tuple[ReviewThread, ...]:
        _validate_number(pr_number)
        return tuple(
            sorted(
                (
                    thread
                    for thread in self._query_threads(pr_number)
                    if not thread.resolved and not thread.outdated
                ),
                key=lambda thread: thread.thread_id,
            )
        )

    def reply_to_thread(
        self,
        pr_number: int,
        thread_id: str,
        body: str,
    ) -> None:
        _validate_number(pr_number)
        _validate_thread_id(thread_id)
        _validate_body(body, allow_empty=False)
        threads = self._query_threads(pr_number)
        matching = [
            thread
            for thread in threads
            if thread.thread_id == thread_id
            and not thread.resolved
            and not thread.outdated
        ]
        if not matching:
            raise ValueError(
                "review thread does not belong to the pull request "
                "or is not actionable"
            )
        args = (
            "api",
            "graphql",
            "-f",
            f"query={_REPLY_MUTATION}",
            "-f",
            f"threadId={thread_id}",
            "-f",
            f"body={body}",
        )
        try:
            response = self._execute(args)
        except ProcessTimeoutError:
            raise RemoteOperationUncertain(
                "reply_to_thread",
                "reply result is unknown",
            ) from None
        if response.exit_code != 0:
            raise GitHubError("reply_to_thread")
        payload = _json_object(response, "reply_to_thread")
        _exact_keys(payload, {"data"}, "reply_to_thread")
        try:
            data = payload["data"]
            if not isinstance(data, dict):
                raise TypeError
            _exact_keys(
                data,
                {"addPullRequestReviewThreadReply"},
                "reply_to_thread",
            )
            mutation = data["addPullRequestReviewThreadReply"]
            if not isinstance(mutation, dict):
                raise TypeError
            _exact_keys(mutation, {"comment"}, "reply_to_thread")
            if not isinstance(mutation["comment"], dict):
                raise TypeError
            _exact_keys(mutation["comment"], {"id"}, "reply_to_thread")
            comment_id = mutation["comment"]["id"]
        except (KeyError, TypeError):
            raise GitHubError(
                "reply_to_thread",
                "response did not confirm the reply",
            ) from None
        if not isinstance(comment_id, str) or not comment_id:
            raise GitHubError("reply_to_thread", "reply ID was invalid")

    def _locate_pr(self, head: str, base: str) -> PullRequestRef | None:
        try:
            view_result = self._execute(
                (
                    "pr",
                    "view",
                    head,
                    "--repo",
                    self._repository,
                    "--json",
                    _PR_FIELDS,
                )
            )
        except ProcessTimeoutError:
            raise GitHubError(
                "locate_pr",
                "pull request existence is unknown",
            ) from None
        if view_result.exit_code == 0:
            ref = self._parse_pr(_json_object(view_result, "view_pr"))
            if ref.head != head:
                raise GitHubError(
                    "locate_pr",
                    "pull request identity did not match",
                )
            if ref.base == base:
                return ref

        try:
            list_result = self._execute(
                (
                    "pr",
                    "list",
                    "--head",
                    head,
                    "--base",
                    base,
                    "--state",
                    "open",
                    "--repo",
                    self._repository,
                    "--json",
                    _PR_FIELDS,
                )
            )
        except ProcessTimeoutError:
            raise GitHubError(
                "locate_pr",
                "pull request existence is unknown",
            ) from None
        if list_result.exit_code != 0:
            raise GitHubError("locate_pr", "pull request existence is unknown")
        items = _json_list(list_result, "list_pr")
        if not items:
            return None
        if len(items) != 1 or not isinstance(items[0], dict):
            raise GitHubError("list_pr", "expected exactly one pull request")
        ref = self._parse_pr(items[0])
        self._require_pr_identity(ref, head, base)
        return ref

    def _view_pr(self, selector: int | str) -> PullRequestRef:
        try:
            result = self._execute(
                (
                    "pr",
                    "view",
                    str(selector),
                    "--repo",
                    self._repository,
                    "--json",
                    _PR_FIELDS,
                )
            )
        except ProcessTimeoutError:
            raise GitHubError("view_pr", "pull request state is unknown") from None
        if result.exit_code != 0:
            raise GitHubError("view_pr")
        return self._parse_pr(_json_object(result, "view_pr"))

    def _parse_pr(self, payload: Mapping[str, Any]) -> PullRequestRef:
        _exact_keys(
            payload,
            {
                "number",
                "url",
                "baseRefName",
                "headRefName",
                "isDraft",
                "headRefOid",
            },
            "parse_pr",
        )
        if not isinstance(payload["headRefOid"], str):
            raise GitHubError("parse_pr", "pull request response was invalid")
        try:
            ref = PullRequestRef(
                number=payload["number"],
                url=payload["url"],
                base=payload["baseRefName"],
                head=payload["headRefName"],
                is_draft=payload["isDraft"],
                head_sha=payload["headRefOid"].lower(),
            )
        except (ValidationError, ValueError, TypeError):
            raise GitHubError("parse_pr", "pull request response was invalid") from None
        expected_prefix = f"https://github.com/{self._repository}/pull/"
        if not ref.url.startswith(expected_prefix):
            raise GitHubError("parse_pr", "pull request repository did not match")
        return ref

    @staticmethod
    def _require_pr_identity(
        ref: PullRequestRef,
        head: str,
        base: str,
    ) -> None:
        if ref.head != head or ref.base != base:
            raise GitHubError("locate_pr", "pull request identity did not match")

    def _reconcile_uncertain_create(
        self,
        head: str,
        base: str,
    ) -> PullRequestRef:
        try:
            reconciled = self._locate_pr(head, base)
        except (GitHubError, ProcessTimeoutError):
            raise RemoteOperationUncertain(
                "create_pr",
                "create result is unknown",
            ) from None
        if reconciled is None:
            raise RemoteOperationUncertain(
                "create_pr",
                "create result is unknown",
            )
        return reconciled

    def _reconcile_ready(self, pr_number: int) -> PullRequestRef:
        try:
            reconciled = self._view_pr(pr_number)
        except (GitHubError, ProcessTimeoutError):
            raise RemoteOperationUncertain(
                "mark_ready",
                "ready result is unknown",
            ) from None
        if reconciled.is_draft:
            raise RemoteOperationUncertain(
                "mark_ready",
                "ready result is unknown",
            )
        return reconciled

    def _snapshot_payload(self, pr_number: int) -> dict[str, Any]:
        try:
            result = self._execute(
                (
                    "pr",
                    "view",
                    str(pr_number),
                    "--repo",
                    self._repository,
                    "--json",
                    _SNAPSHOT_FIELDS,
                )
            )
        except ProcessTimeoutError:
            raise GitHubError("snapshot", "pull request state is unknown") from None
        if result.exit_code != 0:
            raise GitHubError("snapshot")
        return _json_object(result, "snapshot")

    @staticmethod
    def _payload_sha(payload: Mapping[str, Any], operation: str) -> str:
        sha = payload.get("headRefOid")
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise GitHubError(operation, "head SHA was invalid")
        return sha.lower()

    @staticmethod
    def _checks_passed(raw_checks: Any) -> bool:
        normalized = GitHubAdapter._normalize_checks(raw_checks)
        if not normalized:
            return False
        return all(
            (
                entry[0] == "CheckRun"
                and entry[3] == "COMPLETED"
                and entry[4] == "SUCCESS"
            )
            or (
                entry[0] == "StatusContext"
                and entry[3] == "SUCCESS"
            )
            for entry in normalized
        )

    @staticmethod
    def _normalize_checks(raw_checks: Any) -> tuple[tuple[str, ...], ...]:
        if not isinstance(raw_checks, list):
            raise GitHubError("snapshot", "check rollup was invalid")
        normalized: list[tuple[str, ...]] = []
        for raw in raw_checks:
            if not isinstance(raw, dict):
                raise GitHubError("snapshot", "check entry was invalid")
            typename = raw.get("__typename")
            if typename == "CheckRun":
                if not {
                    "__typename",
                    "name",
                    "conclusion",
                    "status",
                }.issubset(raw):
                    raise GitHubError("snapshot", "check entry was incomplete")
                status = raw["status"]
                conclusion = raw["conclusion"]
                if not isinstance(raw["name"], str):
                    raise GitHubError("snapshot", "check name was invalid")
                if status not in _KNOWN_CHECK_STATUSES:
                    raise GitHubError("snapshot", "unknown check status")
                if conclusion is not None and conclusion not in _KNOWN_CHECK_CONCLUSIONS:
                    raise GitHubError("snapshot", "unknown check conclusion")
                workflow_name = raw.get("workflowName")
                if workflow_name is not None and not isinstance(workflow_name, str):
                    raise GitHubError("snapshot", "check workflow was invalid")
                normalized.append(
                    (
                        "CheckRun",
                        workflow_name or "",
                        raw["name"],
                        status,
                        conclusion or "",
                    )
                )
            elif typename == "StatusContext":
                if not {
                    "__typename",
                    "context",
                    "state",
                }.issubset(raw):
                    raise GitHubError("snapshot", "status context was incomplete")
                state = raw["state"]
                if not isinstance(raw["context"], str):
                    raise GitHubError("snapshot", "status context was invalid")
                if state not in {
                    "SUCCESS",
                    "ERROR",
                    "FAILURE",
                    "PENDING",
                    "EXPECTED",
                }:
                    raise GitHubError("snapshot", "unknown status context state")
                normalized.append(
                    (
                        "StatusContext",
                        "",
                        raw["context"],
                        state,
                        "",
                    )
                )
            else:
                raise GitHubError("snapshot", "unknown check entry type")
        return tuple(sorted(normalized))

    def _query_threads(self, pr_number: int) -> tuple[ReviewThread, ...]:
        try:
            result = self._execute(
                (
                    "api",
                    "graphql",
                    "-f",
                    f"query={_THREAD_QUERY}",
                    "-F",
                    f"owner={self._owner}",
                    "-F",
                    f"name={self._name}",
                    "-F",
                    f"number={pr_number}",
                )
            )
        except ProcessTimeoutError:
            raise GitHubError(
                "review_threads",
                "review thread state is unknown",
            ) from None
        if result.exit_code != 0:
            raise GitHubError("review_threads")
        payload = _json_object(result, "review_threads")
        _exact_keys(payload, {"data"}, "review_threads")
        try:
            data = payload["data"]
            if not isinstance(data, dict):
                raise TypeError
            _exact_keys(data, {"repository"}, "review_threads")
            repository = data["repository"]
            if not isinstance(repository, dict):
                raise TypeError
            _exact_keys(repository, {"pullRequest"}, "review_threads")
            pull_request = repository["pullRequest"]
            if not isinstance(pull_request, dict):
                raise TypeError
            _exact_keys(pull_request, {"reviewThreads"}, "review_threads")
            review_threads = pull_request["reviewThreads"]
            if not isinstance(review_threads, dict):
                raise TypeError
            _exact_keys(
                review_threads,
                {"nodes", "pageInfo"},
                "review_threads",
            )
            nodes = review_threads["nodes"]
            page_info = review_threads["pageInfo"]
        except (KeyError, TypeError):
            raise GitHubError(
                "review_threads",
                "thread response was incomplete",
            ) from None
        if not isinstance(nodes, list):
            raise GitHubError("review_threads", "thread nodes were invalid")
        if not isinstance(page_info, dict):
            raise GitHubError("review_threads", "page information was invalid")
        _exact_keys(
            page_info,
            {"hasNextPage", "endCursor"},
            "review_threads",
        )
        if not isinstance(page_info["hasNextPage"], bool):
            raise GitHubError("review_threads", "page information was invalid")
        if page_info["hasNextPage"]:
            raise GitHubError(
                "review_threads",
                "review thread result was incomplete",
            )
        return tuple(self._parse_thread(node) for node in nodes)

    def _snapshot_sample(
        self,
        pr_number: int,
    ) -> tuple[dict[str, Any], tuple[ReviewThread, ...]]:
        payload = self._snapshot_payload(pr_number)
        threads = self._query_threads(pr_number)
        # Validate every sample before it participates in comparison.
        self.parse_snapshot(payload, threads)
        return payload, threads

    def _gate_fingerprint(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[object, ...]:
        # parse_snapshot validates all gate fields and unknown states.
        self.parse_snapshot(payload, ())
        return (
            self._payload_sha(payload, "snapshot"),
            payload["mergeable"],
            payload["reviewDecision"],
            payload["isDraft"],
            self._normalize_checks(payload["statusCheckRollup"]),
        )

    @staticmethod
    def _thread_fingerprint(
        threads: Sequence[ReviewThread],
    ) -> tuple[tuple[object, ...], ...]:
        return tuple(
            sorted(
                (
                    thread.thread_id,
                    thread.resolved,
                    thread.outdated,
                    thread.path,
                    thread.line,
                    thread.comments,
                )
                for thread in threads
            )
        )

    @staticmethod
    def _parse_thread(payload: Any) -> ReviewThread:
        if not isinstance(payload, dict):
            raise GitHubError("review_threads", "thread was invalid")
        _exact_keys(
            payload,
            {"id", "isResolved", "isOutdated", "path", "line", "comments"},
            "review_threads",
        )
        comments = payload["comments"]
        if not isinstance(comments, dict):
            raise GitHubError("review_threads", "thread comments were invalid")
        _exact_keys(
            comments,
            {"nodes", "pageInfo"},
            "review_threads",
        )
        nodes = comments["nodes"]
        if not isinstance(nodes, list):
            raise GitHubError("review_threads", "thread comments were invalid")
        page_info = comments["pageInfo"]
        if not isinstance(page_info, dict):
            raise GitHubError(
                "review_threads",
                "comment page information was invalid",
            )
        _exact_keys(
            page_info,
            {"hasNextPage", "endCursor"},
            "review_threads",
        )
        has_next_page = page_info["hasNextPage"]
        end_cursor = page_info["endCursor"]
        if (
            not isinstance(has_next_page, bool)
            or (end_cursor is not None and not isinstance(end_cursor, str))
        ):
            raise GitHubError(
                "review_threads",
                "comment page information was invalid",
            )
        if has_next_page:
            raise GitHubError(
                "review_threads",
                "review comment result was incomplete",
            )
        bodies: list[str] = []
        for node in nodes:
            if not isinstance(node, dict) or set(node) != {"body"}:
                raise GitHubError("review_threads", "thread comment was invalid")
            body = node["body"]
            if not isinstance(body, str):
                raise GitHubError("review_threads", "thread comment was invalid")
            bodies.append(body)
        try:
            return ReviewThread(
                thread_id=payload["id"],
                resolved=payload["isResolved"],
                outdated=payload["isOutdated"],
                path=payload["path"],
                line=payload["line"],
                comments=tuple(bodies),
            )
        except (ValidationError, ValueError, TypeError):
            raise GitHubError("review_threads", "thread was invalid") from None

    def _execute(self, args: Sequence[str]) -> ProcessResult:
        return self._runner.run(
            CommandSpec(
                executable="gh",
                args=tuple(args),
                workdir=self._repo_path,
                timeout_seconds=60,
                env_names=self._env_names,
            )
        )
