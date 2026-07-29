from __future__ import annotations

import json
from pathlib import Path
from urllib.error import URLError

import pytest

from change_delivery.adapters.http import HttpMethodNotAllowed, ReadOnlyHttpClient
from change_delivery.adapters.project import (
    ExampleProjectAdapter,
    acceptance_data_prefix,
    cleanup_matches_run,
    discover_registration,
)
from change_delivery.config import load_project_config


DEVELOP_SHA = "a" * 40
TEST_SHA = "b" * 40


class FakeHttp:
    def __init__(self, body: object) -> None:
        self.json_body = body
        self.calls: list[tuple[str, str]] = []

    def get_json(self, url: str) -> object:
        self.calls.append(("GET", url))
        return self.json_body


class FakeGit:
    def __init__(self, config) -> None:
        self.repo_path = config.repo_path
        self.remote = config.remote
        self.base_branch = config.base_branch
        self.test_branch = config.test_branch
        self.remote_fetch_url = config.remote_fetch_url
        self.remote_push_url = config.remote_push_url
        self.read_calls: list[tuple[str, str]] = []
        self.write_calls: list[object] = []

    def remote_sha(self, remote: str, branch: str) -> str:
        self.read_calls.append((remote, branch))
        return DEVELOP_SHA if branch == "develop" else TEST_SHA


class FakeGitHub:
    def __init__(self) -> None:
        self.write_calls: list[object] = []


@pytest.fixture
def fixture_config():
    fixture = Path(__file__).parents[1] / "fixtures" / "example-project.yaml"
    return load_project_config(fixture).config


def test_project_dry_run_uses_only_read_adapters(fixture_config) -> None:
    fake_http = FakeHttp({"git_sha": TEST_SHA})
    fake_git = FakeGit(fixture_config)
    fake_github = FakeGitHub()
    adapter = ExampleProjectAdapter(
        fixture_config,
        git=fake_git,
        github=fake_github,
        http=fake_http,
        file_exists=lambda path: path.name in {"pyproject.toml", "pytest.ini"},
    )

    report = adapter.discover(dry_run=True)

    assert report.repo_path == Path("C:/workspace/example-project")
    assert report.remote == "origin"
    assert report.base_branch == "develop"
    assert report.test_branch == "test"
    assert report.remote_shas == {
        "origin/develop": DEVELOP_SHA,
        "origin/test": TEST_SHA,
    }
    assert report.pytest_configs == ("pyproject.toml", "pytest.ini")
    assert report.candidate_checks == fixture_config.required_pre_pr_checks
    assert fake_git.write_calls == []
    assert fake_github.write_calls == []
    assert fake_http.calls == []


def test_discovery_rejects_git_probe_bound_to_other_remote_url(
    fixture_config,
) -> None:
    fake_git = FakeGit(fixture_config)
    fake_git.remote_push_url = "C:/other/remote.git"
    adapter = ExampleProjectAdapter(
        fixture_config,
        git=fake_git,
        github=FakeGitHub(),
        http=FakeHttp({"git_sha": TEST_SHA}),
    )

    with pytest.raises(ValueError, match="remote push URL"):
        adapter.discover(dry_run=True)


def test_discovery_rejects_git_probe_without_frozen_remote_identity(
    fixture_config,
) -> None:
    fake_git = FakeGit(fixture_config)
    del fake_git.remote_fetch_url
    adapter = ExampleProjectAdapter(
        fixture_config,
        git=fake_git,
        github=FakeGitHub(),
        http=FakeHttp({"git_sha": TEST_SHA}),
    )

    with pytest.raises(ValueError, match="remote fetch URL"):
        adapter.discover(dry_run=True)


def test_version_probe_requires_present_exact_40_hex_sha(
    fixture_config,
) -> None:
    fake_http = FakeHttp({"git_sha": TEST_SHA.upper()})
    adapter = ExampleProjectAdapter(
        fixture_config,
        git=FakeGit(fixture_config),
        github=FakeGitHub(),
        http=fake_http,
    )

    assert adapter.version_matches(TEST_SHA)
    assert not adapter.version_matches("c" * 40)
    assert fake_http.calls == [
        ("GET", "https://example.test.invalid/version"),
        ("GET", "https://example.test.invalid/version"),
    ]

    for body in ({}, {"git_sha": "short"}, {"git_sha": "g" * 40}):
        fake_http.json_body = body
        with pytest.raises(ValueError, match="40 hexadecimal"):
            adapter.deployed_sha()


def test_nested_version_sha_field_is_supported(fixture_config) -> None:
    config = fixture_config.model_copy(
        update={"version_sha_field": "build.git_sha"}
    )
    adapter = ExampleProjectAdapter(
        config,
        git=FakeGit(config),
        github=FakeGitHub(),
        http=FakeHttp({"build": {"git_sha": TEST_SHA}}),
    )

    assert adapter.deployed_sha() == TEST_SHA


def test_missing_real_version_endpoint_is_blocked(fixture_config) -> None:
    raw = fixture_config.model_dump(mode="python")
    raw.pop("version_endpoint")

    report = discover_registration(raw, dry_run=True)

    assert report.status == "blocked"
    assert report.missing_fields == ("version_endpoint",)
    assert report.config is None


@pytest.mark.parametrize(
    "endpoint",
    (
        "https://example.test.invalid/version",
        "https://INVALID/version",
        "https://project.test.INVALID./version",
    ),
)
def test_fixture_endpoint_cannot_be_used_for_real_registration(
    fixture_config,
    endpoint,
) -> None:
    registration = fixture_config.model_dump(mode="python")
    registration["version_endpoint"] = endpoint

    report = discover_registration(
        registration,
        dry_run=False,
    )

    assert report.status == "blocked"
    assert report.missing_fields == ("version_endpoint",)
    assert report.config is None


def test_acceptance_prefix_and_cleanup_are_scoped_to_exact_run() -> None:
    assert acceptance_data_prefix("run-123") == "codex-run-123"
    assert cleanup_matches_run("codex-run-123", "run-123")
    assert cleanup_matches_run("codex-run-123--item-1", "run-123")
    assert not cleanup_matches_run("codex-run-123-item-1", "run-123")
    assert not cleanup_matches_run("codex-run-1234", "run-123")
    assert not cleanup_matches_run("codex-run-1234-item", "run-123")
    with pytest.raises(ValueError, match="run ID"):
        acceptance_data_prefix("../run")
    with pytest.raises(ValueError, match="run ID"):
        acceptance_data_prefix("run--123")


class FakeResponse:
    def __init__(self, body: object) -> None:
        self._body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self._body


def test_http_client_uses_get_ten_second_timeout_and_three_attempts() -> None:
    calls: list[tuple[str, str, float]] = []
    sleeps: list[float] = []

    def opener(request, timeout):
        calls.append((request.get_method(), request.full_url, timeout))
        if len(calls) < 3:
            raise URLError("temporary")
        return FakeResponse({"ok": True})

    client = ReadOnlyHttpClient(opener=opener, sleep=sleeps.append)

    assert client.get_json("https://example.invalid/version") == {"ok": True}
    assert calls == [
        ("GET", "https://example.invalid/version", 10.0),
        ("GET", "https://example.invalid/version", 10.0),
        ("GET", "https://example.invalid/version", 10.0),
    ]
    assert sleeps == [1.0, 2.0]


def test_http_client_exposes_no_non_get_request() -> None:
    client = ReadOnlyHttpClient(
        opener=lambda *_args, **_kwargs: FakeResponse({}),
    )

    with pytest.raises(HttpMethodNotAllowed):
        client.request_json("POST", "https://example.invalid/version")
