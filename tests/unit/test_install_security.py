from __future__ import annotations

import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[2]
SCHEDULED = ROOT / "scripts" / "install-scheduled-task.ps1"
SECURITY = ROOT / "scripts" / "host-security.ps1"
TICK = ROOT / "scripts" / "run-tick.ps1"


def run_ps(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )


def test_task_comparison_rejects_disabled_wrong_user_and_expired_trigger() -> None:
    command = rf"""
. '{SECURITY}'
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument 'fixed'
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
$timer = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(-1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -WakeToRun:$false -MultipleInstances IgnoreNew -StartWhenAvailable
$expected = New-ScheduledTask -Action $action -Trigger @($logon,$timer) -Principal $principal -Settings $settings
if (-not (Test-StrictTaskDefinition @($expected) $expected 'Codex Change Delivery Tick' $user)) {{ exit 10 }}
$disabled = New-ScheduledTask -Action $action -Trigger @($logon,$timer) -Principal $principal -Settings $settings
$disabled.Settings.Enabled = $false
if (Test-StrictTaskDefinition @($disabled) $expected 'Codex Change Delivery Tick' $user) {{ exit 11 }}
$wrongPrincipal = New-ScheduledTaskPrincipal -UserId 'OTHER\User' -LogonType Interactive -RunLevel Limited
$wrong = New-ScheduledTask -Action $action -Trigger @($logon,$timer) -Principal $wrongPrincipal -Settings $settings
if (Test-StrictTaskDefinition @($wrong) $expected 'Codex Change Delivery Tick' $user) {{ exit 12 }}
$expiredTimer = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(-10) -RepetitionInterval (New-TimeSpan -Minutes 5)
$expiredTimer.EndBoundary = (Get-Date).AddMinutes(-1).ToString('s')
$expired = New-ScheduledTask -Action $action -Trigger @($logon,$expiredTimer) -Principal $principal -Settings $settings
if (Test-StrictTaskDefinition @($expired) $expected 'Codex Change Delivery Tick' $user) {{ exit 13 }}
if (Test-StrictTaskDefinition @($expected,$expected) $expected 'Codex Change Delivery Tick' $user) {{ exit 14 }}
"""
    result = run_ps(command)
    assert result.returncode == 0, result.stderr + result.stdout


def test_fake_vendor_manifest_version_is_rejected_before_execution(tmp_path: Path) -> None:
    root = tmp_path / "node_modules"
    parent = root / "@openai" / "codex"
    platform = parent / "node_modules" / "@openai" / "codex-win32-x64"
    binary = platform / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"not an executable")
    (parent / "package.json").write_text(
        json.dumps(
            {
                "name": "@openai/codex",
                "version": "1.2.3",
                "optionalDependencies": {
                    "@openai/codex-win32-x64": "npm:@openai/codex@1.2.3-win32-x64"
                },
            }
        ),
        encoding="utf-8",
    )
    (platform / "package.json").write_text(
        json.dumps(
            {
                "name": "@openai/codex",
                "version": "9.9.9-win32-x64",
                "os": ["win32"],
                "cpu": ["x64"],
            }
        ),
        encoding="utf-8",
    )
    command = rf"""
. '{SECURITY}'
$rejected = $false
try {{ Resolve-CodexIdentityAtRoot '{root}' | Out-Null }}
catch {{ if ($_.Exception.Message -notmatch 'version') {{ throw }}; $rejected = $true }}
if (-not $rejected) {{ exit 20 }}
"""
    result = run_ps(command)
    assert result.returncode == 0, result.stderr + result.stdout


def test_runtime_host_rejects_extra_fields_and_mutable_execution_roots() -> None:
    scheduled = SCHEDULED.read_text(encoding="utf-8")
    tick = TICK.read_text(encoding="utf-8")

    assert "Assert-StrictRuntimeHost" in tick
    assert "Assert-FixedSourceRoot" in scheduled
    assert "C:\\Users\\JoySun\\change-delivery-plugin" in scheduled
    assert "$PSScriptRoot\\..\\src" in tick
    contract = SECURITY.read_text(encoding="utf-8")
    assert "python_sha256" in scheduled and "python_sha256" in contract
    assert "codex_sha256" in scheduled and "codex_sha256" in contract
    assert "codex_platform_version" in scheduled and "codex_platform_version" in contract
    assert "Assert-ExactJsonProperties" in contract
    assert "NpmGlobalRoot" not in scheduled
    assert "[string] $PythonPath" not in scheduled


def test_authentication_mode_is_exactly_one() -> None:
    command = rf"""
. '{SECURITY}'
$env:OPENAI_API_KEY = 'test-only'
if ((Resolve-Authentication 'api_key' $null) -ne $null) {{ exit 30 }}
$sourceRejected = $false
try {{ Resolve-Authentication 'api_key' 'C:\fake\auth.json' }} catch {{ $sourceRejected = $true }}
if (-not $sourceRejected) {{ exit 31 }}
$missingRejected = $false
try {{ Resolve-Authentication 'auth_file' $null }} catch {{ $missingRejected = $true }}
if (-not $missingRejected) {{ exit 32 }}
"""
    result = run_ps(command)
    assert result.returncode == 0, result.stderr + result.stdout
