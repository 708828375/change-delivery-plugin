from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from typing import Mapping

from pydantic import ValidationError
import pytest

from change_delivery.domain import CommandSpec
from change_delivery.process import (
    ProcessLaunchError,
    ProcessResult,
    ProcessRunner,
    ProcessStartError,
    ProcessTimeoutError,
)


def command_spec(workdir: Path, **overrides: object) -> CommandSpec:
    data: dict[str, object] = {
        "executable": sys.executable,
        "args": ("-c", "print('ok')"),
        "workdir": workdir,
        "timeout_seconds": 12,
        "env_names": ("DELIVERY_TEST_SECRET",),
    }
    data.update(overrides)
    return CommandSpec.model_validate(data)


def test_process_result_is_frozen_and_forbids_extra_fields() -> None:
    result = ProcessResult(
        command_id="0123456789abcdef",
        exit_code=0,
        stdout="ok",
        stderr="",
        duration_ms=1,
    )

    with pytest.raises(ValidationError):
        result.stdout = "changed"
    with pytest.raises(ValidationError):
        ProcessResult(
            command_id="0123456789abcdef",
            exit_code=0,
            stdout="",
            stderr="",
            duration_ms=0,
            unexpected=True,
        )


@pytest.mark.parametrize("command_id", ["A" * 16, "a" * 15, "g" * 16])
def test_process_result_rejects_invalid_command_id(command_id: str) -> None:
    with pytest.raises(ValidationError):
        ProcessResult(
            command_id=command_id,
            exit_code=0,
            stdout="",
            stderr="",
            duration_ms=0,
        )


def test_runner_uses_direct_argv_isolated_env_and_redacts_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_value = os.environ.get("DELIVERY_TEST_SECRET")
    provider_calls: list[tuple[str, ...]] = []
    captured: dict[str, object] = {}

    def provider(names: tuple[str, ...]) -> Mapping[str, str]:
        provider_calls.append(names)
        return {
            "DELIVERY_TEST_SECRET": "s3cr.et[值]",
            "UNREQUESTED_SECRET": "must-not-enter-child",
        }

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            argv,
            0,
            "value=s3cr.et[值]\nAuthorization: Bearer other",
            "Cookie: a=1; secret=s3cr.et[值]",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    ticks = iter((10.0, 10.025))
    runner = ProcessRunner(provider, start_time_ns=lambda: 123, monotonic=lambda: next(ticks))

    result = runner.run(
        command_spec(
            tmp_path,
            args=("-c", "print('a;b|c')"),
        )
    )

    assert provider_calls == [("DELIVERY_TEST_SECRET",)]
    assert captured["argv"] == [sys.executable, "-c", "print('a;b|c')"]
    assert captured["shell"] is False
    assert captured["check"] is False
    assert captured["capture_output"] is True
    assert captured["text"] is True
    assert captured["encoding"] == "utf-8"
    assert captured["errors"] == "replace"
    assert captured["timeout"] == 12
    assert captured["cwd"] == str(tmp_path.resolve())
    assert captured["stdin"] is subprocess.DEVNULL
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert child_env["DELIVERY_TEST_SECRET"] == "s3cr.et[值]"
    assert "UNREQUESTED_SECRET" not in child_env
    assert os.environ.get("DELIVERY_TEST_SECRET") == parent_value
    assert "s3cr.et[值]" not in result.stdout
    assert result.stdout == "value=***\nAuthorization: Bearer ***"
    assert result.stderr == "Cookie: ***"
    assert result.duration_ms == 25


def test_runner_passes_prompt_only_through_stdin_and_dynamic_fixed_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_prompt = "prompt-only-secret"
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = ProcessRunner(lambda names: {}, start_time_ns=lambda: 123).run(
        command_spec(tmp_path, args=("exec", "-"), env_names=()),
        stdin_text=secret_prompt,
        fixed_environment={"CODEX_HOME": str(tmp_path / "isolated-home")},
    )

    assert captured["input"] == secret_prompt
    assert "stdin" not in captured
    assert captured["env"]["CODEX_HOME"] == str(tmp_path / "isolated-home")
    assert secret_prompt not in repr(captured["argv"])
    assert secret_prompt not in result.command_id


@pytest.mark.parametrize(
    "fixed_environment",
    [
        {"BAD=NAME": "value"},
        {"BAD\x00NAME": "value"},
        {"CODEX_HOME": "bad\x00value"},
    ],
)
def test_dynamic_fixed_environment_is_strict_and_does_not_leak(
    tmp_path: Path,
    fixed_environment: Mapping[str, str],
) -> None:
    runner = ProcessRunner(lambda names: {})
    with pytest.raises(ValueError) as caught:
        runner.run(
            command_spec(tmp_path, env_names=()),
            fixed_environment=fixed_environment,
        )
    assert "bad\x00value" not in repr(caught.value)


def test_runner_uses_minimal_base_environment_and_never_inherits_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNREQUESTED_SECRET", "parent-unrequested")
    monkeypatch.setenv("GH_TOKEN", "parent-gh")
    monkeypatch.setenv("OPENAI_API_KEY", "parent-openai")
    monkeypatch.setenv("USERPROFILE", "C:/sensitive-profile")
    monkeypatch.setenv("HOME", "C:/sensitive-home")
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = ProcessRunner(
        lambda names: {"DELIVERY_TEST_SECRET": "explicit-secret"},
    )

    runner.run(command_spec(tmp_path))

    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert child_env["DELIVERY_TEST_SECRET"] == "explicit-secret"
    assert "UNREQUESTED_SECRET" not in child_env
    assert "GH_TOKEN" not in child_env
    assert "OPENAI_API_KEY" not in child_env
    assert "USERPROFILE" not in child_env
    assert "HOME" not in child_env
    allowed = (
        {"systemroot", "windir", "path", "pathext", "temp", "tmp"}
        if os.name == "nt"
        else {"path", "lang", "lc_all", "lc_ctype", "tmpdir"}
    )
    assert {name.casefold() for name in child_env} <= (
        allowed | {"delivery_test_secret"}
    )


def test_missing_provider_value_does_not_fall_back_to_parent_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "parent-path-must-not-fallback")
    monkeypatch.setenv("HOME", "parent-home-must-not-fallback")
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = ProcessRunner(lambda names: {})

    runner.run(command_spec(tmp_path, env_names=("PATH", "HOME")))

    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert "PATH" not in child_env
    assert "HOME" not in child_env


def test_fixed_non_secret_environment_is_injected_without_output_redaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, "mode=never", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = ProcessRunner(
        lambda names: {},
        fixed_environment_provider=lambda names: {"DELIVERY_MODE": "never"},
    )

    result = runner.run(
        command_spec(
            tmp_path,
            env_names=("DELIVERY_MODE",),
        )
    )

    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert child_env["DELIVERY_MODE"] == "never"
    assert result.stdout == "mode=never"


@pytest.mark.parametrize(
    "provided",
    [
        {"BAD=NAME": "value"},
        {"BAD\x00NAME": "value"},
        {"UNREQUESTED": "bad\x00value"},
    ],
)
def test_fixed_environment_validates_every_entry_without_leaking_value(
    tmp_path: Path,
    provided: Mapping[str, str],
) -> None:
    runner = ProcessRunner(
        lambda names: {},
        fixed_environment_provider=lambda names: provided,
    )

    with pytest.raises(ValueError) as caught:
        runner.run(command_spec(tmp_path, env_names=("DELIVERY_MODE",)))

    assert "bad\x00value" not in str(caught.value)
    assert "value" not in str(caught.value)


def test_sensitive_home_can_only_enter_through_explicit_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = ProcessRunner(lambda names: {"HOME": "C:/explicit-home"})

    runner.run(command_spec(tmp_path, env_names=("HOME",)))

    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert child_env["HOME"] == "C:/explicit-home"


def test_runner_executes_real_python_child_and_returns_nonzero(
    tmp_path: Path,
) -> None:
    runner = ProcessRunner(lambda names: {})

    result = runner.run(
        command_spec(
            tmp_path,
            args=(
                "-c",
                "import sys; print('smoke'); print('problem', file=sys.stderr); sys.exit(7)",
            ),
            env_names=(),
        )
    )

    assert result.exit_code == 7
    assert result.stdout.strip() == "smoke"
    assert result.stderr.strip() == "problem"


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_runner_requires_existing_directory_workdir(
    tmp_path: Path,
    kind: str,
) -> None:
    workdir = tmp_path / kind
    if kind == "file":
        workdir.write_text("not a directory", encoding="utf-8")
    runner = ProcessRunner(lambda names: {})

    with pytest.raises(NotADirectoryError):
        runner.run(command_spec(workdir))


@pytest.mark.parametrize(
    "provided",
    [
        {"BAD=NAME": "value"},
        {"BAD\x00NAME": "value"},
        {"DELIVERY_TEST_SECRET": "bad\x00value"},
    ],
)
def test_runner_rejects_invalid_environment_without_leaking_value(
    tmp_path: Path,
    provided: Mapping[str, str],
) -> None:
    runner = ProcessRunner(lambda names: provided)

    with pytest.raises(ValueError) as caught:
        runner.run(command_spec(tmp_path, env_names=tuple(provided)))

    assert "bad\x00value" not in str(caught.value)
    assert "value" not in str(caught.value)


def test_timeout_error_contains_only_redacted_output_and_safe_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "timeout-secret"

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            cmd=[sys.executable, secret],
            timeout=12,
            output=f"partial {secret}".encode(),
            stderr=f"token={secret}".encode(),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = ProcessRunner(
        lambda names: {"DELIVERY_TEST_SECRET": secret},
        start_time_ns=lambda: 123,
    )

    with pytest.raises(ProcessTimeoutError) as caught:
        runner.run(command_spec(tmp_path))

    error = caught.value
    assert len(error.command_id) == 16
    assert error.stdout == "partial ***"
    assert error.stderr == "token=***"
    assert secret not in str(error)
    assert secret not in repr(error)
    assert secret not in repr(error.__dict__)
    assert secret not in repr(error.__context__)


def test_launch_error_does_not_expose_secret_or_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "launch-secret"

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError(f"failed with {secret}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = ProcessRunner(
        lambda names: {"DELIVERY_TEST_SECRET": secret},
        start_time_ns=lambda: 123,
    )

    with pytest.raises(ProcessLaunchError) as caught:
        runner.run(command_spec(tmp_path))

    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert secret not in repr(caught.value.__context__)
    assert caught.value.command_id


@pytest.mark.parametrize("failure_kind", ["timeout", "start"])
def test_safe_process_errors_have_no_raw_chain_or_sensitive_traceback_locals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    secret = f"{failure_kind}-deep-secret"

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if failure_kind == "timeout":
            raise subprocess.TimeoutExpired(
                cmd=[sys.executable, secret],
                timeout=12,
                output=f"partial {secret}",
                stderr=f"api_key={secret}",
            )
        raise OSError(f"start failed with {secret}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = ProcessRunner(
        lambda names: {"DELIVERY_TEST_SECRET": secret},
        start_time_ns=lambda: 123,
    )
    expected_error = (
        ProcessTimeoutError
        if failure_kind == "timeout"
        else ProcessStartError
    )

    with pytest.raises(expected_error) as caught:
        runner.run(
            command_spec(tmp_path, args=("-c", "safe-argv")),
            stdin_text=f"stdin-{secret}",
            fixed_environment={"CODEX_HOME": f"C:/isolated/{secret}"},
        )

    error = caught.value
    assert error.__context__ is None
    assert error.__cause__ is None
    assert secret not in repr(error)
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_code.co_filename.endswith("change_delivery\\process.py"):
            assert secret not in repr(frame.f_locals)
        traceback = traceback.tb_next


def test_runner_validates_requested_environment_name_even_when_missing(
    tmp_path: Path,
) -> None:
    runner = ProcessRunner(lambda names: {})

    with pytest.raises(ValueError):
        runner.run(command_spec(tmp_path, env_names=("BAD=NAME",)))


def test_command_id_has_stable_shape_and_changes_with_start_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
    )
    spec = command_spec(tmp_path, env_names=())

    first = ProcessRunner(lambda names: {}, start_time_ns=lambda: 100).run(spec)
    same = ProcessRunner(lambda names: {}, start_time_ns=lambda: 100).run(spec)
    later = ProcessRunner(lambda names: {}, start_time_ns=lambda: 101).run(spec)

    assert first.command_id == same.command_id
    assert first.command_id != later.command_id
    assert len(first.command_id) == 16
    assert set(first.command_id) <= set("0123456789abcdef")
