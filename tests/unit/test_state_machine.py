from __future__ import annotations

from itertools import pairwise
from types import MappingProxyType

import pytest

from change_delivery.domain import RunState
from change_delivery.domain import EvidenceRef
from change_delivery.state_machine import (
    ALLOWED_TRANSITIONS,
    READY_TO_MERGE_EVIDENCE,
    assert_transition,
)


HAPPY_PATH = (
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
APPROVAL_SHA = "a" * 64
APPROVAL_EVIDENCE = (
    EvidenceRef(
        evidence_id="approval-1",
        kind="explicit_approval",
        sha256=APPROVAL_SHA,
    ),
)


def test_transition_table_is_deeply_immutable_and_covers_every_state() -> None:
    assert isinstance(ALLOWED_TRANSITIONS, MappingProxyType)
    assert set(ALLOWED_TRANSITIONS) == set(RunState)
    assert all(isinstance(targets, frozenset) for targets in ALLOWED_TRANSITIONS.values())

    with pytest.raises(TypeError):
        ALLOWED_TRANSITIONS[RunState.DRAFT] = frozenset()  # type: ignore[index]


@pytest.mark.parametrize(
    "state",
    [RunState.COMPLETED, RunState.CANCELLED, RunState.FAILED_TERMINAL],
)
def test_terminal_states_have_no_outgoing_transitions(state: RunState) -> None:
    assert ALLOWED_TRANSITIONS[state] == frozenset()


def test_complete_happy_path_is_allowed_without_stage_skips() -> None:
    for current, target in pairwise(HAPPY_PATH):
        evidence = READY_TO_MERGE_EVIDENCE if target is RunState.READY_TO_MERGE else ()
        kwargs = (
            {
                "evidence_refs": APPROVAL_EVIDENCE,
                "approval_snapshot_sha256": APPROVAL_SHA,
            }
            if target is RunState.APPROVED
            else {}
        )
        assert_transition(current, target, evidence, **kwargs)

    with pytest.raises(ValueError, match="非法状态迁移"):
        assert_transition(RunState.DRAFT, RunState.IMPLEMENTING, ())
    with pytest.raises(ValueError, match="非法状态迁移"):
        assert_transition(RunState.PR_DRAFT, RunState.READY_TO_MERGE, ())


def test_internal_review_fix_uses_dedicated_bounded_loop() -> None:
    assert RunState.FIXING_INTERNAL_REVIEW in ALLOWED_TRANSITIONS[
        RunState.INTERNAL_REVIEW
    ]
    assert RunState.LOCAL_VALIDATION in ALLOWED_TRANSITIONS[
        RunState.FIXING_INTERNAL_REVIEW
    ]
    assert RunState.IMPLEMENTING not in ALLOWED_TRANSITIONS[
        RunState.INTERNAL_REVIEW
    ]
    assert RunState.COMMITTING not in ALLOWED_TRANSITIONS[
        RunState.FIXING_INTERNAL_REVIEW
    ]


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunState.LOCAL_VALIDATION, RunState.IMPLEMENTING),
        (RunState.INTERNAL_REVIEW, RunState.FIXING_INTERNAL_REVIEW),
        (RunState.FIXING_INTERNAL_REVIEW, RunState.LOCAL_VALIDATION),
        (RunState.MONITORING_CI_REVIEW, RunState.FIXING_CI_REVIEW),
        (RunState.FIXING_CI_REVIEW, RunState.LOCAL_VALIDATION),
        (RunState.DEPLOYING_TEST, RunState.ENVIRONMENT_PREEMPTED),
        (RunState.ACCEPTANCE_TESTING, RunState.ENVIRONMENT_PREEMPTED),
        (RunState.ENVIRONMENT_PREEMPTED, RunState.DEPLOYING_TEST),
        (RunState.INTERNAL_REVIEW, RunState.REVIEW_DECISION_REQUIRED),
    ],
)
def test_repair_and_interruption_recovery_transitions(
    current: RunState,
    target: RunState,
) -> None:
    assert_transition(current, target, ())


def test_every_non_terminal_state_can_fail_block_or_cancel() -> None:
    terminal = {
        RunState.COMPLETED,
        RunState.CANCELLED,
        RunState.FAILED_TERMINAL,
    }
    for state in RunState:
        if state in terminal:
            continue
        expected = {RunState.CANCELLED, RunState.FAILED_TERMINAL}
        if state is not RunState.BLOCKED:
            expected.add(RunState.BLOCKED)
        assert expected <= ALLOWED_TRANSITIONS[state]


def test_ready_to_merge_requires_all_gates_and_lists_missing_kinds() -> None:
    missing = "head_sha_verified"
    evidence = READY_TO_MERGE_EVIDENCE - {missing}

    with pytest.raises(ValueError, match="缺少门禁") as raised:
        assert_transition(
            RunState.MONITORING_CI_REVIEW,
            RunState.READY_TO_MERGE,
            evidence,
        )

    assert missing in str(raised.value)


def test_ready_to_merge_accepts_required_and_extra_evidence() -> None:
    assert_transition(
        RunState.MONITORING_CI_REVIEW,
        RunState.READY_TO_MERGE,
        {*READY_TO_MERGE_EVIDENCE, "security_scan_passed"},
    )


def test_assert_transition_does_not_modify_evidence_input() -> None:
    evidence = list(READY_TO_MERGE_EVIDENCE) + ["extra"]
    before = evidence.copy()

    assert_transition(
        RunState.MONITORING_CI_REVIEW,
        RunState.READY_TO_MERGE,
        evidence,
    )

    assert evidence == before


@pytest.mark.parametrize("kind", ["", " ", "bad\nkind", "bad\x00kind", "UPPER"])
def test_assert_transition_rejects_invalid_evidence_kind(kind: str) -> None:
    with pytest.raises(ValueError, match="证据 kind"):
        assert_transition(RunState.DRAFT, RunState.AWAITING_DESIGN_APPROVAL, [kind])


def test_assert_transition_rejects_bare_string_instead_of_kind_collection() -> None:
    with pytest.raises(ValueError, match="证据 kind"):
        assert_transition(
            RunState.DRAFT,
            RunState.AWAITING_DESIGN_APPROVAL,
            "ci_passed",
        )


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunState.BLOCKED, RunState.IMPLEMENTING),
        (RunState.REVIEW_DECISION_REQUIRED, RunState.INTERNAL_REVIEW),
    ],
)
def test_interruption_recovery_requires_source_provenance(
    current: RunState,
    target: RunState,
) -> None:
    with pytest.raises(ValueError, match="缺少中断来源"):
        assert_transition(current, target, ())


@pytest.mark.parametrize(
    ("current", "target", "source"),
    [
        (RunState.BLOCKED, RunState.LOCAL_VALIDATION, RunState.IMPLEMENTING),
        (
            RunState.REVIEW_DECISION_REQUIRED,
            RunState.INTERNAL_REVIEW,
            RunState.LOCAL_VALIDATION,
        ),
    ],
)
def test_interruption_recovery_rejects_target_other_than_source(
    current: RunState,
    target: RunState,
    source: RunState,
) -> None:
    with pytest.raises(ValueError, match="恢复目标不匹配"):
        assert_transition(current, target, (), interrupted_from=source)


@pytest.mark.parametrize(
    ("current", "source"),
    [
        (RunState.BLOCKED, RunState.IMPLEMENTING),
        (RunState.REVIEW_DECISION_REQUIRED, RunState.INTERNAL_REVIEW),
    ],
)
def test_interruption_recovery_accepts_matching_active_source(
    current: RunState,
    source: RunState,
) -> None:
    assert_transition(current, source, (), interrupted_from=source)


@pytest.mark.parametrize(
    "invalid_source",
    [
        RunState.BLOCKED,
        RunState.REVIEW_DECISION_REQUIRED,
        RunState.ENVIRONMENT_PREEMPTED,
        RunState.COMPLETED,
        RunState.CANCELLED,
        RunState.FAILED_TERMINAL,
    ],
)
def test_interrupted_from_must_be_normal_non_terminal_active_state(
    invalid_source: RunState,
) -> None:
    with pytest.raises(ValueError, match="中断来源"):
        assert_transition(
            RunState.BLOCKED,
            RunState.IMPLEMENTING,
            (),
            interrupted_from=invalid_source,
        )


def test_early_interruption_source_cannot_jump_to_ready_to_merge() -> None:
    with pytest.raises(ValueError, match="恢复目标不匹配"):
        assert_transition(
            RunState.BLOCKED,
            RunState.READY_TO_MERGE,
            READY_TO_MERGE_EVIDENCE,
            interrupted_from=RunState.IMPLEMENTING,
        )


def test_blocked_self_transition_is_idempotently_allowed() -> None:
    assert_transition(RunState.BLOCKED, RunState.BLOCKED, ())


def test_review_decision_may_become_blocked_without_recovery_provenance() -> None:
    assert_transition(
        RunState.REVIEW_DECISION_REQUIRED,
        RunState.BLOCKED,
        (),
    )


def test_recovery_does_not_modify_evidence_input() -> None:
    evidence = ["review_context"]
    before = evidence.copy()

    assert_transition(
        RunState.BLOCKED,
        RunState.IMPLEMENTING,
        evidence,
        interrupted_from=RunState.IMPLEMENTING,
    )

    assert evidence == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("current", "DRAFT"),
        ("target", "READY_TO_MERGE"),
        ("interrupted_from", "IMPLEMENTING"),
    ],
)
def test_assert_transition_rejects_string_state_values(
    field: str,
    value: str,
) -> None:
    arguments: dict[str, object] = {
        "current": RunState.DRAFT,
        "target": RunState.AWAITING_DESIGN_APPROVAL,
        "evidence_kinds": (),
    }
    arguments[field] = value

    with pytest.raises(TypeError, match="RunState"):
        assert_transition(**arguments)  # type: ignore[arg-type]


def test_string_ready_to_merge_cannot_bypass_evidence_gate() -> None:
    with pytest.raises(TypeError, match="RunState"):
        assert_transition(
            RunState.MONITORING_CI_REVIEW,
            "READY_TO_MERGE",  # type: ignore[arg-type]
            (),
        )


def test_approved_requires_explicit_approval_reference_not_only_kind() -> None:
    with pytest.raises(ValueError, match="缺少批准门禁"):
        assert_transition(
            RunState.AWAITING_DESIGN_APPROVAL,
            RunState.APPROVED,
            ("explicit_approval",),
            approval_snapshot_sha256=APPROVAL_SHA,
        )


@pytest.mark.parametrize("snapshot_sha", [None, "", "A" * 64, "a" * 63])
def test_approved_requires_strict_expected_snapshot_sha(
    snapshot_sha: str | None,
) -> None:
    with pytest.raises(ValueError, match="缺少批准门禁"):
        assert_transition(
            RunState.AWAITING_DESIGN_APPROVAL,
            RunState.APPROVED,
            (),
            evidence_refs=APPROVAL_EVIDENCE,
            approval_snapshot_sha256=snapshot_sha,
        )


def test_approved_rejects_approval_reference_for_other_snapshot() -> None:
    with pytest.raises(ValueError, match="批准快照不匹配"):
        assert_transition(
            RunState.AWAITING_DESIGN_APPROVAL,
            RunState.APPROVED,
            (),
            evidence_refs=APPROVAL_EVIDENCE,
            approval_snapshot_sha256="b" * 64,
        )


def test_approved_accepts_matching_explicit_approval_snapshot() -> None:
    assert_transition(
        RunState.AWAITING_DESIGN_APPROVAL,
        RunState.APPROVED,
        (),
        evidence_refs=APPROVAL_EVIDENCE,
        approval_snapshot_sha256=APPROVAL_SHA,
    )


def test_approved_does_not_modify_evidence_reference_input() -> None:
    references = list(APPROVAL_EVIDENCE)
    before = references.copy()

    assert_transition(
        RunState.AWAITING_DESIGN_APPROVAL,
        RunState.APPROVED,
        (),
        evidence_refs=references,
        approval_snapshot_sha256=APPROVAL_SHA,
    )

    assert references == before


def test_approved_rejects_constructed_reference_with_empty_evidence_id() -> None:
    invalid_reference = EvidenceRef.model_construct(
        evidence_id="",
        kind="explicit_approval",
        sha256=APPROVAL_SHA,
    )

    with pytest.raises(ValueError, match="证据引用"):
        assert_transition(
            RunState.AWAITING_DESIGN_APPROVAL,
            RunState.APPROVED,
            (),
            evidence_refs=(invalid_reference,),
            approval_snapshot_sha256=APPROVAL_SHA,
        )
