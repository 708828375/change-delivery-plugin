from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError
from pydantic import ValidationError

from change_delivery.domain import EvidenceRef, Finding, StageResult
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


def test_bytes_hash_is_deterministic() -> None:
    assert sha256_payload(b"same") == sha256_payload(bytearray(b"same"))


def test_file_hash_reads_contents_without_exposing_path(tmp_path: Path) -> None:
    evidence_file = tmp_path / "secret-name.log"
    evidence_file.write_bytes(b"stable contents")

    reference = build_evidence_ref("acceptance-1", "acceptance_passed", evidence_file)

    assert reference.sha256 == sha256_payload(b"stable contents")
    assert str(evidence_file) not in reference.model_dump_json()


@pytest.mark.parametrize("payload", [{"value": object()}, {"value": math.nan}])
def test_hash_rejects_non_canonical_json(payload: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        sha256_payload(payload)


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
