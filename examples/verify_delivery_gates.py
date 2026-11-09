"""Exercise delivery gates with synthetic data and no external actions."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sys


# Run directly from a source checkout without installing the Windows host.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from change_delivery.domain import RunState
from change_delivery.evidence import build_evidence_ref, sha256_payload
from change_delivery.state_machine import (
    READY_TO_MERGE_EVIDENCE,
    assert_transition,
)


def expect_rejection(check: Callable[[], None], expected_message: str) -> None:
    try:
        check()
    except ValueError as error:
        if expected_message not in str(error):
            raise AssertionError(f"Unexpected rejection: {error}") from error
    else:
        raise AssertionError("Expected the gate to reject this transition")


def main() -> None:
    # This synthetic payload is not a real user's approval or a stored run.
    approved_payload = {"run_id": "demo-run", "plan": "add a health check"}
    approval_ref = build_evidence_ref(
        "demo-approval", "explicit_approval", approved_payload
    )

    assert_transition(
        RunState.AWAITING_DESIGN_APPROVAL,
        RunState.APPROVED,
        (),
        evidence_refs=(approval_ref,),
        approval_snapshot_sha256=sha256_payload(approved_payload),
    )
    print("[PASS] Matching approval snapshot accepted")

    changed_payload = {**approved_payload, "plan": "change the billing flow"}
    expect_rejection(
        lambda: assert_transition(
            RunState.AWAITING_DESIGN_APPROVAL,
            RunState.APPROVED,
            (),
            evidence_refs=(approval_ref,),
            approval_snapshot_sha256=sha256_payload(changed_payload),
        ),
        "批准快照不匹配",
    )
    print("[PASS] Changed approval snapshot rejected")

    expect_rejection(
        lambda: assert_transition(
            RunState.MONITORING_CI_REVIEW,
            RunState.READY_TO_MERGE,
            READY_TO_MERGE_EVIDENCE - {"acceptance_passed"},
        ),
        "缺少门禁：acceptance_passed",
    )
    print("[PASS] READY_TO_MERGE rejected when acceptance_passed is missing")

    assert_transition(
        RunState.MONITORING_CI_REVIEW,
        RunState.READY_TO_MERGE,
        READY_TO_MERGE_EVIDENCE,
    )
    print(
        "[PASS] READY_TO_MERGE accepted with all "
        f"{len(READY_TO_MERGE_EVIDENCE)} required evidence kinds"
    )
    print("Offline checks passed (4/4). No delivery actions were invoked.")


if __name__ == "__main__":
    main()
