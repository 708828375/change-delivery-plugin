from __future__ import annotations

import json
import re

from pydantic import Field, field_validator

from change_delivery.domain import FrozenModel


_SINGLE_LINE_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MULTILINE_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")


class CommitDraft(FrozenModel):
    message: str = Field(min_length=1, max_length=8_000)

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or _MULTILINE_CONTROL.search(normalized):
            raise ValueError("commit message 不能为空或包含控制字符")
        return normalized


class PullRequestDraft(FrozenModel):
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=64_000)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or _SINGLE_LINE_CONTROL.search(normalized):
            raise ValueError("PR title 必须是无控制字符的非空单行文本")
        return normalized

    @field_validator("body")
    @classmethod
    def normalize_body(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or _MULTILINE_CONTROL.search(normalized):
            raise ValueError("PR body 不能为空或包含控制字符")
        return normalized


def commit_draft_schema_json() -> str:
    return json.dumps(
        CommitDraft.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def pull_request_draft_schema_json() -> str:
    return json.dumps(
        PullRequestDraft.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
