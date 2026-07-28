from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from itertools import pairwise
from types import MappingProxyType

from change_delivery.domain import EVIDENCE_IDENTIFIER_PATTERN, RunState


READY_TO_MERGE_EVIDENCE = frozenset(
    {
        "ci_passed",
        "acceptance_passed",
        "internal_review_passed",
        "threads_resolved",
        "mergeable",
        "head_sha_verified",
    }
)

_HAPPY_PATH = (
    RunState.DRAFT,
    RunState.AWAITING_DESIGN_APPROVAL,
    RunState.APPROVED,
    RunState.PREPARING_WORKTREE,
    RunState.IMPLEMENTING,
    RunState.LOCAL_VALIDATION,
    RunState.INTERNAL_REVIEW,
    RunState.COMMITTING,
    RunState.PUSHING_FEATURE,
    RunState.PR_DRAFT,
    RunState.DEPLOYING_TEST,
    RunState.ACCEPTANCE_TESTING,
    RunState.PR_READY_FOR_REVIEW,
    RunState.MONITORING_CI_REVIEW,
    RunState.READY_TO_MERGE,
    RunState.MERGED,
    RunState.CLEANING_LOCAL,
    RunState.COMPLETED,
)
_TERMINAL_STATES = frozenset(
    {
        RunState.COMPLETED,
        RunState.CANCELLED,
        RunState.FAILED_TERMINAL,
    }
)
_ACTIVE_STATES = frozenset(_HAPPY_PATH[1:-1])
_DECISION_STATES = frozenset(
    {
        RunState.AWAITING_DESIGN_APPROVAL,
        RunState.IMPLEMENTING,
        RunState.LOCAL_VALIDATION,
        RunState.INTERNAL_REVIEW,
        RunState.COMMITTING,
        RunState.PR_DRAFT,
        RunState.ACCEPTANCE_TESTING,
        RunState.MONITORING_CI_REVIEW,
        RunState.FIXING_CI_REVIEW,
        RunState.READY_TO_MERGE,
    }
)


def _build_transitions() -> Mapping[RunState, frozenset[RunState]]:
    transitions: dict[RunState, set[RunState]] = {
        state: set() for state in RunState
    }
    for current, target in pairwise(_HAPPY_PATH):
        transitions[current].add(target)

    transitions[RunState.LOCAL_VALIDATION].add(RunState.IMPLEMENTING)
    transitions[RunState.INTERNAL_REVIEW].add(RunState.IMPLEMENTING)
    transitions[RunState.MONITORING_CI_REVIEW].add(
        RunState.FIXING_CI_REVIEW
    )
    transitions[RunState.FIXING_CI_REVIEW].add(RunState.LOCAL_VALIDATION)

    transitions[RunState.DEPLOYING_TEST].add(
        RunState.ENVIRONMENT_PREEMPTED
    )
    transitions[RunState.ACCEPTANCE_TESTING].add(
        RunState.ENVIRONMENT_PREEMPTED
    )
    transitions[RunState.ENVIRONMENT_PREEMPTED].add(
        RunState.DEPLOYING_TEST
    )

    for state in _DECISION_STATES:
        transitions[state].add(RunState.REVIEW_DECISION_REQUIRED)
    transitions[RunState.REVIEW_DECISION_REQUIRED].update(_DECISION_STATES)
    transitions[RunState.BLOCKED].update(_ACTIVE_STATES)

    for state in RunState:
        if state in _TERMINAL_STATES:
            continue
        if state is not RunState.BLOCKED:
            transitions[state].add(RunState.BLOCKED)
        transitions[state].update(
            {RunState.CANCELLED, RunState.FAILED_TERMINAL}
        )

    return MappingProxyType(
        {state: frozenset(targets) for state, targets in transitions.items()}
    )


ALLOWED_TRANSITIONS = _build_transitions()
_EVIDENCE_KIND = re.compile(EVIDENCE_IDENTIFIER_PATTERN)


def _normalize_evidence_kinds(
    evidence_kinds: Iterable[str],
) -> frozenset[str]:
    normalized = frozenset(evidence_kinds)
    if any(
        not isinstance(kind, str) or _EVIDENCE_KIND.fullmatch(kind) is None
        for kind in normalized
    ):
        raise ValueError("证据 kind 必须是非空小写规范标识")
    return normalized


def assert_transition(
    current: RunState,
    target: RunState,
    evidence_kinds: Iterable[str],
) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"非法状态迁移：{current.value} -> {target.value}")

    evidence = _normalize_evidence_kinds(evidence_kinds)
    if target is RunState.READY_TO_MERGE:
        missing = READY_TO_MERGE_EVIDENCE - evidence
        if missing:
            raise ValueError(f"缺少门禁：{', '.join(sorted(missing))}")
