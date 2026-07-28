from pathlib import Path

import pytest

from change_delivery.config import load_project_config
from change_delivery.errors import ConfigurationError


def test_load_project_config_returns_stable_hash(tmp_path: Path) -> None:
    config_file = tmp_path / "project.yaml"
    config_file.write_text(
        """
project_id: example-backend
repo_path: C:/workspace/example-project
remote: origin
base_branch: develop
test_branch: test
github_repository: example-org/example_backend
version_endpoint: https://example.test/version
version_sha_field: git_sha
required_pre_pr_checks:
  - executable: python
    args: ["-m", "pytest", "test/api"]
    workdir: C:/workspace/example-project
    timeout_seconds: 900
credentials: []
production_enabled: false
""".strip(),
        encoding="utf-8",
    )

    first = load_project_config(config_file)
    second = load_project_config(config_file)
    check = first.config.required_pre_pr_checks[0]

    assert first.config_hash == second.config_hash
    assert first.config.project_id == "example-backend"
    assert first.config.repo_path == Path(
        "C:/workspace/example-project"
    )
    assert first.config.remote == "origin"
    assert first.config.base_branch == "develop"
    assert first.config.test_branch == "test"
    assert first.config.github_repository == "example-org/example_backend"
    assert first.config.version_endpoint == "https://example.test/version"
    assert first.config.version_sha_field == "git_sha"
    assert check.executable == "python"
    assert check.args == ["-m", "pytest", "test/api"]
    assert check.workdir == Path("C:/workspace/example-project")
    assert check.timeout_seconds == 900
    assert first.config.credentials == []
    assert first.config.production_enabled is False


def test_project_config_rejects_production_enablement(tmp_path: Path) -> None:
    config_file = tmp_path / "unsafe.yaml"
    config_file.write_text(
        """
project_id: unsafe
repo_path: C:/repo
remote: origin
base_branch: main
test_branch: test
github_repository: owner/repo
version_endpoint: https://example.test/version
version_sha_field: git_sha
required_pre_pr_checks:
  - executable: python
    args: ["-m", "pytest"]
    workdir: C:/repo
    timeout_seconds: 900
credentials: []
production_enabled: true
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="正式发布"):
        load_project_config(config_file)
