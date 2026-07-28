from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class RunState(StrEnum):
    DRAFT = "DRAFT"
    AWAITING_DESIGN_APPROVAL = "AWAITING_DESIGN_APPROVAL"
    APPROVED = "APPROVED"
    PREPARING_WORKTREE = "PREPARING_WORKTREE"
    IMPLEMENTING = "IMPLEMENTING"
    LOCAL_VALIDATION = "LOCAL_VALIDATION"
    INTERNAL_REVIEW = "INTERNAL_REVIEW"
    COMMITTING = "COMMITTING"
    PUSHING_FEATURE = "PUSHING_FEATURE"
    PR_DRAFT = "PR_DRAFT"
    DEPLOYING_TEST = "DEPLOYING_TEST"
    ACCEPTANCE_TESTING = "ACCEPTANCE_TESTING"
    PR_READY_FOR_REVIEW = "PR_READY_FOR_REVIEW"
    MONITORING_CI_REVIEW = "MONITORING_CI_REVIEW"
    FIXING_CI_REVIEW = "FIXING_CI_REVIEW"
    READY_TO_MERGE = "READY_TO_MERGE"
    MERGED = "MERGED"
    CLEANING_LOCAL = "CLEANING_LOCAL"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    ENVIRONMENT_PREEMPTED = "ENVIRONMENT_PREEMPTED"
    REVIEW_DECISION_REQUIRED = "REVIEW_DECISION_REQUIRED"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    CANCELLED = "CANCELLED"


class CommandSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    executable: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    workdir: Path
    timeout_seconds: int = Field(default=300, ge=1, le=7200)
    env_names: list[str] = Field(default_factory=list)

    @field_validator("args")
    @classmethod
    def reject_shell_programs(
        cls,
        args: list[str],
        info: ValidationInfo,
    ) -> list[str]:
        executable = info.data.get("executable", "")
        command = " ".join((executable, *args)).lower()
        forbidden = (
            "invoke-expression",
            "cmd /c",
            "; rm ",
            "&& rm ",
            "| remove-item",
        )
        if any(token in command for token in forbidden):
            raise ValueError("禁止任意 Shell 或破坏性命令")
        return args


class ProjectConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    repo_path: Path
    remote: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    base_branch: str
    test_branch: str
    github_repository: str = Field(pattern=r"^[^/]+/[^/]+$")
    version_endpoint: str
    version_sha_field: str
    required_pre_pr_checks: list[CommandSpec] = Field(min_length=1)
    credentials: list[str] = Field(default_factory=list)
    production_enabled: bool = False


class LoadedProjectConfig(BaseModel):
    config: ProjectConfig
    config_hash: str
    source: Path
