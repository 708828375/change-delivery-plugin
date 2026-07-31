from __future__ import annotations

from pathlib import Path
import subprocess


SECURITY = (
    Path(__file__).parents[2] / "scripts" / "host-security.ps1"
)


def test_disabled_finite_and_heterogeneous_task_definitions_are_rejected() -> None:
    command = rf"""
. '{SECURITY}'
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument 'fixed'
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
$timer = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(-1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -WakeToRun:$false -MultipleInstances IgnoreNew -StartWhenAvailable
$expected = New-ScheduledTask -Action $action -Trigger @($logon,$timer) -Principal $principal -Settings $settings
$disabledLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
$disabledLogon.Enabled = $false
$disabled = New-ScheduledTask -Action $action -Trigger @($disabledLogon,$timer) -Principal $principal -Settings $settings
if (Test-StrictTaskDefinition @($disabled) $expected 'Codex Change Delivery Tick' $user) {{ exit 50 }}
$finiteTimer = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(-1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$finiteTimer.Repetition.Duration = 'PT10M'
$finite = New-ScheduledTask -Action $action -Trigger @($logon,$finiteTimer) -Principal $principal -Settings $settings
if (Test-StrictTaskDefinition @($finite) $expected 'Codex Change Delivery Tick' $user) {{ exit 51 }}
$otherAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument 'different'
$heterogeneous = New-ScheduledTask -Action $otherAction -Trigger @($logon,$timer) -Principal $principal -Settings $settings
if (Test-StrictTaskDefinition @($heterogeneous) $expected 'Codex Change Delivery Tick' $user) {{ exit 52 }}
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
