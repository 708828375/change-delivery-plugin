from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import stat
from typing import Literal, Protocol

from filelock import FileLock, Timeout

from change_delivery.domain import FrozenModel, RunState, StepOutcome
from change_delivery.repository import RunRepository


_FAST_WINDOW = timedelta(minutes=5)
_SLOW_WINDOW = timedelta(minutes=30)
_SLOW_AFTER = timedelta(hours=24)
_STOP_AFTER = timedelta(hours=72)
_INTERVENTION_STATES = {
    RunState.BLOCKED,
    RunState.REVIEW_DECISION_REQUIRED,
}


class OrchestratorPort(Protocol):
    def step(self, run_id: str) -> StepOutcome: ...


class SchedulerResult(FrozenModel):
    status: Literal[
        "processed",
        "blocked",
        "idle",
        "throttled",
        "already_running",
        "waiting_for_intervention",
        "unsafe_lock",
    ]
    processed: int
    run_id: str | None = None
    outcome: StepOutcome | None = None


class Scheduler:
    """Single-instance scheduler that delegates one action to the orchestrator."""

    def __init__(
        self,
        *,
        runtime_root: Path,
        repository: RunRepository,
        orchestrator: OrchestratorPort,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        # ``absolute`` preserves the lexical final component so validation can
        # still detect a symlink or Windows reparse point at runtime_root.
        self.runtime_root = runtime_root.absolute()
        self.repository = repository
        self.orchestrator = orchestrator
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lock_path = self.runtime_root / "tick.lock"
        self.lock = FileLock(self.lock_path, timeout=0)

    def tick(self) -> SchedulerResult:
        try:
            self._validate_lock_boundary()
        except (OSError, ValueError):
            return SchedulerResult(status="unsafe_lock", processed=0)
        try:
            with self.lock.acquire(timeout=0):
                try:
                    self._validate_lock_boundary(require_lock=True)
                except (OSError, ValueError):
                    return SchedulerResult(status="unsafe_lock", processed=0)
                return self._tick_locked()
        except Timeout:
            return SchedulerResult(status="already_running", processed=0)

    def _tick_locked(self) -> SchedulerResult:
        runs = self.repository.list_active_runs()
        if not runs:
            return SchedulerResult(status="idle", processed=0)

        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("scheduler clock must return a timezone-aware value")
        now = now.astimezone(UTC)
        throttled = False
        waiting = False
        for run in runs:
            age = max(timedelta(0), now - run.updated_at.astimezone(UTC))
            if run.state not in _INTERVENTION_STATES and age > _STOP_AFTER:
                self.repository.block_for_scheduler(
                    run.run_id,
                    run.state,
                    evidence_kind="scheduler_timeout",
                    event_type="SCHEDULER_INACTIVITY_TIMEOUT",
                    reason_code="inactivity_limit_exceeded",
                    occurred_at=now,
                )
                return SchedulerResult(
                    status="blocked",
                    processed=1,
                    run_id=run.run_id,
                    outcome=StepOutcome(
                        state=RunState.BLOCKED,
                        action="inactivity_timeout",
                        blocked_reason=(
                            "automatic processing stopped after the inactivity limit"
                        ),
                    ),
                )
            if run.state in _INTERVENTION_STATES or run.state is (
                RunState.AWAITING_DESIGN_APPROVAL
            ):
                waiting = True
                continue

            interval = _FAST_WINDOW if age < _SLOW_AFTER else _SLOW_WINDOW
            last_poll = self.repository.get_scheduler_poll_at(run.run_id)
            if last_poll is not None and now - last_poll.astimezone(UTC) < interval:
                throttled = True
                continue

            # Persist before the delegated action so a scheduler crash cannot
            # immediately replay a side effect on the next invocation.
            self.repository.record_scheduler_poll(run.run_id, now)
            outcome = self.orchestrator.step(run.run_id)
            if outcome.action == "process_timeout":
                current = self.repository.get_run(run.run_id)
                if current.state is not RunState.BLOCKED:
                    self.repository.block_for_scheduler(
                        run.run_id,
                        current.state,
                        evidence_kind="process_timeout",
                        event_type="SCHEDULER_PROCESS_TIMEOUT",
                        reason_code="configured_process_timeout",
                        occurred_at=now,
                    )
                outcome = StepOutcome(
                    state=RunState.BLOCKED,
                    action="process_timeout",
                    blocked_reason=(
                        "the configured process timeout stopped automatic processing"
                    ),
                )
                return SchedulerResult(
                    status="blocked",
                    processed=1,
                    run_id=run.run_id,
                    outcome=outcome,
                )
            return SchedulerResult(
                status="processed",
                processed=1,
                run_id=run.run_id,
                outcome=outcome,
            )

        if throttled:
            return SchedulerResult(status="throttled", processed=0)
        if waiting:
            return SchedulerResult(
                status="waiting_for_intervention",
                processed=0,
            )
        return SchedulerResult(status="idle", processed=0)

    def _validate_lock_boundary(self, *, require_lock: bool = False) -> None:
        root = self.runtime_root
        metadata = root.lstat()
        if (
            root.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or self._is_reparse(metadata)
        ):
            raise ValueError("runtime root must be a regular non-reparse directory")
        if os.name != "nt" and metadata.st_mode & 0o022:
            raise ValueError("runtime root permissions are too broad")
        if not self.lock_path.exists():
            if require_lock:
                raise ValueError("tick lock disappeared while held")
            return
        lock_metadata = self.lock_path.lstat()
        if (
            self.lock_path.is_symlink()
            or not stat.S_ISREG(lock_metadata.st_mode)
            or self._is_reparse(lock_metadata)
            or self.lock_path.resolve(strict=True).parent != root.resolve(strict=True)
        ):
            raise ValueError("tick lock must be a regular non-reparse file")

    @staticmethod
    def _is_reparse(metadata: os.stat_result) -> bool:
        return bool(
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
