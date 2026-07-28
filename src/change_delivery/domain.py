from enum import StrEnum
from pathlib import Path
import re

from pydantic import AnyUrl, BaseModel, ConfigDict, Field, field_validator


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


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CommandSpec(FrozenModel):

    executable: str = Field(min_length=1)
    args: tuple[str, ...] = Field(default_factory=tuple)
    workdir: Path
    timeout_seconds: int = Field(default=300, ge=1, le=7200)
    env_names: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("executable")
    @classmethod
    def reject_shell_or_script_executable(cls, executable: str) -> str:
        basename = re.split(r"[\\/]", executable)[-1].casefold()
        executable_name = (
            basename[:-4] if basename.endswith(".exe") else basename
        )
        forbidden_names = {
            "powershell",
            "pwsh",
            "cmd",
            "bash",
            "sh",
            "wsl",
            "rm",
            "remove-item",
            "rmdir",
        }
        forbidden_suffixes = (".ps1", ".bat", ".cmd", ".sh")
        if (
            executable_name in forbidden_names
            or basename.endswith(forbidden_suffixes)
        ):
            raise ValueError("禁止 Shell、破坏性程序或脚本文件")
        return executable

    @field_validator("args")
    @classmethod
    def reject_shell_invocation_arguments(
        cls,
        args: tuple[str, ...],
    ) -> tuple[str, ...]:
        normalized = tuple(argument.strip().casefold() for argument in args)
        has_newline = any(
            "\r" in argument or "\n" in argument for argument in args
        )
        has_invoke_expression = any(
            re.match(r"^invoke-expression(?:\s|$)", argument)
            for argument in normalized
        )
        has_split_cmd = any(
            command in {"cmd", "cmd.exe"} and option == "/c"
            for command, option in zip(normalized, normalized[1:])
        )
        has_inline_cmd = any(
            re.search(r"(?i)(?:^|\s)cmd(?:\.exe)?\s+/c(?:\s|$)", argument)
            for argument in args
        )
        joined = " ".join(args)
        has_dangerous_command = re.search(
            r"(?i)(?:;\s+rm|&&\s+rm|\|\s+remove-item)(?:\s|$)",
            joined,
        )
        if (
            has_newline
            or has_invoke_expression
            or has_split_cmd
            or has_inline_cmd
            or has_dangerous_command
        ):
            raise ValueError("参数中禁止换行或 Shell 调用组合")
        return args

    @field_validator("workdir")
    @classmethod
    def require_absolute_workdir(cls, workdir: Path) -> Path:
        if not workdir.is_absolute():
            raise ValueError("命令工作目录必须是绝对路径")
        return workdir


class ProjectConfig(FrozenModel):

    project_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    repo_path: Path
    remote: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    base_branch: str = Field(min_length=1)
    test_branch: str = Field(min_length=1)
    github_repository: str = Field(pattern=r"^[^/]+/[^/]+$")
    version_endpoint: str = Field(
        pattern=r"^https://[^/]",
        json_schema_extra={"format": "uri"},
    )
    version_sha_field: str = Field(
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
    )
    required_pre_pr_checks: tuple[CommandSpec, ...] = Field(min_length=1)
    credentials: tuple[str, ...] = Field(default_factory=tuple)
    production_enabled: bool = False

    @field_validator("repo_path")
    @classmethod
    def require_absolute_repo_path(cls, repo_path: Path) -> Path:
        if not repo_path.is_absolute():
            raise ValueError("仓库目录必须是绝对路径")
        return repo_path

    @field_validator("base_branch", "test_branch")
    @classmethod
    def reject_invalid_branch_names(cls, branch: str) -> str:
        if (
            branch.startswith("-")
            or " " in branch
            or ".." in branch
            or "@{" in branch
            or "\\" in branch
            or any(ord(character) < 32 or ord(character) == 127 for character in branch)
        ):
            raise ValueError("分支名称无效")
        return branch

    @field_validator("version_endpoint")
    @classmethod
    def require_https_version_endpoint(cls, endpoint: str) -> str:
        parsed = AnyUrl(endpoint)
        if parsed.scheme.casefold() != "https" or not parsed.host:
            raise ValueError("版本接口必须是 HTTPS URL")
        return endpoint


class LoadedProjectConfig(FrozenModel):
    config: ProjectConfig
    config_hash: str
    source: Path
