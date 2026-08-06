from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = ROOT / "plugin" / "change-delivery-plugin"
LAUNCHER_FRAGMENT = ".codex\\change-delivery\\bin\\change-delivery.ps1"

EXPECTED_SKILLS = {
    "change-delivery",
    "delivery-commit",
    "delivery-implement",
    "delivery-local-validation",
    "delivery-planning",
    "delivery-pr",
    "delivery-ready-check",
    "delivery-review",
    "delivery-review-fix",
    "delivery-test-deploy",
    "project-test-acceptance",
    "production-release",
}

STAGE_REFERENCE_FILES = {
    "delivery-planning": ("project-analysis-and-test-strategy.md",),
    "delivery-implement": ("team-development-rules.md", "test-assets.md"),
    "delivery-local-validation": ("project-validation-checklist.md",),
    "delivery-review": ("team-review-checklist.md", "project-async-review.md"),
    "delivery-review-fix": ("review-feedback-triage.md",),
    "delivery-commit": ("chinese-commit-convention.md",),
    "delivery-pr": ("chinese-pr-template.md",),
    "delivery-test-deploy": ("project-deploy-preflight.md",),
    "project-test-acceptance": ("checklist.md", "functional-acceptance.md"),
    "delivery-ready-check": ("merge-readiness.md",),
}

SKILL_CHARACTER_BUDGETS = {
    "change-delivery": 1700,
    "delivery-commit": 650,
    "delivery-implement": 900,
    "delivery-local-validation": 550,
    "delivery-planning": 600,
    "delivery-pr": 650,
    "delivery-ready-check": 550,
    "delivery-review": 850,
    "delivery-review-fix": 750,
    "delivery-test-deploy": 600,
    "project-test-acceptance": 1000,
    "production-release": 750,
}


def _skill(name: str) -> tuple[str, dict[str, object]]:
    skill_root = PLUGIN_ROOT / "skills" / name
    body = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    metadata = yaml.safe_load(
        (skill_root / "agents" / "openai.yaml").read_text(encoding="utf-8")
    )
    return body, metadata


def test_plugin_exposes_only_the_expected_explicit_chinese_skills() -> None:
    skill_roots = {
        path.name
        for path in (PLUGIN_ROOT / "skills").iterdir()
        if (path / "SKILL.md").is_file()
    }
    assert skill_roots == EXPECTED_SKILLS

    for name in EXPECTED_SKILLS:
        body, metadata = _skill(name)
        assert metadata["policy"]["allow_implicit_invocation"] is False
        assert f"${name}" in metadata["interface"]["default_prompt"]
        assert any("\u4e00" <= char <= "\u9fff" for char in body), name
        for field in ("display_name", "short_description", "default_prompt"):
            value = metadata["interface"][field]
            assert any("\u4e00" <= char <= "\u9fff" for char in value), (name, field)


def test_public_entry_skill_preserves_cli_approval_and_stop_contract() -> None:
    body, _ = _skill("change-delivery")
    for required in (
        LAUNCHER_FRAGMENT,
        "doctor --json",
        "start --project",
        "prepare-approval",
        "approval_sha256",
        "approve <run-id>",
        "run <run-id> --json",
        "status --run-id",
        "READY_TO_MERGE",
        "状态机",
        "测试分支",
        "生产",
    ):
        assert required in body
    assert "python -m change_delivery" not in body


def test_internal_stage_skills_define_outcome_evidence_and_authority() -> None:
    required_by_skill = {
        "delivery-planning": (
            "design.md",
            "implementation-plan.md",
            "完成判据",
            "显式批准",
        ),
        "delivery-implement": (
            "唯一写入者",
            "当前 worktree",
            "行为变更",
            "回归证据",
            "退出判据",
        ),
        "delivery-local-validation": (
            "权威命令事实",
            "当前 SHA",
            "退出判据",
            "只读",
        ),
        "delivery-review": (
            "准确树",
            "先完整发现",
            "再分级",
            "critical",
            "important",
            "只读",
        ),
        "delivery-review-fix": (
            "问题是否存在",
            "最小修复",
            "重新验证",
            "重新 Review",
        ),
        "delivery-commit": (
            "准确事实",
            "chinese-commit-convention.md",
            "trailer",
        ),
        "delivery-pr": (
            "准确事实",
            "chinese-pr-template.md",
            "权威证据",
        ),
        "delivery-test-deploy": (
            "remote",
            "test_branch",
            "test_push_strategy",
            "预期 SHA",
            "只读",
        ),
        "project-test-acceptance": (
            "预期 SHA",
            "ENVIRONMENT_PREEMPTED",
            "资源台账",
            "清理",
            "退出判据",
        ),
        "delivery-ready-check": (
            "当前 SHA",
            "合并门禁",
            "证据矩阵",
            "READY_TO_MERGE",
        ),
    }

    for name, required_values in required_by_skill.items():
        body, _ = _skill(name)
        for required in required_values:
            assert required in body, (name, required)


def test_production_release_remains_read_only_preflight() -> None:
    body, _ = _skill("production-release")
    for required in (
        LAUNCHER_FRAGMENT,
        "release <run-id> --json",
        "只读预检",
        "生产写入",
        "退出判据",
    ):
        assert required in body
    assert "python -m change_delivery" not in body


def test_stage_references_are_packaged_chinese_and_explicitly_routed() -> None:
    for skill_name, reference_names in STAGE_REFERENCE_FILES.items():
        skill_root = PLUGIN_ROOT / "skills" / skill_name
        body = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        for reference_name in reference_names:
            relative = f"references/{reference_name}"
            reference = skill_root / relative
            assert relative in body, (skill_name, relative)
            text = reference.read_text(encoding="utf-8")
            assert any("\u4e00" <= char <= "\u9fff" for char in text), reference
            for forbidden in (
                "C:\\Users\\JoySun",
                "PRIVATE KEY",
                "git push origin HEAD:test -f",
                "gh pr create",
                "gh pr merge",
            ):
                assert forbidden not in text, (reference, forbidden)


def test_specialized_references_have_narrow_activation_conditions() -> None:
    implementation, _ = _skill("delivery-implement")
    review, _ = _skill("delivery-review")
    acceptance, _ = _skill("project-test-acceptance")

    assert "已批准计划包含" in implementation
    assert "test-assets.md" in implementation
    assert "变更涉及" in review
    assert "project-async-review.md" in review
    assert "未触发" in review
    assert "只读探针事实" in acceptance


def test_skills_stay_within_attention_budgets() -> None:
    total_characters = 0
    negative_instructions = 0
    for name, budget in SKILL_CHARACTER_BUDGETS.items():
        body, _ = _skill(name)
        total_characters += len(body)
        negative_instructions += sum(
            body.count(marker)
            for marker in ("不得", "不能", "禁止", "绝不", "不要")
        )
        assert len(body) <= budget, (name, len(body), budget)

    assert total_characters <= 9500
    assert negative_instructions <= 60


def test_runtime_schema_boilerplate_is_not_duplicated_in_skills() -> None:
    bodies = "\n".join(_skill(name)[0] for name in EXPECTED_SKILLS)
    assert bodies.count("完整且权威") <= 1
    assert bodies.count("不得猜测 JSON") == 0
    assert bodies.count("纯文本的 blocked") == 0
    assert bodies.count("status, summary, evidence, findings, next_action") == 0


def test_references_keep_team_contracts_instead_of_generic_tutorials() -> None:
    required_content = {
        "delivery-implement/team-development-rules.md": (
            "表注释",
            "字段注释",
            "中文",
            "开发记录",
        ),
        "delivery-review/team-review-checklist.md": (
            "必须修复",
            "建议修改",
            "仅供参考",
            "退出判据",
        ),
        "delivery-review/project-async-review.md": (
            "Outbox",
            "幂等",
            "Watchdog",
            "扣费",
        ),
        "delivery-commit/chinese-commit-convention.md": (
            "Conventional Commits",
            "变更背景",
            "取舍",
        ),
        "delivery-pr/chinese-pr-template.md": (
            "变更说明",
            "测试情况",
            "部署注意事项",
        ),
        "project-test-acceptance/checklist.md": (
            "远程测试分支",
            "版本端点",
            "资源 ID",
            "清理",
        ),
    }
    for relative, required_values in required_content.items():
        skill_name, reference_name = relative.split("/", 1)
        text = (
            PLUGIN_ROOT / "skills" / skill_name / "references" / reference_name
        ).read_text(encoding="utf-8")
        for required in required_values:
            assert required in text, (relative, required)


def test_no_skill_bypasses_the_stable_launcher() -> None:
    for skill_file in (PLUGIN_ROOT / "skills").glob("*/SKILL.md"):
        body = skill_file.read_text(encoding="utf-8")
        assert "python -m change_delivery" not in body, skill_file
