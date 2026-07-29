from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def _skill(name: str) -> tuple[str, dict[str, object]]:
    skill_root = ROOT / "skills" / name
    body = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    metadata = yaml.safe_load(
        (skill_root / "agents" / "openai.yaml").read_text(encoding="utf-8")
    )
    return body, metadata


def test_change_delivery_is_explicit_and_enforces_public_entry_gates() -> None:
    body, metadata = _skill("change-delivery")

    assert metadata["policy"]["allow_implicit_invocation"] is False
    assert "$change-delivery" in metadata["interface"]["default_prompt"]
    for required in (
        "doctor --json",
        "dry-run",
        "approval",
        "state machine",
        "READY_TO_MERGE",
        "Do not merge",
        "Do not write the state database",
        "--force-with-lease",
        "production",
        "prepare-approval <run-id> --json",
        "approve <run-id> --approval-file <absolute-approval-file> --confirm-hash <approval_sha256> --json",
        "resume <run-id> --json",
        "status --run-id <run-id> --json",
    ):
        assert required in body


def test_delivery_implement_is_single_writer_tdd_and_worktree_scoped() -> None:
    body, metadata = _skill("delivery-implement")

    assert metadata["policy"]["allow_implicit_invocation"] is False
    assert "$delivery-implement" in metadata["interface"]["default_prompt"]
    for required in (
        "single writer",
        "current worktree",
        "test fail",
        "minimum implementation",
        "StageResult",
        "Do not commit",
        "Do not push",
        "complete authoritative",
        "plain-text",
    ):
        assert required in body


def test_delivery_review_is_read_only_and_returns_stage_result() -> None:
    body, metadata = _skill("delivery-review")

    assert metadata["policy"]["allow_implicit_invocation"] is False
    assert "$delivery-review" in metadata["interface"]["default_prompt"]
    for required in (
        "read-only",
        "exact pending tree",
        "approved requirement",
        "critical",
        "important",
        "StageResult",
        "status, summary, evidence, findings, next_action",
        "Do not invent evidence",
        "Do not modify",
        "Do not mark",
        "complete authoritative",
        "plain-text",
    ):
        assert required in body


def test_project_acceptance_binds_sha_and_scopes_temporary_data() -> None:
    body, metadata = _skill("project-test-acceptance")
    checklist = (
        ROOT
        / "skills"
        / "project-test-acceptance"
        / "references"
        / "checklist.md"
    ).read_text(encoding="utf-8")

    assert metadata["policy"]["allow_implicit_invocation"] is False
    assert "$project-test-acceptance" in metadata["interface"]["default_prompt"]
    for required in (
        "references/checklist.md",
        "expected SHA",
        "ENVIRONMENT_PREEMPTED",
        "public API",
        "exact resource IDs",
        "Do not write the database",
        "StageResult",
        "status=blocked",
        "complete authoritative",
        "plain-text",
    ):
        assert required in body
    for required in (
        "remote test branch",
        "version endpoint",
        "created resource IDs",
        "exact run prefix",
        "direct database writes",
        "real email",
        "third-party",
        "cleanup",
    ):
        assert required in checklist


def test_production_release_is_preflight_only_and_refuses_writes() -> None:
    body, metadata = _skill("production-release")

    assert metadata["policy"]["allow_implicit_invocation"] is False
    assert "$production-release" in metadata["interface"]["default_prompt"]
    for required in (
        "preflight",
        "release <run-id> --json",
        "read-only",
        "always refuse",
        "Do not deploy",
        "Do not run smoke",
        "Do not mark",
        "written authorization",
    ):
        assert required in body
