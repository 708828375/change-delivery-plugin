from __future__ import annotations

import json
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from change_delivery.adapters.git import GitDiscoveryAttestation, RepositoryIdentity
from change_delivery.adapters.http import (
    HttpMethodNotAllowed,
    HttpReadError,
    ReadOnlyHttpClient,
)
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
        self.attested_repo_path = config.repo_path
        self.remote = config.remote
        self.base_branch = config.base_branch
        self.test_branch = config.test_branch
        self.remote_fetch_url = config.remote_fetch_url
        self.remote_push_url = config.remote_push_url
        self.read_calls: list[tuple[str, tuple[str, ...]]] = []
        self.write_calls: list[object] = []

    def discovery_attestation(
        self,
        remote: str,
        branches: tuple[str, ...],
    ) -> GitDiscoveryAttestation:
        self.read_calls.append((remote, branches))
        return GitDiscoveryAttestation(
            repository=RepositoryIdentity(
                canonical_path=self.attested_repo_path,
                device=1,
                inode=2,
            ),
            remote=remote,
            remote_fetch_url=self.remote_fetch_url,
            remote_push_url=self.remote_push_url,
            branch_shas={
                "develop": DEVELOP_SHA,
                "test": TEST_SHA,
            },
        )


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


def test_project_dry_run_requires_literal_true(fixture_config) -> None:
    adapter = ExampleProjectAdapter(
        fixture_config,
        git=FakeGit(fixture_config),
        github=FakeGitHub(),
        http=FakeHttp({"git_sha": TEST_SHA}),
    )

    with pytest.raises(ValueError, match="dry_run"):
        adapter.discover(dry_run=1)
    with pytest.raises(TypeError, match="dry_run"):
        discover_registration(
            fixture_config.model_dump(mode="python"),
            dry_run="false",
        )


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


def test_discovery_rejects_git_attestation_for_other_repository(
    fixture_config,
) -> None:
    fake_git = FakeGit(fixture_config)
    fake_git.attested_repo_path = Path("C:/other/repository")
    adapter = ExampleProjectAdapter(
        fixture_config,
        git=fake_git,
        github=FakeGitHub(),
        http=FakeHttp({"git_sha": TEST_SHA}),
    )

    with pytest.raises(ValueError, match="repository path"):
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
    def __init__(
        self,
        body: object,
        *,
        url: str = "https://example.invalid/version",
        content_length: int | None = None,
    ) -> None:
        self._body = (
            body if isinstance(body, bytes) else json.dumps(body).encode()
        )
        self._offset = 0
        self._url = url
        self.headers = Message()
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._body) - self._offset
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def geturl(self) -> str:
        return self._url


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


@pytest.mark.parametrize(
    "url",
    (
        "https://127.0.0.1/version",
        "https://169.254.169.254/version",
        "https://[::1]/version",
        "https://example.invalid/version?token=secret",
        "https://example.invalid/version#secret",
    ),
)
def test_http_client_rejects_dangerous_or_secret_bearing_urls(url) -> None:
    client = ReadOnlyHttpClient(opener=lambda *_args, **_kwargs: FakeResponse({}))

    with pytest.raises(ValueError):
        client.get_json(url)


def test_registration_blocks_query_and_fragment_endpoints(fixture_config) -> None:
    for suffix in ("?token=secret", "#token"):
        raw = fixture_config.model_dump(mode="python")
        raw["version_endpoint"] = f"https://version.example.com/version{suffix}"

        report = discover_registration(raw, dry_run=False)

        assert report.status == "blocked"
        assert report.missing_fields == ("version_endpoint",)


def test_registration_blocks_dangerous_ip_literal(fixture_config) -> None:
    raw = fixture_config.model_dump(mode="python")
    raw["version_endpoint"] = "https://169.254.169.254/version"

    report = discover_registration(raw, dry_run=False)

    assert report.status == "blocked"
    assert report.missing_fields == ("version_endpoint",)


def test_http_client_rejects_redirect_without_retry() -> None:
    calls = 0

    def opener(request, timeout):
        nonlocal calls
        calls += 1
        raise HTTPError(
            request.full_url,
            302,
            "Found",
            {"Location": "http://127.0.0.1/version"},
            None,
        )

    with pytest.raises(HttpReadError, match="redirect"):
        ReadOnlyHttpClient(opener=opener, sleep=lambda _delay: None).get_json(
            "https://example.invalid/version"
        )
    assert calls == 1


def test_http_client_rejects_opener_that_followed_redirect() -> None:
    client = ReadOnlyHttpClient(
        opener=lambda *_args, **_kwargs: FakeResponse(
            {"git_sha": TEST_SHA},
            url="http://127.0.0.1/version",
        )
    )

    with pytest.raises(HttpReadError, match="redirect"):
        client.get_json("https://example.invalid/version")


def test_http_client_rejects_declared_and_streamed_oversize_bodies() -> None:
    too_large = 65_537
    declared = ReadOnlyHttpClient(
        opener=lambda *_args, **_kwargs: FakeResponse(
            {},
            content_length=too_large,
        )
    )
    streamed = ReadOnlyHttpClient(
        opener=lambda *_args, **_kwargs: FakeResponse(b"x" * too_large)
    )

    with pytest.raises(HttpReadError, match="too large"):
        declared.get_json("https://example.invalid/version")
    with pytest.raises(HttpReadError, match="too large"):
        streamed.get_json("https://example.invalid/version")


def test_http_client_enforces_total_deadline_while_streaming() -> None:
    ticks = iter((0.0, 0.0, 0.0, 11.0))
    client = ReadOnlyHttpClient(
        opener=lambda *_args, **_kwargs: FakeResponse(b"{}"),
        monotonic=lambda: next(ticks),
    )

    with pytest.raises(HttpReadError, match="deadline"):
        client.get_json("https://example.invalid/version")


@pytest.mark.parametrize("status", (401, 403, 404))
def test_http_client_does_not_retry_permanent_http_errors(status) -> None:
    calls = 0

    def opener(request, timeout):
        nonlocal calls
        calls += 1
        raise HTTPError(request.full_url, status, "failure", {}, None)

    with pytest.raises(HttpReadError):
        ReadOnlyHttpClient(opener=opener).get_json(
            "https://example.invalid/version"
        )
    assert calls == 1


def test_http_client_retries_retryable_status_and_caps_retry_after() -> None:
    calls = 0
    sleeps = []

    def opener(request, timeout):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise HTTPError(
                request.full_url,
                429,
                "busy",
                {"Retry-After": "999"},
                None,
            )
        return FakeResponse({"ok": True})

    client = ReadOnlyHttpClient(opener=opener, sleep=sleeps.append)
    assert client.get_json("https://example.invalid/version") == {"ok": True}
    assert sleeps == [5.0, 5.0]
