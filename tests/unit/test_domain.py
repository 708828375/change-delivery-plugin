from pathlib import Path

import pytest
from pydantic import ValidationError

from change_delivery.domain import CommandSpec, RunState


def test_command_spec_rejects_shell_tokens() -> None:
    with pytest.raises(ValidationError):
        CommandSpec(
            executable="powershell",
            args=["-Command", "git status; rm x"],
            workdir=Path("C:/repo"),
        )


def test_run_state_has_ready_to_merge_terminal_gate() -> None:
    assert RunState.READY_TO_MERGE.value == "READY_TO_MERGE"
    assert RunState.COMPLETED.value == "COMPLETED"


def test_command_spec_preserves_path_workdir() -> None:
    spec = CommandSpec(
        executable="python",
        args=["-m", "pytest"],
        workdir=Path("C:/repo"),
    )

    assert spec.workdir == Path("C:/repo")
    assert isinstance(spec.workdir, Path)
