from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[2]
SECURITY = ROOT / "scripts" / "host-security.ps1"
SCHEDULED = ROOT / "scripts" / "install-scheduled-task.ps1"
TICK = ROOT / "scripts" / "run-tick.ps1"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def test_scheduler_uses_the_installed_shared_layout() -> None:
    scheduled = text(SCHEDULED)
    tick = text(TICK)
    security = text(SECURITY)
    combined = scheduled + tick + security

    assert "C:\\Users\\JoySun" not in combined
    assert "C:\\softs\\" not in combined
    assert "C:\\Program Files\\GitHub CLI" not in combined
    assert "shared-layout.ps1" in scheduled
    assert "Get-SharedInstallLayout" in scheduled
    assert "$layout.host_root" in scheduled
    assert "$layout.venv_root" in scheduled
    assert "Get-SharedInstallLayout" in tick
    assert "$layout.host_root" in tick
    assert "Assert-FixedSourceRoot" in scheduled


def test_external_tools_are_discovered_once_then_pinned_by_identity() -> None:
    security = text(SECURITY)

    assert "Get-Command codex.cmd" in security
    assert "Get-Command gh.exe" in security
    assert "Resolve-CodexIdentityAtRoot" in security
    assert "Get-FileSha256 $binary" in security
    assert "Get-FileSha256 $path" in security


def test_runtime_python_identity_is_bound_to_the_private_venv() -> None:
    security = text(SECURITY)

    assert "FixedPythonPath" not in security
    assert "Get-SharedInstallLayout" in security
    assert "Join-Path $layout.venv_root 'Scripts\\python.exe'" in security
    assert "$contract.minor -lt 12" in security
