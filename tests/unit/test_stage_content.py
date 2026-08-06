from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from change_delivery.stage_content import (
    CommitDraft,
    PullRequestDraft,
    commit_draft_schema_json,
    pull_request_draft_schema_json,
)


def test_commit_draft_accepts_subject_and_multiline_body() -> None:
    draft = CommitDraft(message="feat(delivery): 支持自定义提交\n\n补充回归说明")

    assert draft.message.endswith("补充回归说明")
    assert json.loads(commit_draft_schema_json()) == CommitDraft.model_json_schema()


def test_pull_request_draft_accepts_custom_multiline_body() -> None:
    draft = PullRequestDraft(title="支持自定义交付", body="## 变更\n\n- 新能力")

    assert draft.title == "支持自定义交付"
    assert json.loads(
        pull_request_draft_schema_json()
    ) == PullRequestDraft.model_json_schema()


@pytest.mark.parametrize("value", ["", "   ", "bad\x00text", "bad\ttext"])
def test_commit_draft_rejects_blank_or_control_text(value: str) -> None:
    with pytest.raises(ValidationError):
        CommitDraft(message=value)


@pytest.mark.parametrize("field", ["title", "body"])
def test_pull_request_draft_rejects_blank_or_control_text(field: str) -> None:
    data = {"title": "有效标题", "body": "有效正文"}
    data[field] = "bad\x00text"
    with pytest.raises(ValidationError):
        PullRequestDraft.model_validate(data)
    data[field] = "   "
    with pytest.raises(ValidationError):
        PullRequestDraft.model_validate(data)


def test_pull_request_title_rejects_newlines() -> None:
    with pytest.raises(ValidationError):
        PullRequestDraft(title="第一行\n第二行", body="正文")


def test_content_drafts_forbid_extra_fields_and_oversize() -> None:
    with pytest.raises(ValidationError):
        CommitDraft.model_validate({"message": "ok", "command": "git push"})
    with pytest.raises(ValidationError):
        CommitDraft(message="x" * 8_001)
    with pytest.raises(ValidationError):
        PullRequestDraft(title="x" * 257, body="正文")
    with pytest.raises(ValidationError):
        PullRequestDraft(title="标题", body="x" * 64_001)
