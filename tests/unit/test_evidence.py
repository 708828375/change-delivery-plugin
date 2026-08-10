from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError
from pydantic import ValidationError

from change_delivery.domain import (
    EvidenceRef,
    Finding,
    StageResult,
    stage_result_schema_json,
)
from change_delivery.evidence import build_evidence_ref, sha256_payload


def valid_evidence_data() -> dict[str, str]:
    return {
        "evidence_id": "ci-run-123",
        "kind": "ci_passed",
        "sha256": "a" * 64,
    }


def valid_stage_data() -> dict[str, object]:
    return {
        "status": "passed",
        "summary": "验证通过",
        "evidence": [valid_evidence_data()],
        "findings": [
            {
                "severity": "minor",
                "title": "提示",
                "evidence": "日志第 10 行",
                "actionable": False,
            }
        ],
        "next_action": "进入下一阶段",
    }


def stage_schema_validator() -> Draft202012Validator:
    return Draft202012Validator(StageResult.model_json_schema())


def test_canonical_json_hash_is_stable_across_mapping_order() -> None:
    first = {"name": "测试", "nested": {"b": 2, "a": [True, None]}}
    second = {"nested": {"a": [True, None], "b": 2}, "name": "测试"}

    assert sha256_payload(first) == sha256_payload(second)


def test_canonical_json_hash_changes_with_semantics() -> None:
    assert sha256_payload({"count": 1}) != sha256_payload({"count": 2})


@pytest.mark.parametrize(
    "payload",
    [
        {"value": object()},
        {"value": math.nan},
        {"value": math.inf},
        {"value": -math.inf},
        {1: "x"},
        b"bytes",
        bytearray(b"bytes"),
        Path("C:/absolute/secret.txt"),
    ],
)
def test_hash_rejects_values_outside_canonical_json(payload: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        sha256_payload(payload)


def test_hash_rejects_self_referential_container() -> None:
    payload: list[object] = []
    payload.append(payload)

    with pytest.raises(ValueError, match="循环"):
        sha256_payload(payload)


def test_tuple_and_list_have_same_canonical_json_semantics() -> None:
    assert sha256_payload(("one", 2, True)) == sha256_payload(["one", 2, True])


def test_non_string_mapping_key_cannot_collide_with_string_key() -> None:
    with pytest.raises(TypeError, match="字符串"):
        sha256_payload({1: "x"})

    assert len(sha256_payload({"1": "x"})) == 64


def test_build_evidence_ref_contains_only_identity_kind_and_hash() -> None:
    reference = build_evidence_ref(
        "review-1",
        "internal_review_passed",
        {"token": "do-not-copy-to-ref", "result": "passed"},
    )

    assert set(reference.model_dump()) == {"evidence_id", "kind", "sha256"}
    assert "do-not-copy-to-ref" not in reference.model_dump_json()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("evidence_id", ""),
        ("evidence_id", "UPPER"),
        ("kind", ""),
        ("kind", "contains space"),
        ("sha256", "A" * 64),
        ("sha256", "a" * 63),
        ("sha256", "g" * 64),
    ],
)
def test_evidence_ref_rejects_invalid_fields(field: str, value: str) -> None:
    data = valid_evidence_data()
    data[field] = value

    with pytest.raises(ValidationError):
        EvidenceRef.model_validate(data)


def test_passed_stage_requires_evidence_at_runtime_and_schema() -> None:
    data = valid_stage_data()
    data["evidence"] = []

    with pytest.raises(ValidationError):
        StageResult.model_validate(data)
    with pytest.raises(JSONSchemaValidationError):
        stage_schema_validator().validate(data)


@pytest.mark.parametrize("status", ["failed", "blocked"])
def test_non_passed_stage_may_have_no_evidence_at_runtime_and_schema(
    status: str,
) -> None:
    data = valid_stage_data()
    data["status"] = status
    data["evidence"] = []

    result = StageResult.model_validate(data)
    stage_schema_validator().validate(result.model_dump(mode="json"))

    assert result.evidence == ()


@pytest.mark.parametrize("field", ["summary", "next_action"])
def test_stage_result_rejects_blank_required_text(field: str) -> None:
    data = valid_stage_data()
    data[field] = "  "

    with pytest.raises(ValidationError):
        StageResult.model_validate(data)
    with pytest.raises(JSONSchemaValidationError):
        stage_schema_validator().validate(data)


def test_stage_result_is_deeply_immutable_and_forbids_extras() -> None:
    result = StageResult.model_validate(valid_stage_data())

    assert isinstance(result.evidence, tuple)
    assert isinstance(result.findings, tuple)
    with pytest.raises(ValidationError):
        result.status = "failed"
    with pytest.raises(ValidationError):
        result.evidence[0].kind = "changed"
    with pytest.raises(ValidationError):
        result.findings[0].title = "changed"
    with pytest.raises(ValidationError):
        StageResult.model_validate({**valid_stage_data(), "extra": True})
    with pytest.raises(ValidationError):
        Finding.model_validate(
            {
                "severity": "minor",
                "title": "提示",
                "evidence": "证据",
                "actionable": False,
                "extra": True,
            }
        )


def test_stage_result_schema_is_valid_and_matches_committed_file() -> None:
    schema = StageResult.model_json_schema()
    Draft202012Validator.check_schema(schema)
    schema_path = Path(__file__).parents[2] / "schemas" / "stage-result.schema.json"

    assert json.loads(schema_path.read_text(encoding="utf-8")) == schema


def test_codex_stage_result_schema_avoids_unsupported_allof() -> None:
    """The Codex response-format API rejects schemas containing ``allOf``."""

    schema = json.loads(stage_result_schema_json())

    Draft202012Validator.check_schema(schema)
    assert "allOf" not in schema
    Draft202012Validator(schema).validate(
        StageResult.model_validate(valid_stage_data()).model_dump(mode="json")
    )


def test_schema_and_runtime_accept_and_reject_same_examples() -> None:
    accepted = valid_stage_data()
    runtime = StageResult.model_validate(accepted)
    stage_schema_validator().validate(runtime.model_dump(mode="json"))

    rejected = [
        {**valid_stage_data(), "status": "unknown"},
        {**valid_stage_data(), "summary": ""},
        {**valid_stage_data(), "unexpected": True},
        {
            **valid_stage_data(),
            "evidence": [{**valid_evidence_data(), "sha256": "A" * 64}],
        },
    ]
    for data in rejected:
        with pytest.raises(ValidationError):
            StageResult.model_validate(data)
        with pytest.raises(JSONSchemaValidationError):
            stage_schema_validator().validate(data)


@pytest.mark.parametrize("severity", ["critical", "important"])
def test_passed_stage_rejects_actionable_serious_finding_in_runtime_and_schema(
    severity: str,
) -> None:
    data = valid_stage_data()
    data["findings"] = [
        {
            "severity": severity,
            "title": "必须处理",
            "evidence": "审查证据",
            "actionable": True,
        }
    ]

    with pytest.raises(ValidationError):
        StageResult.model_validate(data)
    with pytest.raises(JSONSchemaValidationError):
        stage_schema_validator().validate(data)


def test_passed_stage_allows_actionable_minor_finding() -> None:
    data = valid_stage_data()
    data["findings"] = [
        {
            "severity": "minor",
            "title": "后续优化",
            "evidence": "审查证据",
            "actionable": True,
        }
    ]

    result = StageResult.model_validate(data)
    stage_schema_validator().validate(result.model_dump(mode="json"))


def test_failed_stage_allows_actionable_critical_finding() -> None:
    data = valid_stage_data()
    data["status"] = "failed"
    data["findings"] = [
        {
            "severity": "critical",
            "title": "阻断问题",
            "evidence": "审查证据",
            "actionable": True,
        }
    ]

    result = StageResult.model_validate(data)
    stage_schema_validator().validate(result.model_dump(mode="json"))
