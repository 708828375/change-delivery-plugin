from __future__ import annotations

import json
import multiprocessing
from email.message import Message
from io import StringIO
from pathlib import Path
import time
from urllib.error import URLError

import pytest

from change_delivery.adapters.git import GitDiscoveryAttestation, RepositoryIdentity
from change_delivery.adapters.http import (
    AddressResolver,
    HttpMethodNotAllowed,
    HttpReadError,
    MultiprocessAddressResolver,
    PinnedHttpsTransport,
    ReadOnlyHttpClient,
    SecureHttpTransport,
    frozen_address_attestations,
    validate_public_addresses,
)
from change_delivery.adapters.project import (
    ExampleProjectAdapter,
    acceptance_data_prefix,
    cleanup_matches_run,
    discover_registration,
)
from change_delivery.cli import CliServices, main
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


def test_cli_start_dry_run_returns_remote_and_version_report_without_writes(
    fixture_config,
    tmp_path: Path,
) -> None:
    requirement = tmp_path / "requirement.md"
    requirement.write_text("read-only discovery", encoding="utf-8")
    fake_http = FakeHttp({"git_sha": TEST_SHA.upper()})
    fake_git = FakeGit(fixture_config)
    fake_github = FakeGitHub()
    adapter = ExampleProjectAdapter(
        fixture_config,
        git=fake_git,
        github=fake_github,
        http=fake_http,
        file_exists=lambda _path: False,
    )
    factory_calls = []

    def discovery_factory(config):
        factory_calls.append(config)
        return adapter

    services = CliServices(
        plugin_root=tmp_path / "runtime",
        project_loader=lambda _project_id: (fixture_config, "c" * 64),
        discovery_factory=discovery_factory,
    )
    stdout, stderr = StringIO(), StringIO()

    result = main(
        [
            "start",
            "--project",
            "example-backend",
            "--requirement-file",
            str(requirement),
            "--dry-run",
            "--json",
        ],
        services=services,
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 0, stderr.getvalue()
    payload = json.loads(stdout.getvalue())
    assert payload == {
        "ok": True,
        "dry_run": True,
        "status": "passed",
        "project_id": "example-backend",
        "repo_path": "C:\\Huamei\\projects\\code\\example_backend",
        "remote": "origin",
        "base_branch": "develop",
        "base_sha": DEVELOP_SHA,
        "test_branch": "test",
        "test_sha": TEST_SHA,
        "version_endpoint": "https://example.test.invalid/version",
        "version_sha_field": "git_sha",
        "version_probe": {
            "status": "passed",
            "reason": "deployed SHA matches origin/test",
            "deployed_sha": TEST_SHA,
        },
    }
    assert factory_calls == [fixture_config]
    assert fake_git.read_calls == [("origin", ("develop", "test"))]
    assert fake_git.write_calls == []
    assert fake_github.write_calls == []
    assert fake_http.calls == [
        ("GET", "https://example.test.invalid/version")
    ]
    runtime = tmp_path / "runtime"
    assert not (runtime / "state.sqlite3").exists()
    assert not (runtime / "runs").exists()
    assert not (runtime / "worktrees").exists()


@pytest.mark.parametrize(
    ("body", "reason_fragment"),
    (
        ({}, "40 hexadecimal"),
        ({"git_sha": "short"}, "40 hexadecimal"),
        ({"git_sha": DEVELOP_SHA}, "does not match origin/test"),
    ),
)
def test_cli_start_dry_run_blocks_bad_version_evidence_without_writes(
    fixture_config,
    tmp_path: Path,
    body: object,
    reason_fragment: str,
) -> None:
    requirement = tmp_path / "requirement.md"
    requirement.write_text("read-only discovery", encoding="utf-8")
    fake_http = FakeHttp(body)
    fake_git = FakeGit(fixture_config)
    fake_github = FakeGitHub()
    adapter = ExampleProjectAdapter(
        fixture_config,
        git=fake_git,
        github=fake_github,
        http=fake_http,
        file_exists=lambda _path: False,
    )
    services = CliServices(
        plugin_root=tmp_path / "runtime",
        project_loader=lambda _project_id: (fixture_config, "c" * 64),
        discovery_factory=lambda _config: adapter,
    )
    stdout, stderr = StringIO(), StringIO()

    result = main(
        [
            "start",
            "--project",
            "example-backend",
            "--requirement-file",
            str(requirement),
            "--dry-run",
            "--json",
        ],
        services=services,
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 2
    assert stdout.getvalue() == ""
    payload = json.loads(stderr.getvalue())
    assert payload["ok"] is False
    assert payload["dry_run"] is True
    assert payload["status"] == "blocked"
    assert payload["base_sha"] == DEVELOP_SHA
    assert payload["test_sha"] == TEST_SHA
    assert payload["version_probe"]["status"] == "blocked"
    assert reason_fragment in payload["version_probe"]["reason"]
    assert fake_git.write_calls == []
    assert fake_github.write_calls == []
    assert not (tmp_path / "runtime" / "state.sqlite3").exists()
    assert not (tmp_path / "runtime" / "runs").exists()


def test_cli_start_dry_run_refuses_production_enabled_config_before_io(
    fixture_config,
    tmp_path: Path,
) -> None:
    requirement = tmp_path / "requirement.md"
    requirement.write_text("read-only discovery", encoding="utf-8")
    production_config = fixture_config.model_copy(
        update={"production_enabled": True}
    )
    factory_calls = []
    services = CliServices(
        plugin_root=tmp_path / "runtime",
        project_loader=lambda _project_id: (production_config, "c" * 64),
        discovery_factory=lambda config: factory_calls.append(config),
    )
    stdout, stderr = StringIO(), StringIO()

    result = main(
        [
            "start",
            "--project",
            "example-backend",
            "--requirement-file",
            str(requirement),
            "--dry-run",
            "--json",
        ],
        services=services,
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 2
    assert stdout.getvalue() == ""
    payload = json.loads(stderr.getvalue())
    assert payload["status"] == "blocked"
    assert "production" in payload["version_probe"]["reason"]
    assert factory_calls == []


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
        status: int = 200,
        retry_after: str | None = None,
    ) -> None:
        self._body = (
            body if isinstance(body, bytes) else json.dumps(body).encode()
        )
        self._offset = 0
        self._url = url
        self.headers = Message()
        self.status = status
        self.timeout_calls: list[float] = []
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        if retry_after is not None:
            self.headers["Retry-After"] = retry_after

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

    def read1(self, size: int = -1) -> bytes:
        return self.read(size)

    def geturl(self) -> str:
        return self._url

    def set_read_timeout(self, timeout: float) -> None:
        self.timeout_calls.append(timeout)


class FakeTransport(SecureHttpTransport):
    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url: str, *, timeout: float):
        self.calls.append((url, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeResolver(AddressResolver):
    def __init__(self, answers) -> None:
        self.answers = tuple(answers)
        self.calls = []

    def resolve(self, hostname: str, port: int, *, deadline: float):
        self.calls.append((hostname, port, deadline))
        return self.answers


def slow_dns_worker(_hostname: str, _port: int):
    time.sleep(1)
    return ("93.184.216.34",)


def slow_exchange_worker(_url: str, _timeout: float):
    time.sleep(1)
    return None


@pytest.mark.parametrize(
    "hostname,addresses",
    (
        ("localhost", ("127.0.0.1",)),
        ("127.0.0.1.nip.io", ("127.0.0.1",)),
        ("mixed.example", ("93.184.216.34", "169.254.169.254")),
    ),
)
def test_pinned_transport_rejects_any_non_public_resolution(
    hostname,
    addresses,
) -> None:
    with pytest.raises(HttpReadError, match="public"):
        validate_public_addresses(addresses)


def test_pinned_transport_resolves_once_and_connects_to_frozen_ip() -> None:
    attestations = frozen_address_attestations(
        "https://example.com/version",
        ("93.184.216.34",),
    )

    assert attestations[0].hostname == "example.com"
    assert attestations[0].pinned_ip == "93.184.216.34"
    assert attestations[0].addresses == ("93.184.216.34",)


def test_pinned_transport_falls_back_without_resolving_again() -> None:
    attestations = frozen_address_attestations(
        "https://example.com/version",
        ("93.184.216.34", "93.184.216.35"),
    )

    assert [item.pinned_ip for item in attestations] == [
        "93.184.216.34",
        "93.184.216.35",
    ]
    assert all(
        item.addresses == ("93.184.216.34", "93.184.216.35")
        for item in attestations
    )


def test_secure_exchange_process_is_reclaimed_at_header_deadline() -> None:
    transport = PinnedHttpsTransport(worker=slow_exchange_worker)
    before = {child.pid for child in multiprocessing.active_children()}
    started = time.monotonic()

    with pytest.raises(HttpReadError, match="deadline"):
        transport.get("https://example.com/version", timeout=0.05)

    elapsed = time.monotonic() - started
    assert 0.04 <= elapsed < 0.5
    assert {child.pid for child in multiprocessing.active_children()} == before


def test_multiprocess_dns_is_terminated_at_hard_deadline() -> None:
    resolver = MultiprocessAddressResolver(worker=slow_dns_worker)
    before = {child.pid for child in multiprocessing.active_children()}
    started = time.monotonic()

    with pytest.raises(HttpReadError, match="deadline"):
        resolver.resolve(
            "example.com",
            443,
            deadline=started + 0.05,
        )

    elapsed = time.monotonic() - started
    assert 0.04 <= elapsed < 0.5
    assert {child.pid for child in multiprocessing.active_children()} == before


def test_http_client_uses_get_ten_second_timeout_and_three_attempts() -> None:
    sleeps: list[float] = []
    transport = FakeTransport(
        (URLError("temporary"), URLError("temporary"), FakeResponse({"ok": True}))
    )
    client = ReadOnlyHttpClient(
        transport=transport,
        sleep=sleeps.append,
        monotonic=lambda: 0.0,
    )

    assert client.get_json("https://example.invalid/version") == {"ok": True}
    assert transport.calls == [
        ("https://example.invalid/version", 10.0),
    ] * 3
    assert sleeps == [1.0, 2.0]


def test_http_client_exposes_no_non_get_request() -> None:
    client = ReadOnlyHttpClient(
        transport=FakeTransport((FakeResponse({}),)),
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
    client = ReadOnlyHttpClient(
        transport=FakeTransport((FakeResponse({}),))
    )

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
    transport = FakeTransport((FakeResponse({}, status=302),))

    with pytest.raises(HttpReadError, match="redirect"):
        ReadOnlyHttpClient(transport=transport).get_json(
            "https://example.invalid/version"
        )
    assert len(transport.calls) == 1


def test_http_client_rejects_opener_that_followed_redirect() -> None:
    client = ReadOnlyHttpClient(
        transport=FakeTransport(
            (
                FakeResponse(
                    {"git_sha": TEST_SHA},
                    url="http://127.0.0.1/version",
                ),
            )
        )
    )

    with pytest.raises(HttpReadError, match="redirect"):
        client.get_json("https://example.invalid/version")


def test_http_client_rejects_declared_and_streamed_oversize_bodies() -> None:
    too_large = 65_537
    declared = ReadOnlyHttpClient(
        transport=FakeTransport(
            (FakeResponse({}, content_length=too_large),)
        )
    )
    streamed = ReadOnlyHttpClient(
        transport=FakeTransport((FakeResponse(b"x" * too_large),))
    )

    with pytest.raises(HttpReadError, match="too large"):
        declared.get_json("https://example.invalid/version")
    with pytest.raises(HttpReadError, match="too large"):
        streamed.get_json("https://example.invalid/version")


def test_http_client_enforces_total_deadline_while_streaming() -> None:
    ticks = iter((0.0, 0.0, 0.0, 11.0))
    client = ReadOnlyHttpClient(
        transport=FakeTransport((FakeResponse(b"{}"),)),
        monotonic=lambda: next(ticks),
    )

    with pytest.raises(HttpReadError, match="deadline"):
        client.get_json("https://example.invalid/version")


def test_http_client_fails_closed_without_read_timeout_control() -> None:
    response = FakeResponse(b"{}")
    response.set_read_timeout = None
    client = ReadOnlyHttpClient(transport=FakeTransport((response,)))

    with pytest.raises(HttpReadError, match="enforce read deadline"):
        client.get_json("https://example.invalid/version")


def test_http_client_fails_closed_without_single_read_contract() -> None:
    response = FakeResponse(b"{}")
    response.read1 = None
    client = ReadOnlyHttpClient(transport=FakeTransport((response,)))

    with pytest.raises(HttpReadError, match="single-read"):
        client.get_json("https://example.invalid/version")


def test_http_client_slow_stream_stops_near_wall_clock_deadline() -> None:
    class SlowResponse(FakeResponse):
        def __init__(self):
            super().__init__(b"{}")
            self.remaining = 0.0

        def set_read_timeout(self, timeout: float) -> None:
            self.remaining = timeout

        def read(self, size: int = -1) -> bytes:
            time.sleep(self.remaining)
            raise TimeoutError("simulated socket deadline")

    client = ReadOnlyHttpClient(
        timeout_seconds=0.05,
        transport=FakeTransport((SlowResponse(),)),
    )
    started = time.monotonic()
    with pytest.raises(HttpReadError, match="deadline"):
        client.get_json("https://example.invalid/version")
    elapsed = time.monotonic() - started
    assert 0.04 <= elapsed < 0.2


@pytest.mark.parametrize("status", (401, 403, 404))
def test_http_client_does_not_retry_permanent_http_errors(status) -> None:
    transport = FakeTransport((FakeResponse({}, status=status),))

    with pytest.raises(HttpReadError):
        ReadOnlyHttpClient(transport=transport).get_json(
            "https://example.invalid/version"
        )
    assert len(transport.calls) == 1


def test_http_client_retries_retryable_status_and_caps_retry_after() -> None:
    sleeps = []
    transport = FakeTransport(
        (
            FakeResponse({}, status=429, retry_after="999"),
            FakeResponse({}, status=429, retry_after="999"),
            FakeResponse({"ok": True}),
        )
    )
    client = ReadOnlyHttpClient(
        transport=transport,
        sleep=sleeps.append,
        monotonic=lambda: 0.0,
    )
    assert client.get_json("https://example.invalid/version") == {"ok": True}
    assert sleeps == [5.0, 5.0]
