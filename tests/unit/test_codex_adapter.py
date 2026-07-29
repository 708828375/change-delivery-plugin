from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from pydantic import ValidationError
import pytest

from change_delivery.adapters.codex import (
    CodexAdapter,
    StageExecutionRequest,
    assert_fix_attempt_allowed,
)
from change_delivery.domain import CommandSpec, StageResult
from change_delivery.errors import BlockedError
from change_delivery.process import ProcessResult


SHA = "a" * 40
EVIDENCE = {
    "evidence_id": "test.result",
    "kind": "test",
    "sha256": "b" * 64,
}


def payload(*, status: str = "passed", extra: object | None = None) -> str:
    value: dict[str, object] = {
        "status": status,
        "summary": "checks complete",
        "evidence": [EVIDENCE] if status == "passed" else [],
        "findings": [],
        "next_action": "continue",
    }
    if extra is not None:
        value["unknown"] = extra
    return json.dumps(value)


def process_result(stdout: str = "", *, exit_code: int = 0) -> ProcessResult:
    return ProcessResult(
        command_id="0123456789abcdef",
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
        duration_ms=1,
    )


class FakeRunner:
    def __init__(self, *outcomes: str | bytes | Exception) -> None:
        self.outcomes = deque(outcomes)
        self.commands: list[CommandSpec] = []

    def run(self, spec: CommandSpec) -> ProcessResult:
        self.commands.append(spec)
        if not self.outcomes:
            raise AssertionError("unexpected Codex invocation")
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        output_index = spec.args.index("-o") + 1
        output_path = Path(spec.args[output_index])
        if isinstance(outcome, bytes):
            output_path.write_bytes(outcome)
            return process_result()
        if outcome.startswith("FILE:"):
            output_path.write_text(outcome.removeprefix("FILE:"), encoding="utf-8")
            return process_result()
        return process_result(outcome)


class BlockingRunner:
    def __init__(self) -> None:
        self.active = 0
        self.maximum = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()

    def run(self, spec: CommandSpec) -> ProcessResult:
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.entered.set()
        self.release.wait(2)
        with self.lock:
            self.active -= 1
        return process_result(payload())


def request(tmp_path: Path, **overrides: object) -> StageExecutionRequest:
    data: dict[str, object] = {
        "run_id": "run-123",
        "stage": "internal-review",
        "instructions": "Review only the approved change.",
        "worktree": tmp_path,
        "head_sha": SHA,
        "approval_scope": "Approved implementation and review.",
        "automation_limit": "Do not push, merge, or deploy.",
        "allowed_actions": ("read files", "run approved checks"),
        "forbidden_actions": ("write files", "use network"),
        "expected_skill": "requesting-code-review",
        "role": "read_only",
        "attempt": 1,
    }
    data.update(overrides)
    return StageExecutionRequest.model_validate(data)


def adapter(
    tmp_path: Path,
    runner: object,
    **overrides: object,
) -> CodexAdapter:
    schema = Path(__file__).parents[2] / "schemas" / "stage-result.schema.json"
    launcher = tmp_path / "vendor" / "codex.exe"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.touch(exist_ok=True)
    values: dict[str, object] = {
        "runner": runner,
        "stage_schema_path": schema,
        "result_root": tmp_path / "results",
        "codex_executable": launcher,
        "limiter_timeout_seconds": 0.05,
    }
    values.update(overrides)
    return CodexAdapter(**values)


def test_request_is_frozen_extra_forbid_and_rejects_untrusted_fields(
    tmp_path: Path,
) -> None:
    value = request(tmp_path)
    with pytest.raises(ValidationError):
        value.stage = "other"
    for changes in (
        {"chat_history": "secret"},
        {"env": {"SECRET": "value"}},
        {"instructions": "bad\x00text"},
        {"approval_scope": "bad\nscope"},
        {"head_sha": "short"},
        {"attempt": 0},
        {"worktree": Path("relative")},
        {"worktree": Path("C:/bad\nworktree")},
    ):
        with pytest.raises(ValidationError):
            StageExecutionRequest.model_validate(
                {**value.model_dump(), **changes}
            )


def test_read_only_exec_uses_exact_safe_flags_and_fixed_prompt(tmp_path: Path) -> None:
    runner = FakeRunner(payload())
    result = adapter(tmp_path, runner).run_stage(request(tmp_path))

    assert result.status == "passed"
    command = runner.commands[0]
    assert command.executable == str((tmp_path / "vendor" / "codex.exe").resolve())
    assert command.args[:9] == (
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--cd",
        str(tmp_path),
        "--output-schema",
        str(Path(__file__).parents[2] / "schemas" / "stage-result.schema.json"),
        "-o",
    )
    assert command.workdir == tmp_path
    assert command.env_names == ("OPENAI_API_KEY", "CODEX_HOME")
    joined = "\n".join(command.args)
    for expected in (
        "run-123",
        "internal-review",
        SHA,
        "Approved implementation and review.",
        "read files",
        "write files",
        "requesting-code-review",
        "read_only",
    ):
        assert expected in joined
    assert "danger-full-access" not in command.args
    assert "--dangerously-bypass-approvals-and-sandbox" not in command.args
    assert "--add-dir" not in command.args


def test_writer_uses_workspace_write_and_output_file(tmp_path: Path) -> None:
    runner = FakeRunner(f"FILE:{payload()}")

    result = adapter(tmp_path, runner).run_stage(
        request(tmp_path, role="writer")
    )

    assert result.status == "passed"
    assert runner.commands[0].args[3] == "workspace-write"


def test_stdout_fallback_is_allowed_but_conflicting_sources_fail_closed(
    tmp_path: Path,
) -> None:
    assert adapter(tmp_path, FakeRunner(payload())).run_stage(
        request(tmp_path)
    ).status == "passed"

    class ConflictRunner(FakeRunner):
        def run(self, spec: CommandSpec) -> ProcessResult:
            self.commands.append(spec)
            path = Path(spec.args[spec.args.index("-o") + 1])
            path.write_text(payload(status="failed"), encoding="utf-8")
            return process_result(payload())

    with pytest.raises(BlockedError, match="conflict"):
        adapter(tmp_path, ConflictRunner()).run_stage(request(tmp_path))


def test_invalid_json_gets_one_read_only_repair_in_isolated_directory(
    tmp_path: Path,
) -> None:
    runner = FakeRunner("{not json", payload())

    result = adapter(tmp_path, runner).run_stage(request(tmp_path))

    assert result.status == "passed"
    assert len(runner.commands) == 2
    repair = runner.commands[1]
    assert repair.args[3] == "read-only"
    repair_workdir = Path(repair.args[repair.args.index("--cd") + 1])
    assert repair_workdir != tmp_path
    assert repair_workdir.is_dir()
    assert tuple(repair_workdir.iterdir()) == ()
    repair_prompt = repair.args[-1]
    assert "{not json" in repair_prompt
    assert "Do not execute tools" in repair_prompt
    assert "Review only the approved change." not in repair_prompt


def test_second_invalid_result_blocks_without_third_call(tmp_path: Path) -> None:
    runner = FakeRunner("{bad", '{"still":"bad"}')

    with pytest.raises(BlockedError, match="format"):
        adapter(tmp_path, runner).run_stage(request(tmp_path))

    assert len(runner.commands) == 2


def test_format_failure_redacts_repair_prompt_and_exception_chain(
    tmp_path: Path,
) -> None:
    secret = "sk-super-secret-value-12345678"
    runner = FakeRunner(f'{{"token":"{secret}"', f'{{"token":"{secret}"')

    with pytest.raises(BlockedError) as caught:
        adapter(tmp_path, runner).run_stage(request(tmp_path))

    assert secret not in runner.commands[1].args[-1]
    error: BaseException | None = caught.value
    while error is not None:
        assert secret not in repr(error)
        error = error.__cause__ or error.__context__


def test_semantically_valid_failed_result_is_not_repaired(tmp_path: Path) -> None:
    runner = FakeRunner(payload(status="failed"))

    result = adapter(tmp_path, runner).run_stage(request(tmp_path))

    assert result.status == "failed"
    assert len(runner.commands) == 1


@pytest.mark.parametrize(
    "bad_output",
    [
        pytest.param(payload(extra=True), id="unknown-field"),
        pytest.param("x" * (1024 * 1024 + 1), id="oversized"),
        pytest.param(b"\xff\xfe", id="invalid-utf8"),
    ],
)
def test_unknown_oversized_or_invalid_utf8_output_fails_safely(
    tmp_path: Path,
    bad_output: str | bytes,
) -> None:
    runner = FakeRunner(bad_output, '{"still":"bad"}')

    with pytest.raises(BlockedError, match="format"):
        adapter(tmp_path, runner).run_stage(request(tmp_path))


@pytest.mark.parametrize(
    "filename",
    [
        "codex.cmd",
        "codex.ps1",
        "codex.bat",
        "wrapper.exe",
        "python.exe",
        "renamed.exe",
    ],
)
def test_script_or_renamed_launcher_is_rejected(
    tmp_path: Path,
    filename: str,
) -> None:
    executable = tmp_path / "untrusted" / filename
    executable.parent.mkdir(exist_ok=True)
    executable.touch()
    with pytest.raises(ValueError):
        adapter(
            tmp_path,
            FakeRunner(),
            codex_executable=executable,
        )


def test_relative_launcher_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        adapter(
            tmp_path,
            FakeRunner(),
            codex_executable=Path("codex.exe"),
        )


def test_launcher_requires_existing_regular_file_named_codex_exe(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing" / "codex.exe"
    with pytest.raises(ValueError):
        adapter(tmp_path, FakeRunner(), codex_executable=missing)

    directory = tmp_path / "directory" / "codex.exe"
    directory.mkdir(parents=True)
    with pytest.raises(ValueError):
        adapter(tmp_path, FakeRunner(), codex_executable=directory)


def test_launcher_accepts_case_insensitive_official_windows_basename(
    tmp_path: Path,
) -> None:
    launcher = tmp_path / "official" / "CoDeX.ExE"
    launcher.parent.mkdir()
    launcher.touch()

    instance = adapter(
        tmp_path,
        FakeRunner(payload()),
        codex_executable=launcher,
    )

    assert instance.run_stage(request(tmp_path)).status == "passed"


def test_launcher_rejects_symlink_even_when_named_codex_exe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "native" / "codex.exe"
    target.parent.mkdir()
    target.touch()
    path_type = type(target)
    original_is_symlink = path_type.is_symlink
    monkeypatch.setattr(
        path_type,
        "is_symlink",
        lambda value: value == target or original_is_symlink(value),
    )

    with pytest.raises(ValueError, match="link|reparse"):
        adapter(tmp_path, FakeRunner(), codex_executable=target)


def test_launcher_rejects_windows_reparse_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "native-reparse" / "codex.exe"
    target.parent.mkdir()
    target.touch()
    path_type = type(target)
    original_lstat = path_type.lstat
    monkeypatch.setattr(
        path_type,
        "lstat",
        lambda value: (
            SimpleNamespace(st_file_attributes=0x400)
            if value == target
            else original_lstat(value)
        ),
    )

    with pytest.raises(ValueError, match="link|reparse"):
        adapter(tmp_path, FakeRunner(), codex_executable=target)


def test_schema_must_exist_match_runtime_model_and_be_draft_valid(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(ValueError):
        adapter(tmp_path, FakeRunner(), stage_schema_path=missing)

    wrong = tmp_path / "wrong.json"
    wrong.write_text('{"type":"object"}', encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        adapter(tmp_path, FakeRunner(), stage_schema_path=wrong)


def test_prompt_never_reads_environment_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPER_SECRET", "must-not-leak")
    runner = FakeRunner(payload())

    adapter(tmp_path, runner).run_stage(request(tmp_path))

    assert "must-not-leak" not in runner.commands[0].args[-1]
    assert "SUPER_SECRET" not in runner.commands[0].args[-1]


def test_result_path_is_unique_and_stays_under_controlled_root(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(payload(), payload())
    instance = adapter(tmp_path, runner)
    instance.run_stage(request(tmp_path))
    instance.run_stage(request(tmp_path))

    paths = [
        Path(command.args[command.args.index("-o") + 1])
        for command in runner.commands
    ]
    assert paths[0] != paths[1]
    assert all(
        path.is_relative_to((tmp_path / "results").resolve()) for path in paths
    )


@pytest.mark.parametrize("existing", [-1, 2, 3])
def test_fix_attempt_limit(existing: int) -> None:
    if existing < 0:
        with pytest.raises(ValueError):
            assert_fix_attempt_allowed("PRRT_valid", existing)
    else:
        with pytest.raises(BlockedError, match="两轮"):
            assert_fix_attempt_allowed("PRRT_valid", existing)


def test_fix_attempt_accepts_valid_thread_with_less_than_two_attempts() -> None:
    assert_fix_attempt_allowed("PRRT_kwDOAA_123", 1)
    with pytest.raises(ValueError):
        assert_fix_attempt_allowed("../thread", 0)


def test_fourth_reader_is_rejected_while_three_are_active(tmp_path: Path) -> None:
    runner = BlockingRunner()
    instance = adapter(tmp_path, runner)
    failures: list[Exception] = []

    def invoke(index: int) -> None:
        try:
            instance.run_stage(
                request(tmp_path, run_id=f"run-{index}", attempt=index)
            )
        except Exception as error:
            failures.append(error)

    threads = [threading.Thread(target=invoke, args=(i,)) for i in (1, 2, 3)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 1
    while runner.maximum < 3 and time.monotonic() < deadline:
        time.sleep(0.005)

    with pytest.raises(BlockedError, match="concurrency"):
        instance.run_stage(request(tmp_path, run_id="run-4"))

    runner.release.set()
    for thread in threads:
        thread.join()
    assert failures == []
    assert runner.maximum == 3


def test_writers_are_mutually_exclusive_and_exception_releases_slot(
    tmp_path: Path,
) -> None:
    blocking = BlockingRunner()
    instance = adapter(tmp_path, blocking)
    thread = threading.Thread(
        target=lambda: instance.run_stage(request(tmp_path, role="writer"))
    )
    thread.start()
    assert blocking.entered.wait(1)
    with pytest.raises(BlockedError, match="concurrency"):
        instance.run_stage(request(tmp_path, role="writer", attempt=2))
    blocking.release.set()
    thread.join()

    failing = adapter(tmp_path, FakeRunner(RuntimeError("safe failure")))
    with pytest.raises(RuntimeError, match="safe failure"):
        failing.run_stage(request(tmp_path, role="writer"))
    succeeding = adapter(tmp_path, FakeRunner(payload()))
    assert succeeding.run_stage(request(tmp_path, role="writer")).status == "passed"
