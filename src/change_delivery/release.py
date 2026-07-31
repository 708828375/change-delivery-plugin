"""Read-only production release eligibility checks."""

from __future__ import annotations

from collections.abc import Callable
import json
from typing import Any, Protocol

from pydantic import Field

from change_delivery.adapters.github import MergedPullRequestSnapshot
from change_delivery.domain import EvidenceRecord, FrozenModel, RunRecord, RunState
from change_delivery.errors import BlockedError
from change_delivery.evidence import sha256_payload


class ReadOnlyReleaseRepository(Protocol):
    def get_run(self, run_id: str) -> RunRecord: ...

    def list_evidence(self, run_id: str) -> list[EvidenceRecord]: ...


class ReadOnlyReleaseGitHub(Protocol):
    def merged_snapshot(self, pr_number: int) -> MergedPullRequestSnapshot: ...


class ReleasePreflight(FrozenModel):
    eligible: bool = Field(strict=True)
    run_id: str
    pr_number: int = Field(gt=0, strict=True)
    acceptance_sha: str = Field(pattern=r"^[0-9a-f]{40}$", strict=True)
    merge_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$", strict=True)
    checks_status: str = Field(pattern=r"^passed$", strict=True)
    production_adapter_disabled: bool = Field(strict=True)
    acceptance_evidence_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        strict=True,
    )


class ReleaseService:
    """Observe release eligibility while providing no production write path."""

    def __init__(
        self,
        *,
        repository: ReadOnlyReleaseRepository,
        config_loader: Callable[[str], tuple[Any, str]],
        github_factory: Callable[[Any], ReadOnlyReleaseGitHub],
        production_adapter: object | None = None,
    ) -> None:
        self._repository = repository
        self._config_loader = config_loader
        self._github_factory = github_factory
        self._production_adapter = production_adapter

    def preflight(self, run_id: str) -> ReleasePreflight:
        run = self._repository.get_run(run_id)
        if run.state is not RunState.READY_TO_MERGE:
            raise BlockedError("正式发布预检要求 run 保持 READY_TO_MERGE")
        if run.head_sha is None or run.pr_number is None:
            raise BlockedError("run 缺少已登记的 PR 或 head SHA")

        config, config_hash = self._config_loader(run.project_id)
        if config_hash != run.config_hash:
            raise BlockedError("项目配置已变化，无法执行正式发布预检")
        if (
            getattr(config, "production_enabled", None) is not False
            or self._production_adapter is not None
        ):
            raise BlockedError("第一版要求正式发布适配保持禁用")

        snapshot = self._github_factory(config).merged_snapshot(run.pr_number)
        if not isinstance(snapshot, MergedPullRequestSnapshot):
            raise BlockedError("GitHub 权威事实不完整")
        try:
            remote_number = snapshot.number
            remote_state = snapshot.state
            remote_head_sha = snapshot.head_sha
            merge_commit_sha = snapshot.merge_commit_sha
            checks_status = snapshot.checks_status
        except AttributeError:
            raise BlockedError("GitHub 权威事实不完整") from None
        if remote_number != run.pr_number:
            raise BlockedError("GitHub PR 身份与 run 不匹配")
        if remote_state != "MERGED":
            raise BlockedError("PR 尚未合并")
        if merge_commit_sha is None:
            raise BlockedError("GitHub 权威事实缺少 merge commit SHA")
        if checks_status != "passed":
            raise BlockedError("required checks 尚未全部成功终结")
        if remote_head_sha != run.head_sha:
            raise BlockedError("测试验收 SHA 与 merge 关联不一致")

        acceptance = self._current_acceptance(run)
        return ReleasePreflight(
            eligible=True,
            run_id=run.run_id,
            pr_number=run.pr_number,
            acceptance_sha=run.head_sha,
            merge_commit_sha=merge_commit_sha,
            checks_status="passed",
            production_adapter_disabled=True,
            acceptance_evidence_sha256=acceptance.sha256,
        )

    def execute(self, run_id: str, *_: object, **__: object) -> None:
        del run_id
        raise BlockedError("第一版未启用正式发布写操作")

    def _current_acceptance(self, run: RunRecord) -> EvidenceRecord:
        for evidence in reversed(self._repository.list_evidence(run.run_id)):
            if evidence.kind != "acceptance_passed":
                continue
            try:
                payload = json.loads(evidence.payload_json)
            except (json.JSONDecodeError, TypeError):
                continue
            if (
                type(payload) is dict
                and payload.get("sha") == run.head_sha
                and payload.get("passed") is True
                and sha256_payload(payload) == evidence.sha256
            ):
                return evidence
        raise BlockedError("当前合并 head 缺少严格通过的测试验收证据")
