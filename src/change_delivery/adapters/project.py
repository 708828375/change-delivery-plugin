from __future__ import annotations

import hmac
import os
from collections.abc import Callable, Mapping
from pathlib import Path
import re
from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import Field

from change_delivery.domain import CommandSpec, FrozenModel, ProjectConfig


_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_RUN_ID = re.compile(
    r"^(?=.{1,128}$)[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*$"
)
_PYTEST_CONFIGS = (
    "pyproject.toml",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
)


class HttpReader(Protocol):
    def get_json(self, url: str) -> object: ...


class GitReader(Protocol):
    repo_path: Path
    remote: str
    base_branch: str
    test_branch: str

    def remote_sha(self, remote: str, branch: str) -> str: ...


class DiscoveryReport(FrozenModel):
    status: Literal["passed", "blocked"]
    repo_path: Path
    remote: str
    base_branch: str
    test_branch: str
    remote_shas: dict[str, str]
    pytest_configs: tuple[str, ...]
    candidate_checks: tuple[CommandSpec, ...]
    config: ProjectConfig
    evidence: tuple[str, ...] = Field(min_length=1)


class RegistrationReport(FrozenModel):
    status: Literal["passed", "blocked"]
    config: ProjectConfig | None
    missing_fields: tuple[str, ...]


class ExampleProjectAdapter:
    def __init__(
        self,
        config: ProjectConfig,
        *,
        git: GitReader,
        github: object,
        http: HttpReader,
        file_exists: Callable[[Path], bool] = Path.is_file,
    ) -> None:
        self.config = config
        self.git = git
        self.github = github
        self.http = http
        self._file_exists = file_exists

    def discover(self, *, dry_run: bool) -> DiscoveryReport:
        if not dry_run:
            raise ValueError("ExampleProject discovery is read-only and requires dry_run")
        self._verify_git_binding()

        remote_shas = {
            f"{self.config.remote}/{branch}": self.git.remote_sha(
                self.config.remote,
                branch,
            )
            for branch in (self.config.base_branch, self.config.test_branch)
        }
        for sha in remote_shas.values():
            if not _SHA.fullmatch(sha):
                raise ValueError("remote SHA must be 40 hexadecimal characters")

        pytest_configs = tuple(
            name
            for name in _PYTEST_CONFIGS
            if self._file_exists(self.config.repo_path / name)
        )
        evidence = (
            f"repo:{self.config.repo_path}",
            f"remote:{self.config.remote}",
            *(f"{ref}:{sha.lower()}" for ref, sha in remote_shas.items()),
        )
        return DiscoveryReport(
            status="passed",
            repo_path=self.config.repo_path,
            remote=self.config.remote,
            base_branch=self.config.base_branch,
            test_branch=self.config.test_branch,
            remote_shas={
                ref: sha.lower() for ref, sha in remote_shas.items()
            },
            pytest_configs=pytest_configs,
            candidate_checks=self.config.required_pre_pr_checks,
            config=self.config,
            evidence=evidence,
        )

    def deployed_sha(self) -> str:
        body = self.http.get_json(self.config.version_endpoint)
        value: object = body
        for component in self.config.version_sha_field.split("."):
            if not isinstance(value, Mapping) or component not in value:
                raise ValueError(
                    "version field must contain a 40 hexadecimal SHA"
                )
            value = value[component]
        if not isinstance(value, str) or not _SHA.fullmatch(value):
            raise ValueError("version field must contain a 40 hexadecimal SHA")
        return value.lower()

    def version_matches(self, expected_sha: str) -> bool:
        if not isinstance(expected_sha, str) or not _SHA.fullmatch(expected_sha):
            raise ValueError("expected SHA must contain 40 hexadecimal characters")
        return hmac.compare_digest(
            self.deployed_sha(),
            expected_sha.lower(),
        )

    def _verify_git_binding(self) -> None:
        expected_path = os.path.normcase(
            str(self.config.repo_path.resolve(strict=False))
        )
        actual_path = os.path.normcase(
            str(Path(self.git.repo_path).resolve(strict=False))
        )
        if actual_path != expected_path:
            raise ValueError("Git probe repository path does not match config")
        for attribute, expected, label in (
            ("remote", self.config.remote, "remote"),
            ("base_branch", self.config.base_branch, "base branch"),
            ("test_branch", self.config.test_branch, "test branch"),
            (
                "remote_fetch_url",
                self.config.remote_fetch_url,
                "remote fetch URL",
            ),
            (
                "remote_push_url",
                self.config.remote_push_url,
                "remote push URL",
            ),
        ):
            actual = getattr(self.git, attribute, None)
            if actual is None and attribute == "remote_fetch_url":
                actual = getattr(self.git, "_fetch_url", None)
            if actual is None and attribute == "remote_push_url":
                actual = getattr(self.git, "_push_url", None)
            if not isinstance(actual, str) or not hmac.compare_digest(
                actual,
                expected,
            ):
                raise ValueError(f"Git probe {label} does not match config")


def discover_registration(
    registration: Mapping[str, object],
    *,
    dry_run: bool,
) -> RegistrationReport:
    raw = dict(registration)
    endpoint = raw.get("version_endpoint")
    if not isinstance(endpoint, str) or not endpoint.strip():
        return RegistrationReport(
            status="blocked",
            config=None,
            missing_fields=("version_endpoint",),
        )
    hostname = urlsplit(endpoint).hostname
    normalized_hostname = (
        hostname.rstrip(".").casefold() if hostname is not None else None
    )
    if (
        not dry_run
        and normalized_hostname is not None
        and (
            normalized_hostname == "invalid"
            or normalized_hostname.endswith(".invalid")
        )
    ):
        return RegistrationReport(
            status="blocked",
            config=None,
            missing_fields=("version_endpoint",),
        )
    return RegistrationReport(
        status="passed",
        config=ProjectConfig.model_validate(raw),
        missing_fields=(),
    )


def acceptance_data_prefix(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("run ID is invalid")
    return f"codex-{run_id}"


def cleanup_matches_run(candidate: str, run_id: str) -> bool:
    prefix = acceptance_data_prefix(run_id)
    return candidate == prefix or candidate.startswith(f"{prefix}--")
