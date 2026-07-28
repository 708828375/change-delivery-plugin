from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from change_delivery.domain import EvidenceRef


_MAX_CANONICAL_DEPTH = 64


def _validate_canonical_json(
    value: Any,
    ancestors: set[int],
    depth: int,
) -> None:
    if depth > _MAX_CANONICAL_DEPTH:
        raise ValueError("canonical JSON 嵌套过深")
    if value is None or type(value) in {bool, int, str}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("canonical JSON 浮点数必须有限")
        return
    if type(value) in {list, tuple}:
        identity = id(value)
        if identity in ancestors:
            raise ValueError("canonical JSON 禁止循环引用")
        ancestors.add(identity)
        try:
            for item in value:
                _validate_canonical_json(item, ancestors, depth + 1)
        finally:
            ancestors.remove(identity)
        return
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise TypeError("canonical JSON 对象键必须是字符串")
        identity = id(value)
        if identity in ancestors:
            raise ValueError("canonical JSON 禁止循环引用")
        ancestors.add(identity)
        try:
            for item in value.values():
                _validate_canonical_json(item, ancestors, depth + 1)
        finally:
            ancestors.remove(identity)
        return
    raise TypeError(f"不支持的 canonical JSON 类型：{type(value).__name__}")


def _payload_bytes(payload: Any) -> bytes:
    _validate_canonical_json(payload, set(), 0)
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
