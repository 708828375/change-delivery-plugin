from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[2]
PERSONAL = ROOT / "scripts" / "install-personal-plugin.ps1"
PUBLISHER = ROOT / "scripts" / "plugin-publisher.ps1"
SCHEDULED = ROOT / "scripts" / "install-scheduled-task.ps1"
TICK = ROOT / "scripts" / "run-tick.ps1"
SECURITY = ROOT / "scripts" / "host-security.ps1"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_install_scripts_parse_as_powershell() -> None:
    paths = ", ".join(
        f"'{path}'" for path in (PERSONAL, PUBLISHER, SCHEDULED, TICK, SECURITY)
    )
    command = (
        f"$files=@({paths}); foreach($file in $files){{"
        "[void][scriptblock]::Create("
        "[IO.File]::ReadAllText($file,[Text.UTF8Encoding]::new($false,$true)))"
        "}"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_personal_installer_is_fail_closed_and_uses_codex_marketplace_api() -> None:
    script = _text(PERSONAL)
    publisher = _text(PUBLISHER)

    assert "Resolve-Path" in script
    assert "plugin-publisher.ps1" in script
    assert "New-CleanPluginStage" in script
    assert "Install-CleanPluginStage" in script
    assert "New-Item -ItemType Junction -Path $target" not in script
    assert "$allowed = @('.codex-plugin', 'skills')" in publisher
    assert "validate_plugin.py" in script
    assert "plugin marketplace list --json" in script
    assert "plugin add" in script
    assert "change-delivery-plugin@" in script
    assert "marketplace.json" not in script
    assert "Remove-Item" not in script


def test_personal_installer_resolves_default_source_after_parameter_binding() -> None:
    script = _text(PERSONAL)

    assert "[string] $SourceRoot = (Split-Path -Parent $PSScriptRoot)" not in script
    assert "if ([string]::IsNullOrWhiteSpace($SourceRoot))" in script
    assert "$SourceRoot = Split-Path -Parent $PSScriptRoot" in script


def test_scheduler_installer_pins_native_codex_auth_and_runtime_host() -> None:
    script = _text(SCHEDULED) + _text(SECURITY)

    assert "@openai\\codex" in script
    assert "vendor" in script
    assert "codex.exe" in script
    assert "Get-Command codex" not in script
    assert "codex.cmd" not in script
    assert "codex.ps1" not in script
    assert "ReparsePoint" in script
    assert "--version" in script
    assert "[ValidateSet('auth_file', 'api_key')]" in script
    assert "OPENAI_API_KEY" in script
    assert "runtime-host.json" in script
    assert "gh_path" in script
    assert "gh_sha256" in script
    assert "FileMode]::CreateNew" in script
    assert "Codex Change Delivery Tick" in script
    assert "InteractiveToken" in script
    assert "LeastPrivilege" in script
    assert "-WakeToRun:$false" in script
    assert "IgnoreNew" in script
    assert "Get-ScheduledTask" in script
    assert "Unregister-ScheduledTask" not in script


def test_tick_consumes_explicit_host_auth_and_native_codex() -> None:
    script = _text(TICK)

    assert "auth_mode" in script
    assert "auth_source" in script
    assert "codex_path" in script
    assert "gh_path" in script
    assert "OPENAI_API_KEY = $null" in script
    assert "CHANGE_DELIVERY_AUTH_MODE" in script
    assert "CHANGE_DELIVERY_AUTH_SOURCE" in script
    assert "CHANGE_DELIVERY_CODEX_EXECUTABLE" in script
    assert "CHANGE_DELIVERY_GH_EXECUTABLE" in script
    assert "CHANGE_DELIVERY_GH_SHA256" in script
