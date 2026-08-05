from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[2]
UNINSTALLER = ROOT / "scripts" / "uninstall-shared-plugin.ps1"


def test_uninstaller_preserves_user_data_by_default() -> None:
    script = UNINSTALLER.read_text(encoding="utf-8-sig")

    assert "[switch] $PurgeData" in script
    assert "projects" in script
    assert "runs" in script
    assert "logs" in script
    assert "state" in script
    assert "if ($PurgeData)" in script
    assert "ShouldProcess" in script
    assert "Assert-ManagedPluginRelease" in script
    assert "Assert-ClosedRuntimeHostTree" in script
    assert "plugin remove" in script
    assert "plugin list --json" in script
    assert "if ($installed.Count -eq 1)" in script
    assert "Unregister-ScheduledTask" in script
    assert "C:\\Users\\JoySun" not in script
    assert "host-security.ps1" in script
    assert "$codexPath = (Resolve-OfficialCodexIdentity).path" in script
    assert "& codex " not in script
    assert "$actions.Count -ne 1" in script
    assert "$arguments -cne $expectedArguments" in script
    assert ".Contains($expectedTick)" not in script
    assert "Test-StrictTaskDefinition" in script


def test_uninstaller_uses_native_exact_path_deletion() -> None:
    script = UNINSTALLER.read_text(encoding="utf-8-sig")

    assert "[IO.Directory]::Delete" in script
    assert "[IO.File]::Delete" in script
    assert "cmd /c" not in script
    assert "Remove-Item -Recurse" not in script
    assert "$layout.data_root" in script
    assert "$layout.plugin_root" in script


def test_uninstaller_parses_as_powershell() -> None:
    command = (
        "[void][scriptblock]::Create("
        f"[IO.File]::ReadAllText('{UNINSTALLER}',"
        "[Text.UTF8Encoding]::new($false,$true)))"
    )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
