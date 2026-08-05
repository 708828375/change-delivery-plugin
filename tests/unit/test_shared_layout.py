from __future__ import annotations

import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[2]
LAYOUT = ROOT / "scripts" / "shared-layout.ps1"


def run_ps(script: str) -> subprocess.CompletedProcess[str]:
    utf8_script = (
        "$OutputEncoding=[Console]::OutputEncoding="
        "[Text.UTF8Encoding]::new($false); " + script
    )
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            utf8_script,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def ps_literal(path: Path) -> str:
    return str(path).replace("'", "''")


def load_layout(profile: Path) -> dict[str, str]:
    result = run_ps(
        f". '{ps_literal(LAYOUT)}'; "
        f"Get-SharedInstallLayout -UserProfilePath '{ps_literal(profile)}' "
        "| ConvertTo-Json -Compress"
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)


def test_layout_is_derived_from_an_arbitrary_ordinary_profile(tmp_path: Path) -> None:
    profile = tmp_path / "Another User"
    profile.mkdir()

    layout = load_layout(profile)

    expected_profile = str(profile.resolve())
    assert layout["user_profile"] == expected_profile
    assert layout["plugins_root"] == str(profile / "plugins")
    assert layout["plugin_root"] == str(
        profile / "plugins" / "change-delivery-plugin"
    )
    assert layout["data_root"] == str(profile / ".codex" / "change-delivery")
    assert layout["venv_root"] == str(
        profile / ".codex" / "change-delivery" / "runtime" / "venv"
    )
    assert layout["host_root"] == str(
        profile / ".codex" / "change-delivery" / "host"
    )
    assert layout["launcher_path"] == str(
        profile
        / ".codex"
        / "change-delivery"
        / "bin"
        / "change-delivery.ps1"
    )


def test_layout_supports_non_ascii_profile_paths(tmp_path: Path) -> None:
    profile = tmp_path / "共享 用户"
    profile.mkdir()

    layout = load_layout(profile)

    assert layout["user_profile"] == str(profile.resolve())
    assert "共享 用户" in layout["launcher_path"]


def test_production_layout_rejects_userprofile_that_differs_from_known_folder() -> None:
    result = run_ps(
        f"$env:USERPROFILE='C:\\forged-profile'; . '{ps_literal(LAYOUT)}'; "
        "try { Get-SharedInstallLayout | Out-Null } "
        "catch { if ($_.Exception.Message -match 'USERPROFILE') { exit 41 }; throw }; "
        "exit 42"
    )

    assert result.returncode == 41, result.stderr + result.stdout


def test_layout_rejects_a_profile_junction(tmp_path: Path) -> None:
    target = tmp_path / "target"
    link = tmp_path / "linked-profile"
    target.mkdir()
    create = run_ps(
        f"New-Item -ItemType Junction -Path '{ps_literal(link)}' "
        f"-Target '{ps_literal(target)}' | Out-Null"
    )
    assert create.returncode == 0, create.stderr + create.stdout

    result = run_ps(
        f". '{ps_literal(LAYOUT)}'; "
        f"try {{ Get-SharedInstallLayout -UserProfilePath '{ps_literal(link)}' "
        "| Out-Null } catch { exit 43 }; exit 44"
    )

    assert result.returncode == 43, result.stderr + result.stdout
