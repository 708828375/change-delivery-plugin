from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import stat
from typing import Literal, Protocol

from filelock import FileLock, Timeout

from change_delivery.domain import FrozenModel, RunState, StepOutcome
from change_delivery.notifications import NotificationService
from change_delivery.repository import RunRepository, TERMINAL_STATES
from change_delivery.retention import RetentionService


_FAST_WINDOW = timedelta(minutes=5)
_SLOW_WINDOW = timedelta(minutes=30)
_SLOW_AFTER = timedelta(hours=24)
_STOP_AFTER = timedelta(hours=72)
_INTERVENTION_STATES = {
    RunState.BLOCKED,
    RunState.REVIEW_DECISION_REQUIRED,
}
MANUAL_INTERVENTION_STATES = frozenset(
    {*_INTERVENTION_STATES, RunState.AWAITING_DESIGN_APPROVAL}
)


class OrchestratorPort(Protocol):
    def step(self, run_id: str) -> StepOutcome: ...

class RetentionPort(Protocol):
    def clean(self, *, now: datetime) -> object: ...

class NotificationPort(Protocol):
    def emit(self, run_id: str, state: RunState, message: str, *, action: str) -> object: ...




class SchedulerResult(FrozenModel):
    status: Literal[
        "processed",
        "blocked",
        "idle",
        "throttled",
        "already_running",
        "waiting_for_intervention",
        "unsafe_lock",
        "concurrent_state_change",
    ]
    processed: int
    run_id: str | None = None
    outcome: StepOutcome | None = None
    current_state: RunState | None = None


class Scheduler:
    """Single-instance scheduler that delegates one action to the orchestrator."""

    def __init__(
        self,
        *,
        retention_service: RetentionPort | None = None,
        notification_service: NotificationPort | None = None,
        runtime_root: Path,
        repository: RunRepository,
        orchestrator: OrchestratorPort,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        # ``absolute`` preserves the lexical final component so validation can
        # still detect a symlink or Windows reparse point at runtime_root.
        self.runtime_root = runtime_root.absolute()
        self.retention_service = retention_service or RetentionService(self.runtime_root, repository)
        self.notification_service = notification_service or NotificationService(repository.database)
        self.retention_lock_path = self.runtime_root / "retention.lock"
        self.retention_lock = FileLock(self.retention_lock_path, timeout=0)
        self.retention_day_path = self.runtime_root / "retention-day"
        self.repository = repository
        self.orchestrator = orchestrator
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lock_path = self.runtime_root / "tick.lock"
        self.lock = FileLock(self.lock_path, timeout=0)

    def tick(self) -> SchedulerResult:
        try:
            self._validate_lock_boundary()
            expected_identity = self._named_lock_identity()
        except (OSError, ValueError):
            return SchedulerResult(status="unsafe_lock", processed=0)
        try:
            with self.lock.acquire(timeout=0):
                try:
                    self._validate_lock_boundary(require_lock=True)
                    self._validate_held_lock_identity(expected_identity)
                except (OSError, ValueError):
                    return SchedulerResult(status="unsafe_lock", processed=0)
                result = self._tick_locked()
        except Timeout:
            return SchedulerResult(status="already_running", processed=0)
        self._notify_result(result)
        self._notify_waiting_runs()
        try:
            now = self.clock()
            if now.tzinfo is not None:
                self._run_daily_retention(now.astimezone(UTC))
        except Exception:
            pass
        return result
    def _tick_locked(self) -> SchedulerResult:
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("scheduler clock must return a timezone-aware value")
        now = now.astimezone(UTC)
        self._run_daily_retention(now)
        runs = self.repository.list_active_runs()
        if not runs:
            return SchedulerResult(status="idle", processed=0)
        throttled = False
        waiting = False
        for run in runs:
            age = max(timedelta(0), now - run.updated_at.astimezone(UTC))
            if run.state not in _INTERVENTION_STATES and age > _STOP_AFTER:
                event = self.repository.block_for_scheduler(
                    run.run_id,
                    run.state,
                    evidence_kind="scheduler_timeout",
                    event_type="SCHEDULER_INACTIVITY_TIMEOUT",
                    reason_code="inactivity_limit_exceeded",
                    occurred_at=now,
                    observed_updated_at=run.updated_at,
                    inactivity_threshold=_STOP_AFTER,
                )
                if event is None:
                    throttled = True
                    continue
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
            if not self.repository.claim_scheduler_run(
                run,
                now=now,
                window=interval,
            ):
                throttled = True
                continue

            outcome = self.orchestrator.step(run.run_id)
            if outcome.action == "process_timeout":
                observed = self.repository.get_run(run.run_id)
                if (
                    observed.state in TERMINAL_STATES
                    or observed.state in MANUAL_INTERVENTION_STATES
                ):
                    return SchedulerResult(
                        status="concurrent_state_change",
                        processed=1,
                        run_id=run.run_id,
                        current_state=observed.state,
                    )
                if observed.state is not outcome.state:
                    return SchedulerResult(
                        status="concurrent_state_change",
                        processed=1,
                        run_id=run.run_id,
                        current_state=observed.state,
                    )
                event = self.repository.block_for_scheduler(
                    run.run_id,
                    observed.state,
                    evidence_kind="process_timeout",
                    event_type="SCHEDULER_PROCESS_TIMEOUT",
                    reason_code="configured_process_timeout",
                    occurred_at=now,
                    observed_updated_at=observed.updated_at,
                    evidence_details={
                        "configured_timeout_seconds": (
                            outcome.configured_timeout_seconds
                        ),
                        "operation_hash": outcome.operation_hash,
                        "command_hash": outcome.command_hash,
                    },
                )
                if event is None:
                    authoritative = self.repository.get_run(run.run_id)
                    return SchedulerResult(
                        status="concurrent_state_change",
                        processed=1,
                        run_id=run.run_id,
                        current_state=authoritative.state,
                    )
                outcome = StepOutcome(
                    state=RunState.BLOCKED,
                    action="process_timeout",
                    blocked_reason=(
                        "the configured process timeout stopped automatic processing"
                    ),
                    configured_timeout_seconds=(
                        outcome.configured_timeout_seconds
                    ),
                    operation_hash=outcome.operation_hash,
                    command_hash=outcome.command_hash,
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

    def _notify_result(self, result: SchedulerResult) -> None:
        outcome = result.outcome
        if result.run_id is None or outcome is None:
            return
        if outcome.state not in {
            RunState.AWAITING_DESIGN_APPROVAL,
            RunState.BLOCKED,
            RunState.ENVIRONMENT_PREEMPTED,
            RunState.REVIEW_DECISION_REQUIRED,
            RunState.READY_TO_MERGE,
            RunState.FAILED_TERMINAL,
        }:
            return
        try:
            self.notification_service.emit(
                result.run_id,
                outcome.state,
                self._notification_reason(result.run_id, outcome.state, outcome.blocked_reason or outcome.action),
                action=outcome.action,
            )
        except Exception:
            pass

    def _notification_reason(
        self,
        run_id: str,
        state: RunState,
        fallback: str,
    ) -> str:
        events = self.repository.list_events(run_id)
        entering = next(
            (event for event in reversed(events) if event.to_state is state),
            None,
        )
        return entering.event_type if entering is not None else fallback
    def _notify_waiting_runs(self) -> None:
        try:
            runs = self.repository.list_active_runs()
        except Exception:
            return
        for run in runs:
            if run.state not in MANUAL_INTERVENTION_STATES:
                continue
            try:
                events = self.repository.list_events(run.run_id)
                reason = self._notification_reason(
                    run.run_id, run.state, run.state.value
                )
                self.notification_service.emit(
                    run.run_id,
                    run.state,
                    reason,
                    action="manual intervention required",
                )
            except Exception:
                continue
    def _run_daily_retention(self, now: datetime) -> None:
        service = self.retention_service
        if service is None:
            return
        temporary = self.retention_day_path.with_name(
            f".{self.retention_day_path.name}.{os.getpid()}.tmp"
        )
        try:
            with self.retention_lock.acquire(timeout=0):
                self._require_safe_auxiliary_file(self.retention_lock_path)
                day = now.date().isoformat()
                if self.retention_day_path.exists():
                    self._require_safe_auxiliary_file(self.retention_day_path)
                    if self.retention_day_path.read_text(encoding="ascii") == day:
                        return
                service.clean(now=now)
                descriptor = os.open(
                    temporary,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                try:
                    os.write(descriptor, day.encode("ascii"))
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self._require_safe_auxiliary_file(temporary)
                os.replace(temporary, self.retention_day_path)
                self._require_safe_auxiliary_file(self.retention_day_path)
        except Exception:
            return
        finally:
            try:
                if temporary.exists():
                    metadata = temporary.lstat()
                    if (
                        not temporary.is_symlink()
                        and not self._is_reparse(metadata)
                        and stat.S_ISREG(metadata.st_mode)
                        and metadata.st_nlink == 1
                    ):
                        temporary.unlink()
            except OSError:
                pass

    def _require_safe_auxiliary_file(self, path: Path) -> None:
        metadata = path.lstat()
        if (
            path.is_symlink()
            or self._is_reparse(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or path.resolve(strict=True).parent != self.runtime_root.resolve(strict=True)
        ):
            raise ValueError("scheduler auxiliary file is unsafe")

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

    def _named_lock_identity(self) -> tuple[int, int] | None:
        if not self.lock_path.exists():
            return None
        named = self.lock_path.lstat()
        return named.st_dev, named.st_ino

    def _validate_held_lock_identity(
        self,
        expected_identity: tuple[int, int] | None = None,
    ) -> None:
        context = getattr(self.lock, "_context", None)
        descriptor = getattr(context, "lock_file_fd", None)
        if not isinstance(descriptor, int) or descriptor < 0:
            raise ValueError("tick lock has no stable held descriptor")
        opened = os.fstat(descriptor)
        named = self.lock_path.lstat()
        if self._is_reparse(opened) or self._is_reparse(named):
            raise ValueError("tick lock became a reparse point")
        if (
            opened.st_dev,
            opened.st_ino,
        ) != (
            named.st_dev,
            named.st_ino,
        ):
            raise ValueError("tick lock identity changed")
        if expected_identity is not None and (
            opened.st_dev,
            opened.st_ino,
        ) != expected_identity:
            raise ValueError("tick lock was replaced before acquisition")

    @staticmethod
    def _is_reparse(metadata: os.stat_result) -> bool:
        return bool(
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
