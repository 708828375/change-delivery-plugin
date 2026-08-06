from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from hmac import compare_digest
from itertools import pairwise
from types import MappingProxyType

from change_delivery.domain import (
    EVIDENCE_IDENTIFIER_PATTERN,
    SHA256_PATTERN,
    EvidenceRef,
    RunState,
)


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
_NORMAL_ACTIVE_STATES = frozenset(
    (
        *_HAPPY_PATH[:-1],
        RunState.FIXING_INTERNAL_REVIEW,
        RunState.FIXING_CI_REVIEW,
    )
)
_DECISION_STATES = frozenset(
    {
        RunState.AWAITING_DESIGN_APPROVAL,
        RunState.IMPLEMENTING,
        RunState.LOCAL_VALIDATION,
        RunState.INTERNAL_REVIEW,
        RunState.FIXING_INTERNAL_REVIEW,
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
    transitions[RunState.INTERNAL_REVIEW].add(
        RunState.FIXING_INTERNAL_REVIEW
    )
    transitions[RunState.FIXING_INTERNAL_REVIEW].add(
        RunState.LOCAL_VALIDATION
    )
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
    transitions[RunState.BLOCKED].update(_NORMAL_ACTIVE_STATES)
    transitions[RunState.BLOCKED].add(RunState.BLOCKED)

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
_SHA256 = re.compile(SHA256_PATTERN)


def _normalize_evidence_kinds(
    evidence_kinds: Iterable[str],
) -> frozenset[str]:
    if isinstance(evidence_kinds, str):
        raise ValueError("证据 kind 必须以集合形式提供")
    normalized = frozenset(evidence_kinds)
    if any(
        not isinstance(kind, str) or _EVIDENCE_KIND.fullmatch(kind) is None
        for kind in normalized
    ):
        raise ValueError("证据 kind 必须是非空小写规范标识")
    return normalized


def _normalize_evidence_refs(
    evidence_refs: Iterable[EvidenceRef],
) -> tuple[EvidenceRef, ...]:
    if isinstance(evidence_refs, (str, bytes)):
        raise TypeError("evidence_refs 必须包含 EvidenceRef")
    normalized = tuple(evidence_refs)
    if any(not isinstance(reference, EvidenceRef) for reference in normalized):
        raise TypeError("evidence_refs 必须包含 EvidenceRef")
    if any(
        type(reference.evidence_id) is not str
        or type(reference.kind) is not str
        or type(reference.sha256) is not str
        or _EVIDENCE_KIND.fullmatch(reference.evidence_id) is None
        or _EVIDENCE_KIND.fullmatch(reference.kind) is None
        or _SHA256.fullmatch(reference.sha256) is None
        for reference in normalized
    ):
        raise ValueError("证据引用字段无效")
    return normalized


def assert_transition(
    current: RunState,
    target: RunState,
    evidence_kinds: Iterable[str],
    *,
    interrupted_from: RunState | None = None,
    evidence_refs: Iterable[EvidenceRef] = (),
    approval_snapshot_sha256: str | None = None,
) -> None:
    if not isinstance(current, RunState):
        raise TypeError("current 必须是 RunState")
    if not isinstance(target, RunState):
        raise TypeError("target 必须是 RunState")
    if interrupted_from is not None and not isinstance(
        interrupted_from,
        RunState,
    ):
        raise TypeError("interrupted_from 必须是 RunState")

    if (
        interrupted_from is not None
        and interrupted_from not in _NORMAL_ACTIVE_STATES
    ):
        raise ValueError("中断来源必须是非终态正常活跃状态")

    if target not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"非法状态迁移：{current.value} -> {target.value}")

    if (
        current
        in {RunState.BLOCKED, RunState.REVIEW_DECISION_REQUIRED}
        and target in _NORMAL_ACTIVE_STATES
    ):
        if interrupted_from is None:
            raise ValueError("缺少中断来源")
        if target is not interrupted_from:
            raise ValueError(
                "恢复目标不匹配："
                f"期望 {interrupted_from.value}，实际 {target.value}"
            )

    evidence = _normalize_evidence_kinds(evidence_kinds)
    references = _normalize_evidence_refs(evidence_refs)
    if target is RunState.APPROVED:
        approval_references = tuple(
            reference
            for reference in references
            if reference.kind == "explicit_approval"
        )
        if (
            not isinstance(approval_snapshot_sha256, str)
            or _SHA256.fullmatch(approval_snapshot_sha256) is None
            or not approval_references
        ):
            raise ValueError("缺少批准门禁")
        if not any(
            compare_digest(
                reference.sha256,
                approval_snapshot_sha256,
            )
            for reference in approval_references
        ):
            raise ValueError("批准快照不匹配")

    if target is RunState.READY_TO_MERGE:
        missing = READY_TO_MERGE_EVIDENCE - evidence
        if missing:
            raise ValueError(f"缺少门禁：{', '.join(sorted(missing))}")
