from enum import StrEnum
from pathlib import Path
import re
from typing import Self

from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


BRANCH_PATTERN = (
    r"^[^-./\x00-\x20\x7f~^:?*\[\\]"
    r"(?:[^\x00-\x20\x7f~^:?*\[\\]*"
    r"[^/.\x00-\x20\x7f~^:?*\[\\])?$"
)
WINDOWS_ABSOLUTE_PATH_PATTERN = (
    r"^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+)"
)


def executable_basename(executable: str) -> str:
    basename = re.split(r"[\\/]", executable)[-1].casefold()
    return basename[:-4] if basename.endswith(".exe") else basename


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
    workdir: Path = Field(
        json_schema_extra={"pattern": WINDOWS_ABSOLUTE_PATH_PATTERN}
    )
    timeout_seconds: int = Field(default=300, ge=1, le=7200)
    env_names: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("executable")
    @classmethod
    def reject_shell_or_script_executable(cls, executable: str) -> str:
        basename = re.split(r"[\\/]", executable)[-1].casefold()
        executable_name = executable_basename(executable)
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
    def reject_multiline_arguments(
        cls,
        args: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any("\r" in argument or "\n" in argument for argument in args):
            raise ValueError("参数中禁止换行")
        return args

    @field_validator("workdir")
    @classmethod
    def require_absolute_workdir(cls, workdir: Path) -> Path:
        if not workdir.is_absolute():
            raise ValueError("命令工作目录必须是绝对路径")
        return workdir


class ProjectConfig(FrozenModel):

    project_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    repo_path: Path = Field(
        json_schema_extra={"pattern": WINDOWS_ABSOLUTE_PATH_PATTERN}
    )
    remote: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    base_branch: str = Field(pattern=BRANCH_PATTERN)
    test_branch: str = Field(pattern=BRANCH_PATTERN)
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
            ".." in branch
            or "@{" in branch
            or "//" in branch
            or branch.endswith(".lock")
        ):
            raise ValueError("分支名称无效")
        return branch

    @field_validator("version_endpoint", mode="before")
    @classmethod
    def normalize_https_scheme(cls, endpoint: object) -> object:
        if isinstance(endpoint, str) and re.match(r"(?i)^https://", endpoint):
            return f"https://{endpoint[8:]}"
        return endpoint

    @field_validator("version_endpoint")
    @classmethod
    def require_https_version_endpoint(cls, endpoint: str) -> str:
        parsed = AnyUrl(endpoint)
        if parsed.scheme.casefold() != "https" or not parsed.host:
            raise ValueError("版本接口必须是 HTTPS URL")
        return endpoint

    @model_validator(mode="after")
    def require_validation_profile_checks(self) -> Self:
        direct_tools = {"pytest", "ruff", "mypy"}
        python_tools = {"python", "python3", "py"}
        python_modules = {"pytest", "ruff", "mypy", "compileall"}
        for check in self.required_pre_pr_checks:
            executable = executable_basename(check.executable)
            is_direct_tool = executable in direct_tools
            is_python_module = (
                executable in python_tools
                and len(check.args) >= 2
                and check.args[0] == "-m"
                and check.args[1] in python_modules
            )
            if not is_direct_tool and not is_python_module:
                raise ValueError("项目预检命令不属于第一版验证工具白名单")
        return self


class LoadedProjectConfig(FrozenModel):
    config: ProjectConfig
    config_hash: str
    source: Path
