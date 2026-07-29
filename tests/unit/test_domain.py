import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError
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
        "remote_fetch_url": "https://github.com/owner/repo.git",
        "remote_push_url": "git@github.com:owner/repo.git",
        "base_branch": "develop",
        "test_branch": "test",
        "github_repository": "owner/repo",
        "version_endpoint": "https://example.test/version",
        "version_sha_field": "data.git_sha",
        "required_pre_pr_checks": [valid_command_data()],
    }


def valid_project_json_data() -> dict[str, object]:
    return ProjectConfig.model_validate(valid_project_data()).model_dump(mode="json")


def project_schema_validator() -> Draft202012Validator:
    return Draft202012Validator(ProjectConfig.model_json_schema())


@pytest.mark.parametrize(
    "field",
    ["remote_fetch_url", "remote_push_url"],
)
def test_project_config_requires_explicit_remote_identity(field: str) -> None:
    data = valid_project_data()
    data.pop(field)

    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(data)


@pytest.mark.parametrize(
    "identity",
    [
        "",
        " ",
        "-instead-of-a-url",
        "relative/repository.git",
        "https:///missing-host.git",
        "ssh:///missing-host.git",
        "https://example.test/repo.git\n--upload-pack=evil",
        "ssh://git@example.test/repo.git\x00evil",
    ],
)
@pytest.mark.parametrize("field", ["remote_fetch_url", "remote_push_url"])
def test_project_config_and_schema_reject_invalid_remote_identity(
    field: str,
    identity: str,
) -> None:
    data = valid_project_data()
    data[field] = identity
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(data)

    schema_data = valid_project_json_data()
    schema_data[field] = identity
    with pytest.raises(JSONSchemaValidationError):
        project_schema_validator().validate(schema_data)


@pytest.mark.parametrize(
    "identity",
    [
        "https://github.com/owner/repo.git",
        "ssh://git@github.com/owner/repo.git",
        "git@github.com:owner/repo.git",
        "C:/test/remotes/origin.git",
        r"\\server\share\origin.git",
    ],
)
def test_project_config_accepts_supported_remote_identity(identity: str) -> None:
    data = valid_project_data()
    data["remote_fetch_url"] = identity
    data["remote_push_url"] = identity

    config = ProjectConfig.model_validate(data)
    schema_data = config.model_dump(mode="json")
    project_schema_validator().validate(schema_data)

    assert config.remote_fetch_url == identity
    assert config.remote_push_url == identity


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
    "args",
    [
        ("first\nsecond",),
        ("first\rsecond",),
    ],
)
def test_command_spec_rejects_newlines_in_arguments(args: tuple[str, ...]) -> None:
    data = valid_command_data()
    data["args"] = args

    with pytest.raises(ValidationError):
        CommandSpec.model_validate(data)


@pytest.mark.parametrize(
    "args",
    [
        ("-k", "foo|bar"),
        ("--expression", "a;b"),
        ("literal", "a>b"),
        ("cmd /c echo",),
        ("value;rm x",),
        ("foo|Remove-Item x",),
    ],
)
def test_command_spec_accepts_literal_shell_characters_in_argv(
    args: tuple[str, ...],
) -> None:
    data = valid_command_data()
    data["args"] = args

    spec = CommandSpec.model_validate(data)

    assert spec.args == args


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
        ("base_branch", "feature~next"),
        ("test_branch", "feature^next"),
        ("base_branch", "feature:next"),
        ("test_branch", "feature?next"),
        ("base_branch", "feature*next"),
        ("test_branch", "feature[next"),
        ("base_branch", "feature//next"),
        ("test_branch", "feature/"),
        ("base_branch", "feature."),
        ("test_branch", "feature.lock"),
        ("base_branch", "feature/.hidden"),
        ("test_branch", "foo/bar.lock/baz"),
        ("base_branch", "@"),
    ],
)
def test_project_config_rejects_invalid_branches(field: str, branch: str) -> None:
    data = valid_project_data()
    data[field] = branch

    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(data)

    schema_data = valid_project_json_data()
    schema_data[field] = branch
    with pytest.raises(JSONSchemaValidationError):
        project_schema_validator().validate(schema_data)


def test_project_config_accepts_uppercase_lock_component() -> None:
    data = valid_project_data()
    data["base_branch"] = "foo/bar.LOCK/baz"

    config = ProjectConfig.model_validate(data)

    assert config.base_branch == "foo/bar.LOCK/baz"


def test_project_schema_exposes_branch_and_absolute_path_patterns() -> None:
    schema = ProjectConfig.model_json_schema()
    properties = schema["properties"]
    command_properties = schema["$defs"]["CommandSpec"]["properties"]

    assert properties["base_branch"]["pattern"]
    assert (
        properties["base_branch"]["pattern"]
        == properties["test_branch"]["pattern"]
    )
    assert properties["repo_path"]["pattern"]
    assert command_properties["workdir"]["pattern"]


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.test/version",
        "example.test/version",
        "https://?query",
        "https://#fragment",
        "https:///version",
        "https://user@?query",
        "https://:443/path",
    ],
)
def test_project_config_requires_https_version_url(endpoint: str) -> None:
    data = valid_project_data()
    data["version_endpoint"] = endpoint

    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(data)

    schema_data = valid_project_json_data()
    schema_data["version_endpoint"] = endpoint
    with pytest.raises(JSONSchemaValidationError):
        project_schema_validator().validate(schema_data)


def test_project_config_normalizes_uppercase_https_scheme() -> None:
    data = valid_project_data()
    data["version_endpoint"] = "HTTPS://example.test/version"

    config = ProjectConfig.model_validate(data)

    assert config.version_endpoint == "https://example.test/version"
    schema_data = valid_project_json_data()
    schema_data["version_endpoint"] = "HTTPS://example.test/version"
    project_schema_validator().validate(schema_data)


@pytest.mark.parametrize(
    ("executable", "args"),
    [
        ("pytest", ("tests",)),
        (r"C:\venv\Scripts\ruff.exe", ("check", "--no-fix", ".")),
        ("/usr/bin/mypy", ("src",)),
        ("python", ("-m", "pytest", "tests")),
        ("python3", ("-m", "ruff", "check", ".", "--no-fix")),
        (
            "ruff",
            ("check", ".", "--config", "fix=true", "--no-fix"),
        ),
        ("py", ("-m", "mypy", "src")),
        ("python", ("-m", "compileall", "src")),
    ],
)
def test_project_config_accepts_validation_profile_commands(
    executable: str,
    args: tuple[str, ...],
) -> None:
    data = valid_project_data()
    data["required_pre_pr_checks"] = [
        {
            "executable": executable,
            "args": args,
            "workdir": Path("C:/repo"),
        }
    ]

    config = ProjectConfig.model_validate(data)

    assert config.required_pre_pr_checks[0].args == args
    schema_data = valid_project_json_data()
    schema_data["required_pre_pr_checks"] = [
        {
            "executable": executable,
            "args": list(args),
            "workdir": "C:/repo",
        }
    ]
    project_schema_validator().validate(schema_data)


@pytest.mark.parametrize(
    ("executable", "args"),
    [
        ("python", ("-c", "print('unsafe')")),
        ("python", ("script.py",)),
        ("git", ("clean", "-fd")),
        ("gh", ("pr", "create")),
        ("codex", ("exec", "task")),
        ("node", ("script.js",)),
        ("coverage", ("run", "-m", "pytest")),
        ("ruff", ("format", ".")),
        ("ruff", ("check", ".")),
        ("ruff", ("check", ".", "--config", "fix=true")),
        ("python", ("-m", "ruff", "check", ".")),
        ("ruff", ("check", ".", "--fix")),
        ("ruff", ("check", ".", "--", "--no-fix")),
        (
            "python",
            ("-m", "ruff", "check", ".", "--", "--no-fix"),
        ),
        ("ruff", ("check", ".", "--fix-only", "--no-fix")),
        (
            "python",
            ("-m", "ruff", "check", ".", "--fix-only=true", "--no-fix"),
        ),
        ("python", ("-m", "ruff", "check", ".", "--unsafe-fixes")),
        ("ruff", ("check", ".", "--add-noqa")),
        ("mypy", ("--install-types", "src")),
        ("python", ("-m", "mypy", "--install-types=yes", "src")),
        ("pytest", ("--cache-clear", "tests")),
        ("python", ("-m", "pytest", "--basetemp", "C:/tmp")),
        ("pytest", ("--basetemp=C:/tmp", "tests")),
    ],
)
def test_project_config_rejects_commands_outside_validation_profile(
    executable: str,
    args: tuple[str, ...],
) -> None:
    data = valid_project_data()
    data["required_pre_pr_checks"] = [
        {
            "executable": executable,
            "args": args,
            "workdir": Path("C:/repo"),
        }
    ]

    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(data)

    schema_data = valid_project_json_data()
    schema_data["required_pre_pr_checks"] = [
        {
            "executable": executable,
            "args": list(args),
            "workdir": "C:/repo",
        }
    ]
    with pytest.raises(JSONSchemaValidationError):
        project_schema_validator().validate(schema_data)


def test_project_schema_rejects_ruff_without_args() -> None:
    schema_data = valid_project_json_data()
    schema_data["required_pre_pr_checks"] = [
        {
            "executable": "ruff",
            "workdir": "C:/repo",
        }
    ]

    with pytest.raises(JSONSchemaValidationError):
        project_schema_validator().validate(schema_data)


@pytest.mark.parametrize(
    ("executable", "args"),
    [
        ("powershell", ("Get-Date",)),
        ("tools/check.ps1", ()),
        ("python", ("first\nsecond",)),
        ("python", ("\n",)),
        ("python", ("trailing\n",)),
        ("python", ("\r",)),
        ("python", ("trailing\r",)),
    ],
)
def test_command_spec_schema_rejects_runtime_security_violations(
    executable: str,
    args: tuple[str, ...],
) -> None:
    data = {
        "executable": executable,
        "args": list(args),
        "workdir": "C:/repo",
    }
    command_schema = ProjectConfig.model_json_schema()["$defs"]["CommandSpec"]

    with pytest.raises(ValidationError):
        CommandSpec.model_validate(data)
    with pytest.raises(JSONSchemaValidationError):
        Draft202012Validator(command_schema).validate(data)


@pytest.mark.parametrize(
    "repository",
    [
        "owner with-space/repo",
        "owner/repo with-space",
        "owner\tname/repo",
        "owner/repo\nname",
    ],
)
def test_github_repository_rejects_whitespace_and_control(
    repository: str,
) -> None:
    data = valid_project_data()
    data["github_repository"] = repository

    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(data)

    schema_data = valid_project_json_data()
    schema_data["github_repository"] = repository
    with pytest.raises(JSONSchemaValidationError):
        project_schema_validator().validate(schema_data)


def test_project_schema_is_valid_draft_2020_12() -> None:
    Draft202012Validator.check_schema(ProjectConfig.model_json_schema())


def test_committed_project_schema_matches_model() -> None:
    schema_path = Path(__file__).parents[2] / "schemas" / "project.schema.json"

    assert json.loads(schema_path.read_text(encoding="utf-8")) == (
        ProjectConfig.model_json_schema()
    )


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
