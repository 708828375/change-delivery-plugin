from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from pydantic import Field

from change_delivery.domain import CommandSpec, FrozenModel
from change_delivery.errors import DeliveryError
from change_delivery.redaction import redact_text


SecretProvider = Callable[[tuple[str, ...]], Mapping[str, str]]


class ProcessResult(FrozenModel):
    command_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int = Field(ge=0)


class ProcessTimeoutError(DeliveryError):
    def __init__(self, command_id: str, stdout: str, stderr: str) -> None:
        self.command_id = command_id
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(f"command {command_id} timed out")


class ProcessLaunchError(DeliveryError):
    def __init__(self, command_id: str) -> None:
        self.command_id = command_id
        super().__init__(f"command {command_id} could not be started")


def _as_text(output: str | bytes | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output


def _validate_environment_entry(name: str, value: str) -> None:
    if not isinstance(name, str) or not name or "=" in name or "\x00" in name:
        raise ValueError("invalid environment entry")
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("invalid environment entry")


class ProcessRunner:
    def __init__(
        self,
        secret_provider: SecretProvider,
        *,
        start_time_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._secret_provider = secret_provider
        self._start_time_ns = start_time_ns
        self._monotonic = monotonic

    def run(self, spec: CommandSpec) -> ProcessResult:
        workdir = self._resolve_workdir(spec.workdir)
        start_time_ns = self._start_time_ns()
        command_id = self._command_id(spec, workdir, start_time_ns)
        secrets = self._requested_secrets(spec.env_names)
        child_environment = os.environ.copy()
        child_environment.update(secrets)
        started = self._monotonic()

        try:
            completed = subprocess.run(
                [spec.executable, *spec.args],
                cwd=str(workdir),
                env=child_environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=spec.timeout_seconds,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            stdout = redact_text(_as_text(error.output), secrets)
            stderr = redact_text(_as_text(error.stderr), secrets)
            error.cmd = [command_id]
            error.output = stdout
            error.stderr = stderr
            error.args = ([command_id], spec.timeout_seconds)
            raise ProcessTimeoutError(command_id, stdout, stderr) from None
        except OSError as error:
            error.args = ("process launch failed",)
            error.filename = None
            error.filename2 = None
            raise ProcessLaunchError(command_id) from None

        duration_ms = max(0, round((self._monotonic() - started) * 1000))
        return ProcessResult(
            command_id=command_id,
            exit_code=completed.returncode,
            stdout=redact_text(_as_text(completed.stdout), secrets),
            stderr=redact_text(_as_text(completed.stderr), secrets),
            duration_ms=duration_ms,
        )

    @staticmethod
    def _resolve_workdir(workdir: Path) -> Path:
        if not workdir.is_dir():
            raise NotADirectoryError("command workdir must be an existing directory")
        return workdir.resolve()

    def _requested_secrets(self, env_names: tuple[str, ...]) -> dict[str, str]:
        for name in env_names:
            _validate_environment_entry(name, "")
        provided = self._secret_provider(env_names)
        requested: dict[str, str] = {}
        for name in env_names:
            if name not in provided:
                continue
            value = provided[name]
            _validate_environment_entry(name, value)
            requested[name] = value
        return requested

    @staticmethod
    def _command_id(
        spec: CommandSpec,
        workdir: Path,
        start_time_ns: int,
    ) -> str:
        identity = json.dumps(
            {
                "executable": spec.executable,
                "args": list(spec.args),
                "workdir": str(workdir),
                "start_time_ns": start_time_ns,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
