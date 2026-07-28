from pathlib import Path

import pytest
from pydantic import ValidationError

from change_delivery.domain import (
    CommandSpec,
    LoadedProjectConfig,
    ProjectConfig,
    RunState,
)


def valid_command_data() -> dict[str, object]:
    return {
        "executable": "python",
        "args": ["-m", "pytest", "test/api"],
        "workdir": Path("C:/repo"),
    }


def valid_project_data() -> dict[str, object]:
    return {
        "project_id": "example-project",
        "repo_path": Path("C:/repo"),
        "remote": "origin",
        "base_branch": "develop",
        "test_branch": "test",
        "github_repository": "owner/repo",
        "version_endpoint": "https://example.test/version",
        "version_sha_field": "data.git_sha",
        "required_pre_pr_checks": [valid_command_data()],
    }


@pytest.mark.parametrize(
    "executable",
    [
        "powershell",
        "PWSH",
        "cmd",
        r"C:\Windows\System32\CMD.EXE",
        "/usr/bin/bash",
        "sh",
        r"C:\Windows\System32\wsl",
        "/usr/bin/rm",
        "Remove-Item",
        "/bin/rmdir",
    ],
)
def test_command_spec_rejects_shell_and_destructive_executables(
    executable: str,
) -> None:
    data = valid_command_data()
    data["executable"] = executable

    with pytest.raises(ValidationError):
        CommandSpec.model_validate(data)


@pytest.mark.parametrize(
    "executable",
    [
        "deploy.ps1",
        r"C:\scripts\DEPLOY.BAT",
        "tools/build.cmd",
        "/tmp/check.sh",
    ],
)
def test_command_spec_rejects_script_executables(executable: str) -> None:
    data = valid_command_data()
    data["executable"] = executable

    with pytest.raises(ValidationError):
        CommandSpec.model_validate(data)


@pytest.mark.parametrize(
    "argument",
    [
        "first\nsecond",
        "git status; git clean",
        "first && second",
        "first || second",
        "first | second",
        "result > output.txt",
        "tool < input.txt",
    ],
)
def test_command_spec_rejects_shell_control_in_each_argument(argument: str) -> None:
    data = valid_command_data()
    data["args"] = ["-m", "pytest", argument]

    with pytest.raises(ValidationError):
        CommandSpec.model_validate(data)


def test_command_spec_accepts_regular_pytest_arguments() -> None:
    spec = CommandSpec.model_validate(valid_command_data())

    assert spec.executable == "python"
    assert spec.args == ("-m", "pytest", "test/api")


def test_run_state_has_ready_to_merge_terminal_gate() -> None:
    assert RunState.READY_TO_MERGE.value == "READY_TO_MERGE"
    assert RunState.COMPLETED.value == "COMPLETED"


def test_command_spec_preserves_absolute_path_workdir() -> None:
    spec = CommandSpec.model_validate(valid_command_data())

    assert spec.workdir == Path("C:/repo")
    assert isinstance(spec.workdir, Path)


def test_command_spec_rejects_relative_workdir() -> None:
    data = valid_command_data()
    data["workdir"] = Path("repo")

    with pytest.raises(ValidationError):
        CommandSpec.model_validate(data)


def test_project_config_rejects_relative_repo_path() -> None:
    data = valid_project_data()
    data["repo_path"] = Path("repo")

    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(data)


@pytest.mark.parametrize(
    ("field", "branch"),
    [
        ("base_branch", "feature with-space"),
        ("test_branch", "-dangerous"),
        ("base_branch", "feature..next"),
        ("test_branch", "feature@{next"),
        ("base_branch", r"feature\next"),
        ("test_branch", "feature\nnext"),
    ],
)
def test_project_config_rejects_invalid_branches(field: str, branch: str) -> None:
    data = valid_project_data()
    data[field] = branch

    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(data)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.test/version",
        "example.test/version",
        "https:///version",
    ],
)
def test_project_config_requires_https_version_url(endpoint: str) -> None:
    data = valid_project_data()
    data["version_endpoint"] = endpoint

    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(data)


@pytest.mark.parametrize(
    "field_name",
    ["git-sha", ".git_sha", "git_sha.", "data..git_sha", "data/git_sha"],
)
def test_project_config_rejects_invalid_version_sha_field(field_name: str) -> None:
    data = valid_project_data()
    data["version_sha_field"] = field_name

    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(data)


def test_collection_fields_are_immutable_tuples() -> None:
    command = CommandSpec.model_validate(valid_command_data())
    project = ProjectConfig.model_validate(valid_project_data())

    assert isinstance(command.args, tuple)
    assert isinstance(command.env_names, tuple)
    assert isinstance(project.required_pre_pr_checks, tuple)
    assert isinstance(project.credentials, tuple)
    with pytest.raises(AttributeError):
        command.args.append("extra")  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        command.env_names.clear()  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        project.required_pre_pr_checks.append(command)  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        project.credentials.clear()  # type: ignore[attr-defined]


def test_all_models_reject_field_reassignment() -> None:
    command = CommandSpec.model_validate(valid_command_data())
    project = ProjectConfig.model_validate(valid_project_data())
    loaded = LoadedProjectConfig(
        config=project,
        config_hash="a" * 64,
        source=Path("C:/config.yaml"),
    )

    with pytest.raises(ValidationError):
        command.args = ("--version",)
    with pytest.raises(ValidationError):
        project.credentials = ("TOKEN",)
    with pytest.raises(ValidationError):
        loaded.config_hash = "b" * 64


@pytest.mark.parametrize("model_name", ["command", "project", "loaded"])
def test_all_models_forbid_extra_fields(model_name: str) -> None:
    project = ProjectConfig.model_validate(valid_project_data())
    data_by_model = {
        "command": {**valid_command_data(), "unexpected": True},
        "project": {**valid_project_data(), "unexpected": True},
        "loaded": {
            "config": project,
            "config_hash": "a" * 64,
            "source": Path("C:/config.yaml"),
            "unexpected": True,
        },
    }
    model_by_name = {
        "command": CommandSpec,
        "project": ProjectConfig,
        "loaded": LoadedProjectConfig,
    }

    with pytest.raises(ValidationError):
        model_by_name[model_name].model_validate(data_by_model[model_name])


def test_project_schema_forbids_extra_properties_at_each_model_level() -> None:
    schema = ProjectConfig.model_json_schema()

    assert schema["additionalProperties"] is False
    assert schema["$defs"]["CommandSpec"]["additionalProperties"] is False
