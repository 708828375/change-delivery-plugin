from pathlib import Path

import pytest

from change_delivery.artifacts import ArtifactStore


def test_artifact_store_creates_only_canonical_run_paths(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    paths = store.create_run("CD-20260728-0001")

    assert paths.requirement.name == "requirement.md"
    assert paths.design.name == "design.md"
    assert paths.plan.name == "implementation-plan.md"
    assert paths.approval.name == "approval.json"
    assert paths.evidence_dir.is_dir()
    assert paths.reports_dir.is_dir()
    assert paths.sanitized_summary.name == "sanitized-summary.md"
    assert {item.name for item in paths.run_dir.iterdir()} == {
        "requirement.md", "design.md", "implementation-plan.md",
        "approval.json", "evidence", "reports", "sanitized-summary.md",
    }


@pytest.mark.parametrize("run_id", ("../escape", "a/b", ".", "..", " bad"))
def test_artifact_store_rejects_noncanonical_run_id(
    tmp_path: Path, run_id: str
) -> None:
    with pytest.raises(ValueError):
        ArtifactStore(tmp_path).create_run(run_id)


def test_cancel_inventory_lists_resources_without_deleting(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    paths = store.create_run("CD-1")
    resource = tmp_path / "still-here"
    resource.write_text("keep", encoding="utf-8")

    summary = store.write_resource_inventory(
        "CD-1",
        branch="feature/CD-1",
        worktree=resource,
        pr_url="https://github.com/example/repo/pull/1",
        pr_number=1,
        test_sha="a" * 40,
    )

    assert resource.exists()
    text = summary.read_text(encoding="utf-8")
    assert "feature/CD-1" in text
    assert str(resource) in text
    assert "#1" in text
    assert summary == paths.reports_dir / "resource-inventory.md"


def test_artifact_store_rejects_reparse_run_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ArtifactStore(tmp_path)
    real = store._is_reparse
    monkeypatch.setattr(store, "_is_reparse", lambda path: path.name == "runs" or real(path))
    with pytest.raises(ValueError, match="reparse"):
        store.create_run("CD-1")

def test_create_run_refuses_existing_path_without_modifying_it(tmp_path: Path) -> None:
    existing = tmp_path / "runs" / "CD-1"
    existing.mkdir(parents=True)
    marker = existing / "owner.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        ArtifactStore(tmp_path).create_run("CD-1")
    assert marker.read_text(encoding="utf-8") == "keep"
