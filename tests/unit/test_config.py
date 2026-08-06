from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from change_delivery.config import load_project_config
from change_delivery.errors import ConfigurationError


def valid_config_data() -> dict[str, object]:
    return {
        "project_id": "example-backend",
        "repo_path": "C:/path/to/example-project",
        "remote": "origin",
        "remote_fetch_url": "https://github.com/example-org/example_backend.git",
        "remote_push_url": "git@github.com:example-org/example_backend.git",
        "base_branch": "develop",
        "test_branch": "test",
        "github_repository": "example-org/example_backend",
        "version_endpoint": "https://example.test/version",
        "version_sha_field": "git_sha",
        "required_pre_pr_checks": [
            {
                "executable": "python",
                "args": ["-m", "pytest", "test/api"],
                "workdir": "C:/path/to/example-project",
                "timeout_seconds": 900,
            }
        ],
    }


def write_yaml(path: Path, data: dict[str, object], *, sort_keys: bool = False) -> None:
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=sort_keys),
        encoding="utf-8",
    )


def test_load_project_config_returns_expected_model(tmp_path: Path) -> None:
    config_file = tmp_path / "project.yaml"
    write_yaml(config_file, valid_config_data())

    loaded = load_project_config(config_file)
    loaded_again = load_project_config(config_file)
    check = loaded.config.required_pre_pr_checks[0]

    assert loaded.config_hash == loaded_again.config_hash
    assert loaded.config.project_id == "example-backend"
    assert loaded.config.repo_path == Path(
        "C:/path/to/example-project"
    )
    assert loaded.config.remote == "origin"
    assert loaded.config.remote_fetch_url == (
        "https://github.com/example-org/example_backend.git"
    )
    assert loaded.config.remote_push_url == (
        "git@github.com:example-org/example_backend.git"
    )
    assert loaded.config.base_branch == "develop"
    assert loaded.config.test_branch == "test"
    assert loaded.config.test_push_strategy == "force-with-lease"
    assert loaded.config.max_internal_review_fix_attempts == 3
    assert loaded.config.github_repository == "example-org/example_backend"
    assert loaded.config.version_endpoint == "https://example.test/version"
    assert loaded.config.version_sha_field == "git_sha"
    assert check.executable == "python"
    assert check.args == ("-m", "pytest", "test/api")
    assert check.workdir == Path("C:/path/to/example-project")
    assert check.timeout_seconds == 900
    assert loaded.config.credentials == ()
    assert loaded.config.production_enabled is False


def test_project_config_accepts_bounded_codex_timeout(tmp_path: Path) -> None:
    config_file = tmp_path / "project.yaml"
    data = valid_config_data()
    data["codex_timeout_seconds"] = 321
    write_yaml(config_file, data)

    loaded = load_project_config(config_file)

    assert loaded.config.codex_timeout_seconds == 321


def test_hash_is_independent_of_yaml_key_order(tmp_path: Path) -> None:
    first_file = tmp_path / "first.yaml"
    second_file = tmp_path / "second.yaml"
    data = valid_config_data()
    write_yaml(first_file, data, sort_keys=False)
    write_yaml(second_file, dict(reversed(list(data.items()))), sort_keys=False)

    assert (
        load_project_config(first_file).config_hash
        == load_project_config(second_file).config_hash
    )


def test_hash_treats_explicit_and_omitted_defaults_as_equal(tmp_path: Path) -> None:
    omitted_file = tmp_path / "omitted.yaml"
    explicit_file = tmp_path / "explicit.yaml"
    omitted = valid_config_data()
    explicit = valid_config_data()
    explicit_check = explicit["required_pre_pr_checks"][0]  # type: ignore[index]
    explicit_check["env_names"] = []  # type: ignore[index]
    explicit["credentials"] = []
    explicit["production_enabled"] = False
    explicit["test_push_strategy"] = "force-with-lease"
    explicit["max_internal_review_fix_attempts"] = 3
    write_yaml(omitted_file, omitted)
    write_yaml(explicit_file, explicit)

    assert (
        load_project_config(omitted_file).config_hash
        == load_project_config(explicit_file).config_hash
    )


def test_hash_changes_when_configuration_semantics_change(tmp_path: Path) -> None:
    first_file = tmp_path / "first.yaml"
    second_file = tmp_path / "second.yaml"
    first = valid_config_data()
    second = valid_config_data()
    second["test_branch"] = "staging"
    write_yaml(first_file, first)
    write_yaml(second_file, second)

    assert (
        load_project_config(first_file).config_hash
        != load_project_config(second_file).config_hash
    )


def test_hash_changes_when_test_push_strategy_changes(tmp_path: Path) -> None:
    first_file = tmp_path / "first.yaml"
    second_file = tmp_path / "second.yaml"
    first = valid_config_data()
    second = valid_config_data()
    second["test_push_strategy"] = "force"
    write_yaml(first_file, first)
    write_yaml(second_file, second)

    assert (
        load_project_config(first_file).config_hash
        != load_project_config(second_file).config_hash
    )


def test_hash_changes_when_internal_review_fix_limit_changes(
    tmp_path: Path,
) -> None:
    first_file = tmp_path / "first.yaml"
    second_file = tmp_path / "second.yaml"
    first = valid_config_data()
    second = valid_config_data()
    second["max_internal_review_fix_attempts"] = 2
    write_yaml(first_file, first)
    write_yaml(second_file, second)

    assert (
        load_project_config(first_file).config_hash
        != load_project_config(second_file).config_hash
    )


def test_hash_changes_when_remote_identity_changes(tmp_path: Path) -> None:
    first_file = tmp_path / "first.yaml"
    second_file = tmp_path / "second.yaml"
    first = valid_config_data()
    second = valid_config_data()
    second["remote_push_url"] = "ssh://git@example.test/other.git"
    write_yaml(first_file, first)
    write_yaml(second_file, second)

    assert (
        load_project_config(first_file).config_hash
        != load_project_config(second_file).config_hash
    )


@pytest.mark.parametrize(
    ("filename", "contents", "cause_type"),
    [
        ("invalid.yaml", b"\xff\xfe\xfa", UnicodeDecodeError),
        ("syntax.yaml", b"project_id: [", yaml.YAMLError),
        ("validation.yaml", b"project_id: incomplete\n", ValidationError),
    ],
)
def test_invalid_project_files_are_wrapped_with_cause(
    tmp_path: Path,
    filename: str,
    contents: bytes,
    cause_type: type[BaseException],
) -> None:
    config_file = tmp_path / filename
    config_file.write_bytes(contents)

    with pytest.raises(ConfigurationError, match="项目配置无效") as captured:
        load_project_config(config_file)

    assert isinstance(captured.value.__cause__, cause_type)


def test_missing_project_file_is_wrapped_with_cause(tmp_path: Path) -> None:
    config_file = tmp_path / "missing.yaml"

    with pytest.raises(ConfigurationError, match="项目配置无效") as captured:
        load_project_config(config_file)

    assert isinstance(captured.value.__cause__, OSError)


def test_project_config_rejects_production_enablement(tmp_path: Path) -> None:
    config_file = tmp_path / "unsafe.yaml"
    data = valid_config_data()
    data["production_enabled"] = True
    write_yaml(config_file, data)

    with pytest.raises(ConfigurationError, match="正式发布"):
        load_project_config(config_file)
