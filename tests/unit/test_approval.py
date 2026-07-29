from datetime import datetime, timezone
import hashlib
import json
from collections.abc import Mapping
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

import change_delivery.approval as approval_module
from change_delivery.approval import (
    ApprovalSnapshot,
    build_approval,
    load_approval,
    save_approval,
    verify_approval,
)
from change_delivery.domain import RunState


BASE_SHA = "a" * 40
CONFIG_HASH = "b" * 64
NOW = datetime(2026, 7, 28, 8, 30, tzinfo=timezone.utc)
DEFAULT_SIDE_EFFECTS = (
    "git_commit",
    "git_push_feature",
    "create_or_update_pr",
    "force_with_lease_test",
    "tagged_test_api_data",
    "review_fix_loop",
)


def write_artifacts(root: Path) -> tuple[Path, ...]:
    requirement = root / "requirement.md"
    design = root / "design.md"
    plan = root / "implementation-plan.md"
    requirement.write_text("需求 v1", encoding="utf-8")
    design.write_text("设计 v1", encoding="utf-8")
    plan.write_bytes(b"\x00\xffbinary")
    return requirement, design, plan


def build_snapshot(tmp_path: Path) -> tuple[ApprovalSnapshot, tuple[Path, ...]]:
    files = write_artifacts(tmp_path)
    snapshot = build_approval(
        run_id="run-001",
        project_id="example-backend",
        config_hash=CONFIG_HASH,
        base_branch="develop",
        base_sha=BASE_SHA,
        test_branch="test",
        files=files,
        approved_user="JoySun",
        user_provider=lambda: "JoySun",
        clock=lambda: NOW,
    )
    return snapshot, files


def test_build_approval_hashes_binary_bytes_and_uses_stable_names(
    tmp_path: Path,
) -> None:
    snapshot, files = build_snapshot(tmp_path)

    assert snapshot.artifact_hashes == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files
    }
    assert all(str(tmp_path) not in key for key in snapshot.artifact_hashes)
    assert snapshot.approved_at == NOW
    assert snapshot.approved_at.tzinfo == timezone.utc
    assert snapshot.automation_limit is RunState.READY_TO_MERGE
    assert snapshot.allowed_side_effects == DEFAULT_SIDE_EFFECTS


@pytest.mark.parametrize("field", ["run_id", "project_id", "config_hash"])
def test_build_approval_rejects_empty_identifiers(
    tmp_path: Path,
    field: str,
) -> None:
    files = write_artifacts(tmp_path)
    kwargs = {
        "run_id": "run-001",
        "project_id": "project-001",
        "config_hash": CONFIG_HASH,
    }
    kwargs[field] = ""

    with pytest.raises((ValueError, ValidationError)):
        build_approval(
            **kwargs,
            base_branch="develop",
            base_sha=BASE_SHA,
            test_branch="test",
            files=files,
            approved_user="JoySun",
            user_provider=lambda: "JoySun",
            clock=lambda: NOW,
        )


@pytest.mark.parametrize("base_sha", ["", "a" * 39, "g" * 40])
def test_base_sha_must_be_40_character_hex(
    tmp_path: Path,
    base_sha: str,
) -> None:
    files = write_artifacts(tmp_path)

    with pytest.raises((ValueError, ValidationError)):
        build_approval(
            run_id="run-001",
            project_id="project-001",
            config_hash=CONFIG_HASH,
            base_branch="develop",
            base_sha=base_sha,
            test_branch="test",
            files=files,
            approved_user="JoySun",
            user_provider=lambda: "JoySun",
            clock=lambda: NOW,
        )


def test_approval_snapshot_is_deeply_immutable(tmp_path: Path) -> None:
    snapshot, _ = build_snapshot(tmp_path)

    assert isinstance(snapshot.artifact_hashes, Mapping)
    assert not isinstance(snapshot.artifact_hashes, dict)
    with pytest.raises(ValidationError):
        snapshot.base_sha = "c" * 40
    with pytest.raises(TypeError):
        snapshot.artifact_hashes["design.md"] = "c" * 64
    with pytest.raises(TypeError):
        dict.__setitem__(snapshot.artifact_hashes, "design.md", "c" * 64)


def test_model_forbids_extra_fields_and_side_effect_default_is_fixed(
    tmp_path: Path,
) -> None:
    snapshot, _ = build_snapshot(tmp_path)
    payload = snapshot.model_dump(mode="json")
    payload["unexpected"] = True

    with pytest.raises(ValidationError):
        ApprovalSnapshot.model_validate(payload)
    assert tuple(snapshot.allowed_side_effects) == DEFAULT_SIDE_EFFECTS


def test_model_rejects_noncanonical_artifact_hash_set(tmp_path: Path) -> None:
    snapshot, _ = build_snapshot(tmp_path)
    payload = snapshot.model_dump(mode="json")
    payload["artifact_hashes"]["other.md"] = "d" * 64

    with pytest.raises(ValidationError, match="artifact"):
        ApprovalSnapshot.model_validate(payload)


def test_verify_detects_design_change_and_config_change(tmp_path: Path) -> None:
    snapshot, files = build_snapshot(tmp_path)
    assert verify_approval(snapshot, files, CONFIG_HASH)

    files[0].write_text("设计 v2", encoding="utf-8")
    assert not verify_approval(snapshot, files, CONFIG_HASH)
    assert not verify_approval(snapshot, files, "c" * 64)


def test_verify_rejects_added_missing_and_duplicate_names(tmp_path: Path) -> None:
    snapshot, files = build_snapshot(tmp_path)
    extra = tmp_path / "extra.md"
    extra.write_text("extra", encoding="utf-8")
    assert not verify_approval(snapshot, (*files, extra), CONFIG_HASH)
    assert not verify_approval(snapshot, files[:-1], CONFIG_HASH)

    duplicate_root = tmp_path / "nested"
    duplicate_root.mkdir()
    duplicate = duplicate_root / files[0].name
    duplicate.write_text("another", encoding="utf-8")
    assert not verify_approval(snapshot, (*files, duplicate), CONFIG_HASH)


@pytest.mark.parametrize(
    "replacement",
    ["other.md", "Requirement.md", "requirement.md."],
)
def test_build_requires_exact_three_canonical_artifact_names(
    tmp_path: Path,
    replacement: str,
) -> None:
    files = list(write_artifacts(tmp_path))
    renamed = tmp_path / replacement
    files[0].replace(renamed)
    files[0] = renamed

    with pytest.raises(ValueError, match="artifact|规范"):
        build_approval(
            run_id="run-001",
            project_id="project-001",
            config_hash=CONFIG_HASH,
            base_branch="develop",
            base_sha=BASE_SHA,
            test_branch="test",
            files=files,
            approved_user="JoySun",
            user_provider=lambda: "JoySun",
            clock=lambda: NOW,
        )


def test_build_rejects_artifacts_from_different_parents(tmp_path: Path) -> None:
    files = list(write_artifacts(tmp_path))
    other = tmp_path / "other"
    other.mkdir()
    moved = other / files[-1].name
    files[-1].replace(moved)
    files[-1] = moved

    with pytest.raises(ValueError, match="same directory|同一"):
        build_approval(
            run_id="run-001",
            project_id="project-001",
            config_hash=CONFIG_HASH,
            base_branch="develop",
            base_sha=BASE_SHA,
            test_branch="test",
            files=files,
            user_provider=lambda: "JoySun",
            clock=lambda: NOW,
        )


def test_build_rejects_symlink_or_reparse_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = list(write_artifacts(tmp_path))
    real_check = approval_module._is_reparse_point
    monkeypatch.setattr(
        approval_module,
        "_is_reparse_point",
        lambda path: path.name == "requirement.md" or real_check(path),
    )

    with pytest.raises(ValueError, match="link|reparse"):
        build_approval(
            run_id="run-001",
            project_id="project-001",
            config_hash=CONFIG_HASH,
            base_branch="develop",
            base_sha=BASE_SHA,
            test_branch="test",
            files=files,
            user_provider=lambda: "JoySun",
            clock=lambda: NOW,
        )


@pytest.mark.parametrize("bad_kind", ["missing", "directory"])
def test_build_fails_cleanly_for_unreadable_artifact(
    tmp_path: Path,
    bad_kind: str,
) -> None:
    files = list(write_artifacts(tmp_path))
    bad = tmp_path / "bad"
    if bad_kind == "directory":
        bad.mkdir()
    files.append(bad)

    with pytest.raises((OSError, ValueError), match="artifact|文件"):
        build_approval(
            run_id="run-001",
            project_id="project-001",
            config_hash=CONFIG_HASH,
            base_branch="develop",
            base_sha=BASE_SHA,
            test_branch="test",
            files=files,
            approved_user="JoySun",
            user_provider=lambda: "JoySun",
            clock=lambda: NOW,
        )


def test_verify_is_conservative_when_artifact_cannot_be_read(
    tmp_path: Path,
) -> None:
    snapshot, files = build_snapshot(tmp_path)
    files[0].unlink()

    assert not verify_approval(snapshot, files, CONFIG_HASH)


def test_verify_returns_false_for_invalid_config_hash_types_and_unicode(
    tmp_path: Path,
) -> None:
    snapshot, files = build_snapshot(tmp_path)

    assert not verify_approval(snapshot, files, None)  # type: ignore[arg-type]
    assert not verify_approval(snapshot, files, "配置-不同")


def test_stable_read_rejects_file_change_during_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = write_artifacts(tmp_path)
    real_fstat = os.fstat
    calls = 0

    def changing_fstat(fd: int) -> os.stat_result:
        nonlocal calls
        result = real_fstat(fd)
        calls += 1
        if calls == 2:
            values = list(result)
            values[6] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(approval_module.os, "fstat", changing_fstat)

    with pytest.raises(OSError, match="changed|变化"):
        build_approval(
            run_id="run-001",
            project_id="project-001",
            config_hash=CONFIG_HASH,
            base_branch="develop",
            base_sha=BASE_SHA,
            test_branch="test",
            files=files,
            user_provider=lambda: "JoySun",
            clock=lambda: NOW,
        )


def test_approval_rejects_hardlinked_artifact(tmp_path: Path) -> None:
    requirement, design, plan = write_artifacts(tmp_path)
    outside = tmp_path / "outside-design.md"
    design.unlink()
    outside.write_text("design", encoding="utf-8")
    os.link(outside, design)

    with pytest.raises(ValueError, match="hardlink"):
        build_approval(
            run_id="run-hardlink",
            project_id="project-001",
            config_hash=CONFIG_HASH,
            base_branch="develop",
            base_sha=BASE_SHA,
            test_branch="test",
            files=(requirement, design, plan),
            user_provider=lambda: "JoySun",
            clock=lambda: NOW,
        )


def test_save_load_roundtrip_is_stable_and_contains_no_absolute_paths(
    tmp_path: Path,
) -> None:
    snapshot, _ = build_snapshot(tmp_path)
    destination = tmp_path / "approval.json"

    save_approval(destination, snapshot)
    first = destination.read_bytes()
    loaded = load_approval(destination)
    save_approval(destination, loaded)

    assert loaded == snapshot
    assert destination.read_bytes() == first
    assert str(tmp_path).encode() not in first
    assert json.loads(first)["approved_at"] == "2026-07-28T08:30:00Z"


def test_save_atomically_replaces_existing_file_and_leaves_no_temp(
    tmp_path: Path,
) -> None:
    snapshot, _ = build_snapshot(tmp_path)
    destination = tmp_path / "approval.json"
    destination.write_text("old", encoding="utf-8")

    save_approval(destination, snapshot)

    assert destination.read_text(encoding="utf-8").startswith("{")
    assert list(tmp_path.glob(f".{destination.name}.*.tmp")) == []


def test_failed_replace_preserves_old_file_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _ = build_snapshot(tmp_path)
    destination = tmp_path / "approval.json"
    destination.write_text("old approval", encoding="utf-8")

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(approval_module, "_atomic_replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        save_approval(destination, snapshot)

    assert destination.read_text(encoding="utf-8") == "old approval"
    assert list(tmp_path.glob(f".{destination.name}.*.tmp")) == []


def test_cleanup_failure_does_not_mask_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _ = build_snapshot(tmp_path)
    destination = tmp_path / "approval.json"
    destination.write_text("old approval", encoding="utf-8")

    monkeypatch.setattr(
        approval_module,
        "_atomic_replace",
        lambda source, target: (_ for _ in ()).throw(OSError("replace failed")),
    )
    monkeypatch.setattr(
        approval_module,
        "_remove_temporary",
        lambda path: (_ for _ in ()).throw(OSError("cleanup failed")),
    )

    with pytest.raises(OSError, match="replace failed") as caught:
        save_approval(destination, snapshot)

    assert any("cleanup failed" in note for note in caught.value.__notes__)
    assert destination.read_text(encoding="utf-8") == "old approval"


@pytest.mark.parametrize("contents", ["not json", "{}", '{"base_sha":"bad"}'])
def test_load_rejects_corrupt_or_incomplete_json(
    tmp_path: Path,
    contents: str,
) -> None:
    destination = tmp_path / "approval.json"
    destination.write_text(contents, encoding="utf-8")

    with pytest.raises((ValueError, ValidationError)):
        load_approval(destination)


def test_naive_clock_is_rejected(tmp_path: Path) -> None:
    files = write_artifacts(tmp_path)
    naive = NOW.replace(tzinfo=None)
    with pytest.raises(ValidationError, match="timezone|UTC"):
        build_approval(
            run_id="run-001",
            project_id="project-001",
            config_hash=CONFIG_HASH,
            base_branch="develop",
            base_sha=BASE_SHA,
            test_branch="test",
            files=files,
            approved_user="JoySun",
            user_provider=lambda: "JoySun",
            clock=lambda: naive,
        )


def test_approved_user_must_match_trusted_provider(tmp_path: Path) -> None:
    files = write_artifacts(tmp_path)

    with pytest.raises(ValueError, match="approved_user|user"):
        build_approval(
            run_id="run-001",
            project_id="project-001",
            config_hash=CONFIG_HASH,
            base_branch="develop",
            base_sha=BASE_SHA,
            test_branch="test",
            files=files,
            approved_user="forged-user",
            user_provider=lambda: "trusted-user",
            clock=lambda: NOW,
        )

    snapshot = build_approval(
        run_id="run-001",
        project_id="project-001",
        config_hash=CONFIG_HASH,
        base_branch="develop",
        base_sha=BASE_SHA,
        test_branch="test",
        files=files,
        user_provider=lambda: "trusted-user",
        clock=lambda: NOW,
    )
    assert snapshot.approved_user == "trusted-user"
