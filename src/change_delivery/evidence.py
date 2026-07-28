from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from change_delivery.domain import EvidenceRef


def _payload_bytes(payload: Any) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, (bytearray, memoryview)):
        return bytes(payload)
    if isinstance(payload, Path):
        return payload.read_bytes()
    canonical = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return canonical.encode("utf-8")


def sha256_payload(payload: Any) -> str:
    return hashlib.sha256(_payload_bytes(payload)).hexdigest()


def build_evidence_ref(
    evidence_id: str,
    kind: str,
    payload: Any,
) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=evidence_id,
        kind=kind,
        sha256=sha256_payload(payload),
    )
