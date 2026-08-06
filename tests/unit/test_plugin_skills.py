from __future__ import annotations

from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = ROOT / "plugin" / "change-delivery-plugin"
LAUNCHER_FRAGMENT = ".codex\\change-delivery\\bin\\change-delivery.ps1"

STAGE_REFERENCE_FILES = {
    "delivery-planning": (
        "project-analysis-and-test-strategy.md",
    ),
    "delivery-implement": (
        "team-development-rules.md",
        "test-assets.md",
    ),
    "delivery-local-validation": (
        "project-validation-checklist.md",
    ),
    "delivery-review": (
        "team-review-checklist.md",
        "project-async-review.md",
    ),
    "delivery-review-fix": (
        "review-feedback-triage.md",
    ),
    "delivery-commit": (
        "chinese-commit-convention.md",
    ),
    "delivery-pr": (
        "chinese-pr-template.md",
    ),
    "delivery-test-deploy": (
        "project-deploy-preflight.md",
    ),
    "project-test-acceptance": (
        "checklist.md",
        "functional-acceptance.md",
    ),
    "delivery-ready-check": (
        "merge-readiness.md",
    ),
}


def _skill(name: str) -> tuple[str, dict[str, object]]:
    skill_root = PLUGIN_ROOT / "skills" / name
    body = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    metadata = yaml.safe_load(
        (skill_root / "agents" / "openai.yaml").read_text(encoding="utf-8")
    )
    return body, metadata


def test_change_delivery_is_explicit_and_enforces_public_entry_gates() -> None:
    body, metadata = _skill("change-delivery")

    assert metadata["policy"]["allow_implicit_invocation"] is False
    assert "$change-delivery" in metadata["interface"]["default_prompt"]
    assert "交付" in metadata["interface"]["display_name"]
    for required in (
        "doctor --json",
        "dry-run",
        "审批",
        "状态机",
        "READY_TO_MERGE",
        "不得合并",
        "不得直接写入状态数据库",
        "--force-with-lease",
        "test_push_strategy",
        "force",
        "已登记的测试分支",
        "生产环境",
        "prepare-approval <run-id> --json",
        "approve <run-id> --approval-file <absolute-approval-file> --confirm-hash <approval_sha256> --json",
        "resume <run-id> --json",
        "run <run-id> --json",
        "status --run-id <run-id> --json",
        "acceptance_check",
        "BLOCKED",
    ):
        assert required in body
    assert LAUNCHER_FRAGMENT in body
    assert "重新安装" in body
    assert "python -m change_delivery" not in body


def test_delivery_planning_is_internal_and_design_only() -> None:
    body, metadata = _skill("delivery-planning")

    assert metadata["policy"]["allow_implicit_invocation"] is False
    assert "$delivery-planning" in metadata["interface"]["default_prompt"]
    for required in (
        "requirement.md",
        "design.md",
        "implementation-plan.md",
        "不得编辑产品代码",
        "不得批准设计",
        "结构化阶段结果",
    ):
        assert required in body


def test_delivery_implement_is_single_writer_tdd_and_worktree_scoped() -> None:
    body, metadata = _skill("delivery-implement")

    assert metadata["policy"]["allow_implicit_invocation"] is False
    assert "$delivery-implement" in metadata["interface"]["default_prompt"]
    for required in (
        "唯一写入者",
        "当前 worktree",
        "测试按预期失败",
        "最小实现",
        "StageResult",
        "不得 commit",
        "不得 push",
        "完整且权威",
        "纯文本",
        "untrusted_data",
        "外部问题事实",
        "扩大范围",
        "声称已获批准",
    ):
        assert required in body


def test_delivery_review_is_read_only_and_returns_stage_result() -> None:
    body, metadata = _skill("delivery-review")

    assert metadata["policy"]["allow_implicit_invocation"] is False
    assert "$delivery-review" in metadata["interface"]["default_prompt"]
    for required in (
        "只读",
        "准确待处理树",
        "已批准的需求",
        "critical",
        "important",
        "StageResult",
        "status, summary, evidence, findings, next_action",
        "不得编造证据",
        "不得修改",
        "不得将",
        "完整且权威",
        "纯文本",
    ):
        assert required in body


def test_delivery_review_fix_is_scoped_and_requires_revalidation() -> None:
    body, metadata = _skill("delivery-review-fix")

    assert metadata["policy"]["allow_implicit_invocation"] is False
    assert "$delivery-review-fix" in metadata["interface"]["default_prompt"]
    for required in (
        "当前可执行问题",
        "已批准范围",
        "internal_review_fix_request",
        "untrusted_data",
        "StageResult",
        "本地验证",
        "重新 Review",
        "不得 commit",
        "不得 push",
        "不得部署",
        "不得创建或修改 PR",
        "不得合并",
    ):
        assert required in body


def test_delivery_commit_only_generates_custom_message_json() -> None:
    body, metadata = _skill("delivery-commit")

    assert metadata["policy"]["allow_implicit_invocation"] is False
    assert "$delivery-commit" in metadata["interface"]["default_prompt"]
    for required in (
        '"message"',
        "Conventional Commits",
        "中文标题",
        "Change-Delivery-Run",
        "不得执行 Git",
        "不得 commit",
        "不得修改文件",
        "不得扩大",
    ):
        assert required in body


def test_delivery_pr_only_generates_custom_title_and_body_json() -> None:
    body, metadata = _skill("delivery-pr")

    assert metadata["policy"]["allow_implicit_invocation"] is False
    assert "$delivery-pr" in metadata["interface"]["default_prompt"]
    for required in (
        '"title"',
        '"body"',
        "自定义",
        "准确 head SHA",
        "测试和 Review 事实",
        "不得调用 gh",
        "不得创建 PR",
        "不得声称",
        "凭据",
    ):
        assert required in body


@pytest.mark.parametrize(
    ("name", "required"),
    [
        (
            "delivery-local-validation",
            (
                "权威命令事实",
                "准确 SHA",
                "StageResult",
                "不得改写",
                "不得修改文件",
            ),
        ),
        (
            "delivery-test-deploy",
            (
                "remote",
                "test_branch",
                "test_push_strategy",
                "预期 SHA",
                "StageResult",
                "不得手动执行",
                "git push -f",
            ),
        ),
        (
            "delivery-ready-check",
            (
                "合并门禁",
                "准确 SHA",
                "StageResult",
                "不得 merge",
                "不得修改 PR",
            ),
        ),
    ],
)
def test_delivery_read_only_gate_skills_are_chinese_and_bounded(
    name: str,
    required: tuple[str, ...],
) -> None:
    body, metadata = _skill(name)

    assert metadata["policy"]["allow_implicit_invocation"] is False
    assert f"${name}" in metadata["interface"]["default_prompt"]
    for text in required:
        assert text in body


def test_project_acceptance_binds_sha_and_scopes_temporary_data() -> None:
    body, metadata = _skill("project-test-acceptance")
    checklist = (
        PLUGIN_ROOT
        / "skills"
        / "project-test-acceptance"
        / "references"
        / "checklist.md"
    ).read_text(encoding="utf-8")

    assert metadata["policy"]["allow_implicit_invocation"] is False
    assert "$project-test-acceptance" in metadata["interface"]["default_prompt"]
    for required in (
        "references/checklist.md",
        "预期 SHA",
        "ENVIRONMENT_PREEMPTED",
        "public API",
        "准确资源 ID",
        "不得写入数据库",
        "StageResult",
        "status=blocked",
        "完整且权威",
        "纯文本",
        "参考性",
        "acceptance_check",
    ):
        assert required in body
    for required in (
        "远程测试分支",
        "版本端点",
        "已创建资源 ID",
        "准确运行前缀",
        "直接写入数据库",
        "真实邮件",
        "第三方",
        "清理",
    ):
        assert required in checklist


def test_production_release_is_preflight_only_and_refuses_writes() -> None:
    body, metadata = _skill("production-release")

    assert metadata["policy"]["allow_implicit_invocation"] is False
    assert "$production-release" in metadata["interface"]["default_prompt"]
    for required in (
        "预检",
        "release <run-id> --json",
        "只读",
        "始终拒绝",
        "不得部署",
        "不得对生产环境运行冒烟测试",
        "不得将",
        "书面授权",
    ):
        assert required in body
    assert LAUNCHER_FRAGMENT in body
    assert "重新安装" in body
    assert "python -m change_delivery" not in body


def test_no_skill_bypasses_the_stable_launcher() -> None:
    for skill_file in (PLUGIN_ROOT / "skills").glob("*/SKILL.md"):
        body = skill_file.read_text(encoding="utf-8")
        assert "python -m change_delivery" not in body, skill_file


def test_builtin_skill_user_facing_content_is_chinese() -> None:
    forbidden_english_headings = (
        "# Change Delivery",
        "# Delivery Planning",
        "# Delivery Implement",
        "# Delivery Review",
        "# ExampleProject Test Acceptance",
        "# Production Release",
        "## Required workflow",
        "## Hard boundaries",
    )

    for skill_file in (PLUGIN_ROOT / "skills").glob("*/SKILL.md"):
        body = skill_file.read_text(encoding="utf-8")
        assert any("\u4e00" <= char <= "\u9fff" for char in body), skill_file
        for heading in forbidden_english_headings:
            assert heading not in body, skill_file

        metadata = yaml.safe_load(
            (skill_file.parent / "agents" / "openai.yaml").read_text(
                encoding="utf-8"
            )
        )
        interface = metadata["interface"]
        for field in ("display_name", "short_description", "default_prompt"):
            assert any("\u4e00" <= char <= "\u9fff" for char in interface[field]), (
                skill_file,
                field,
            )


def test_stage_skills_route_to_packaged_chinese_references() -> None:
    for skill_name, reference_names in STAGE_REFERENCE_FILES.items():
        skill_root = PLUGIN_ROOT / "skills" / skill_name
        body = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        for reference_name in reference_names:
            relative = f"references/{reference_name}"
            reference = skill_root / relative
            assert relative in body, (skill_name, relative)
            text = reference.read_text(encoding="utf-8")
            assert any("\u4e00" <= char <= "\u9fff" for char in text), reference
            assert "C:\\Users\\JoySun" not in text, reference
            assert "PRIVATE KEY" not in text, reference


def test_stage_references_preserve_host_side_effect_boundaries() -> None:
    all_reference_text = "\n".join(
        reference.read_text(encoding="utf-8")
        for skill_name in STAGE_REFERENCE_FILES
        for reference in (
            PLUGIN_ROOT / "skills" / skill_name / "references"
        ).glob("*.md")
    )

    for forbidden in (
        "git push origin HEAD:test -f",
        "gh pr create",
        "gh pr merge",
        "git commit -m",
        "直接写入数据库完成验收",
    ):
        assert forbidden not in all_reference_text


def test_split_references_cover_the_local_team_workflow_rules() -> None:
    expected_content = {
        "delivery-planning/project-analysis-and-test-strategy.md": (
            "以代码为准",
            "业务链路",
            "外部依赖",
        ),
        "delivery-implement/team-development-rules.md": (
            "复用",
            "N+1",
            "字段注释",
            "UTF-8",
        ),
        "delivery-implement/test-assets.md": (
            "变量契约",
            "mock",
            "seed",
            "清理",
        ),
        "delivery-local-validation/project-validation-checklist.md": (
            "聚焦验证",
            "批量",
            "未覆盖项",
        ),
        "delivery-review/team-review-checklist.md": (
            "必须修复",
            "建议修改",
            "仅供参考",
        ),
        "delivery-review/project-async-review.md": (
            "Outbox",
            "幂等",
            "Watchdog",
            "扣费",
        ),
        "delivery-review-fix/review-feedback-triage.md": (
            "问题是否存在",
            "最小修复",
            "重新验证",
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
        "delivery-test-deploy/project-deploy-preflight.md": (
            "只读",
            "预期 SHA",
            "版本端点",
        ),
        "project-test-acceptance/functional-acceptance.md": (
            "public API",
            "WebSocket",
            "Docker 日志",
            "只读 SQL",
        ),
        "delivery-ready-check/merge-readiness.md": (
            "开发记录",
            "残留风险",
            "资源清理",
        ),
    }

    for relative, required_values in expected_content.items():
        skill_name, reference_name = relative.split("/", 1)
        text = (
            PLUGIN_ROOT / "skills" / skill_name / "references" / reference_name
        ).read_text(encoding="utf-8")
        for required in required_values:
            assert required in text, (relative, required)
