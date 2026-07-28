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
    r"(?:[^\x00-\x20\x7f~^:?*\[\\/]"
    r"|/[^.\x00-\x20\x7f~^:?*\[\\/])*$"
)
WINDOWS_ABSOLUTE_PATH_PATTERN = (
    r"^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+)"
)
HTTPS_URL_PATTERN = r"^[Hh][Tt][Tt][Pp][Ss]://[^/]"
GITHUB_REPOSITORY_PATTERN = (
    r"^[^\s/\x00-\x1f\x7f]+/[^\s/\x00-\x1f\x7f]+$"
)
SHELL_OR_SCRIPT_EXECUTABLE_PATTERN = (
    r"(?:^|[\\/])(?:(?:"
    r"[Pp][Oo][Ww][Ee][Rr][Ss][Hh][Ee][Ll][Ll]|"
    r"[Pp][Ww][Ss][Hh]|[Cc][Mm][Dd]|[Bb][Aa][Ss][Hh]|[Ss][Hh]|"
    r"[Ww][Ss][Ll]|[Rr][Mm]|[Rr][Ee][Mm][Oo][Vv][Ee]-[Ii][Tt][Ee][Mm]|"
    r"[Rr][Mm][Dd][Ii][Rr])(?:\.[Ee][Xx][Ee])?|"
    r"[^\\/]+\.(?:[Pp][Ss]1|[Bb][Aa][Tt]|[Cc][Mm][Dd]|[Ss][Hh]))$"
)


def executable_schema_pattern(*names: str) -> str:
    alternatives = []
    for name in names:
        alternatives.append("".join(f"[{char.upper()}{char.lower()}]" for char in name))
    return (
        r"(?:^|[\\/])(?:"
        + "|".join(alternatives)
        + r")(?:\.[Ee][Xx][Ee])?$"
    )


PYTEST_EXECUTABLE_PATTERN = executable_schema_pattern("pytest")
RUFF_EXECUTABLE_PATTERN = executable_schema_pattern("ruff")
MYPY_EXECUTABLE_PATTERN = executable_schema_pattern("mypy")
PYTHON_EXECUTABLE_PATTERN = executable_schema_pattern("python", "python3", "py")
PYTEST_UNSAFE_ARGUMENT_PATTERN = r"^(?:--cache-clear|--basetemp(?:=.*)?)$"
RUFF_UNSAFE_ARGUMENT_PATTERN = (
    r"^--(?:fix|unsafe-fixes|add-noqa)(?:=.*)?$"
)
MYPY_UNSAFE_ARGUMENT_PATTERN = r"^--install-types(?:=.*)?$"


def safe_args_schema(
    *,
    prefix: tuple[str, ...] = (),
    unsafe_pattern: str | None = None,
) -> dict[str, object]:
    schema: dict[str, object] = {"type": "array"}
    if prefix:
        schema["prefixItems"] = [{"const": item} for item in prefix]
        schema["minItems"] = len(prefix)
    if unsafe_pattern:
        schema["not"] = {
            "contains": {"type": "string", "pattern": unsafe_pattern}
        }
    return schema


VALIDATION_PROFILE_SCHEMA = {
    "oneOf": [
        {
            "properties": {
                "executable": {"pattern": PYTEST_EXECUTABLE_PATTERN},
                "args": safe_args_schema(
                    unsafe_pattern=PYTEST_UNSAFE_ARGUMENT_PATTERN
                ),
            }
        },
        {
            "properties": {
                "executable": {"pattern": RUFF_EXECUTABLE_PATTERN},
                "args": safe_args_schema(
                    prefix=("check",),
                    unsafe_pattern=RUFF_UNSAFE_ARGUMENT_PATTERN,
                ),
            }
        },
        {
            "properties": {
                "executable": {"pattern": MYPY_EXECUTABLE_PATTERN},
                "args": safe_args_schema(
                    unsafe_pattern=MYPY_UNSAFE_ARGUMENT_PATTERN
                ),
            }
        },
        {
            "properties": {
                "executable": {"pattern": PYTHON_EXECUTABLE_PATTERN},
                "args": safe_args_schema(
                    prefix=("-m", "pytest"),
                    unsafe_pattern=PYTEST_UNSAFE_ARGUMENT_PATTERN,
                ),
            }
        },
        {
            "properties": {
                "executable": {"pattern": PYTHON_EXECUTABLE_PATTERN},
                "args": safe_args_schema(
                    prefix=("-m", "ruff", "check"),
                    unsafe_pattern=RUFF_UNSAFE_ARGUMENT_PATTERN,
                ),
            }
        },
        {
            "properties": {
                "executable": {"pattern": PYTHON_EXECUTABLE_PATTERN},
                "args": safe_args_schema(
                    prefix=("-m", "mypy"),
                    unsafe_pattern=MYPY_UNSAFE_ARGUMENT_PATTERN,
                ),
            }
        },
        {
            "properties": {
                "executable": {"pattern": PYTHON_EXECUTABLE_PATTERN},
                "args": safe_args_schema(prefix=("-m", "compileall")),
            }
        },
    ]
}


def add_project_config_schema_constraints(schema: dict[str, object]) -> None:
    properties = schema["properties"]
    assert isinstance(properties, dict)
    checks = properties["required_pre_pr_checks"]
    assert isinstance(checks, dict)
    command_spec_schema = checks["items"]
    checks["items"] = {
        "allOf": [
            command_spec_schema,
            VALIDATION_PROFILE_SCHEMA,
        ]
    }


BRANCH_SCHEMA_EXTRA = {
    "allOf": [
        {"not": {"const": "@"}},
        {"not": {"pattern": r"\.\."}},
        {"not": {"pattern": r"@\{"}},
        {"not": {"pattern": r"\.$"}},
        {"not": {"pattern": r"(?:^|/)[^/]*\.lock(?:/|$)"}},
    ]
}


def executable_basename(executable: str) -> str:
    basename = re.split(r"[\\/]", executable)[-1].casefold()
    return basename[:-4] if basename.endswith(".exe") else basename


def validation_arguments_are_safe(
    tool: str,
    arguments: tuple[str, ...],
) -> bool:
    if tool == "ruff":
        return (
            bool(arguments)
            and arguments[0] == "check"
            and not any(
                re.match(RUFF_UNSAFE_ARGUMENT_PATTERN, argument)
                for argument in arguments
            )
        )
    if tool == "mypy":
        return not any(
            re.match(MYPY_UNSAFE_ARGUMENT_PATTERN, argument)
            for argument in arguments
        )
    if tool == "pytest":
        return not any(
            re.match(PYTEST_UNSAFE_ARGUMENT_PATTERN, argument)
            for argument in arguments
        )
    # compileall writes __pycache__; the executor must run it in an isolated worktree.
    return tool == "compileall"


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

    executable: str = Field(
        min_length=1,
        json_schema_extra={
            "allOf": [
                {"not": {"pattern": SHELL_OR_SCRIPT_EXECUTABLE_PATTERN}}
            ]
        },
    )
    args: tuple[str, ...] = Field(
        default_factory=tuple,
        json_schema_extra={
            "items": {"type": "string", "pattern": r"^[^\r\n]*$"}
        },
    )
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
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra=add_project_config_schema_constraints,
    )

    project_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    repo_path: Path = Field(
        json_schema_extra={"pattern": WINDOWS_ABSOLUTE_PATH_PATTERN}
    )
    remote: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    base_branch: str = Field(
        pattern=BRANCH_PATTERN,
        json_schema_extra=BRANCH_SCHEMA_EXTRA,
    )
    test_branch: str = Field(
        pattern=BRANCH_PATTERN,
        json_schema_extra=BRANCH_SCHEMA_EXTRA,
    )
    github_repository: str = Field(pattern=GITHUB_REPOSITORY_PATTERN)
    version_endpoint: str = Field(
        pattern=HTTPS_URL_PATTERN,
        json_schema_extra={"format": "uri"},
    )
    version_sha_field: str = Field(
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
    )
    required_pre_pr_checks: tuple[CommandSpec, ...] = Field(
        min_length=1,
    )
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
            or branch == "@"
            or branch.endswith(".")
            or any(
                component.endswith(".lock") for component in branch.split("/")
            )
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
            if executable in direct_tools:
                tool = executable
                tool_arguments = check.args
            elif (
                executable in python_tools
                and len(check.args) >= 2
                and check.args[0] == "-m"
                and check.args[1] in python_modules
            ):
                tool = check.args[1]
                tool_arguments = check.args[2:]
            else:
                raise ValueError("项目预检命令不属于第一版验证工具白名单")
            if not validation_arguments_are_safe(tool, tool_arguments):
                raise ValueError("项目预检命令包含非只读或未批准参数")
        return self


class LoadedProjectConfig(FrozenModel):
    config: ProjectConfig
    config_hash: str
    source: Path
