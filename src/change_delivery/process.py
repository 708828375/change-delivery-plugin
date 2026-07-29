from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import traceback

from pydantic import Field

from change_delivery.domain import CommandSpec, FrozenModel
from change_delivery.errors import DeliveryError
from change_delivery.redaction import redact_text


SecretProvider = Callable[[tuple[str, ...]], Mapping[str, str]]
FixedEnvironmentProvider = Callable[[tuple[str, ...]], Mapping[str, str]]


class ProcessResult(FrozenModel):
    command_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int = Field(ge=0)


class ProcessTimeoutError(DeliveryError):
    def __init__(
        self,
        command_id: str,
        stdout: str,
        stderr: str,
        timeout_seconds: int = 7200,
    ) -> None:
        self.command_id = command_id
        self.stdout = stdout
        self.stderr = stderr
        self.timeout_seconds = timeout_seconds
        super().__init__(f"command {command_id} timed out")


class ProcessStartError(DeliveryError):
    def __init__(self, command_id: str) -> None:
        self.command_id = command_id
        super().__init__(f"command {command_id} could not be started")


ProcessLaunchError = ProcessStartError


@dataclass(frozen=True)
class _ProcessFailure:
    kind: str
    stdout: str = ""
    stderr: str = ""


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


def _validate_environment_mapping(environment: Mapping[str, str]) -> None:
    for name, value in environment.items():
        _validate_environment_entry(name, value)


def _environment_name(name: str) -> str:
    return name.casefold() if os.name == "nt" else name


def _safe_base_environment(env_names: tuple[str, ...]) -> dict[str, str]:
    allowed_names = (
        {"systemroot", "windir", "path", "pathext", "temp", "tmp"}
        if os.name == "nt"
        else {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"}
    )
    requested_names = {_environment_name(name) for name in env_names}
    return {
        name: value
        for name, value in os.environ.items()
        if _environment_name(name) in allowed_names
        and _environment_name(name) not in requested_names
    }


class _WindowsJob:
    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _BASIC_LIMITS(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _EXTENDED_LIMITS(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC_LIMITS),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (
            wintypes.HANDLE,
            wintypes.HANDLE,
        )
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        limits = _EXTENDED_LIMITS()
        limits.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            handle,
            9,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error, "SetInformationJobObject failed")
        self._ctypes = ctypes
        self._kernel32 = kernel32
        self._ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        self._ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
        self._ntdll.NtResumeProcess.restype = ctypes.c_long
        self.handle = handle

    def bind_and_resume(self, process: subprocess.Popen[str]) -> None:
        process_handle = process._handle  # type: ignore[attr-defined]
        if not self._kernel32.AssignProcessToJobObject(
            self.handle,
            process_handle,
        ):
            raise OSError(
                self._ctypes.get_last_error(),
                "AssignProcessToJobObject failed",
            )
        status = self._ntdll.NtResumeProcess(process_handle)
        if status < 0:
            raise OSError(status, "NtResumeProcess failed")

    def terminate(self) -> None:
        if not self._kernel32.TerminateJobObject(self.handle, 1):
            error = self._ctypes.get_last_error()
            raise OSError(error, "TerminateJobObject failed")

    def close(self) -> None:
        if self.handle:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


def _terminate_posix_tree(process: subprocess.Popen[str]) -> None:
    import signal

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    # The direct process exiting does not prove that descendants honored TERM.
    # Always issue KILL to the original process group after the grace period.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_process_tree(
    argv: list[str],
    **kwargs: object,
) -> subprocess.CompletedProcess[str]:
    timeout = kwargs.pop("timeout")
    capture_output = bool(kwargs.pop("capture_output", False))
    kwargs.pop("check", None)
    if capture_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    input_text = kwargs.pop("input", None)
    stdin = kwargs.pop("stdin", None)
    if input_text is not None:
        stdin = subprocess.PIPE
    job: _WindowsJob | None = None
    if os.name == "nt":
        job = _WindowsJob()
        kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | 0x4
    else:
        kwargs["start_new_session"] = True
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(argv, stdin=stdin, **kwargs)
        if job is not None:
            try:
                job.bind_and_resume(process)
            except BaseException:
                process.kill()
                process.wait()
                raise
        try:
            stdout, stderr = process.communicate(input=input_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            if job is not None:
                job.terminate()
            else:
                _terminate_posix_tree(process)
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(
                argv,
                timeout,
                output=stdout,
                stderr=stderr,
            ) from None
        return subprocess.CompletedProcess(
            argv,
            process.returncode,
            stdout,
            stderr,
        )
    finally:
        if process is not None:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        if job is not None:
            job.close()


def _invoke_process(
    spec: CommandSpec,
    workdir: Path,
    child_environment: dict[str, str],
    secrets: Mapping[str, str],
    stdin_text: str | None,
) -> subprocess.CompletedProcess[str] | _ProcessFailure:
    try:
        kwargs: dict[str, object] = {
            "cwd": str(workdir),
            "env": child_environment,
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": spec.timeout_seconds,
            "shell": False,
            "check": False,
        }
        if stdin_text is None:
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            kwargs["input"] = stdin_text
        return _run_process_tree(
            [spec.executable, *spec.args],
            **kwargs,
        )
    except subprocess.TimeoutExpired as raw_error:
        failure = _ProcessFailure(
            kind="timeout",
            stdout=redact_text(_as_text(raw_error.output), secrets),
            stderr=redact_text(_as_text(raw_error.stderr), secrets),
        )
        raw_traceback = raw_error.__traceback__
        raw_error.__traceback__ = None
        if raw_traceback is not None:
            traceback.clear_frames(raw_traceback)
        del raw_traceback
        del raw_error
        return failure
    except OSError as raw_error:
        failure = _ProcessFailure(kind="start")
        raw_traceback = raw_error.__traceback__
        raw_error.__traceback__ = None
        if raw_traceback is not None:
            traceback.clear_frames(raw_traceback)
        del raw_traceback
        del raw_error
        return failure


class ProcessRunner:
    """Run direct argv with isolated environment and redacted secret output.

    ``fixed_environment_provider`` is only for non-secret, process-control
    constants owned by the caller. Project credentials must be supplied through
    ``secret_provider`` so their values are redacted from all captured output.
    """

    def __init__(
        self,
        secret_provider: SecretProvider,
        *,
        fixed_environment_provider: FixedEnvironmentProvider | None = None,
        start_time_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._secret_provider = secret_provider
        self._fixed_environment_provider = (
            fixed_environment_provider or (lambda names: {})
        )
        self._start_time_ns = start_time_ns
        self._monotonic = monotonic

    def run(
        self,
        spec: CommandSpec,
        *,
        stdin_text: str | None = None,
        fixed_environment: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        if stdin_text is not None and (
            not isinstance(stdin_text, str) or "\x00" in stdin_text
        ):
            raise ValueError("invalid stdin text")
        workdir = self._resolve_workdir(spec.workdir)
        start_time_ns = self._start_time_ns()
        command_id = self._command_id(spec, workdir, start_time_ns)
        secrets = self._requested_secrets(spec.env_names)
        environment = self._requested_environment(spec.env_names)
        dynamic_environment = dict(fixed_environment or {})
        _validate_environment_mapping(dynamic_environment)
        child_environment = _safe_base_environment(spec.env_names)
        child_environment.update(environment)
        child_environment.update(dynamic_environment)
        child_environment.update(secrets)
        started = self._monotonic()

        outcome = _invoke_process(
            spec,
            workdir,
            child_environment,
            secrets,
            stdin_text,
        )
        duration_ms = max(0, round((self._monotonic() - started) * 1000))
        if isinstance(outcome, _ProcessFailure):
            failure_kind = outcome.kind
            safe_stdout = outcome.stdout
            safe_stderr = outcome.stderr
            timeout_seconds = spec.timeout_seconds
            del outcome
            del child_environment
            del dynamic_environment
            del environment
            del secrets
            del stdin_text
            del fixed_environment
            del spec
            del self
            if failure_kind == "timeout":
                raise ProcessTimeoutError(
                    command_id,
                    safe_stdout,
                    safe_stderr,
                    timeout_seconds,
                )
            raise ProcessStartError(command_id)

        exit_code = outcome.returncode
        safe_stdout = redact_text(_as_text(outcome.stdout), secrets)
        safe_stderr = redact_text(_as_text(outcome.stderr), secrets)
        del outcome
        del child_environment
        del dynamic_environment
        del environment
        del secrets
        del stdin_text
        del fixed_environment
        return ProcessResult(
            command_id=command_id,
            exit_code=exit_code,
            stdout=safe_stdout,
            stderr=safe_stderr,
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
        provided_by_name = {
            _environment_name(name): value
            for name, value in provided.items()
        }
        requested: dict[str, str] = {}
        for name in env_names:
            normalized_name = _environment_name(name)
            if normalized_name not in provided_by_name:
                continue
            value = provided_by_name[normalized_name]
            _validate_environment_entry(name, value)
            requested[name] = value
        return requested

    def _requested_environment(self, env_names: tuple[str, ...]) -> dict[str, str]:
        for name in env_names:
            _validate_environment_entry(name, "")
        provided = self._fixed_environment_provider(env_names)
        for name, value in provided.items():
            _validate_environment_entry(name, value)
        provided_by_name = {
            _environment_name(name): value
            for name, value in provided.items()
        }
        requested: dict[str, str] = {}
        for name in env_names:
            normalized_name = _environment_name(name)
            if normalized_name not in provided_by_name:
                continue
            value = provided_by_name[normalized_name]
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
