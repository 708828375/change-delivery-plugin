from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[2]
SECURITY = ROOT / "scripts" / "host-security.ps1"
SCHEDULED = ROOT / "scripts" / "install-scheduled-task.ps1"


def run_ps(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )


def test_fixed_source_and_forged_runtime_host_are_rejected(tmp_path: Path) -> None:
    command = rf"""
. '{SECURITY}'
$rootRejected = $false
try {{ Assert-FixedSourceRoot '{tmp_path}' | Out-Null }} catch {{ $rootRejected = $true }}
if (-not $rootRejected) {{ exit 40 }}
$forged = [PSCustomObject][ordered]@{{
  schema_version=1; python_path='C:\fake\python.exe'; python_sha256=('a'*64)
  module_root='{tmp_path}'; codex_path='C:\fake\codex.exe'; codex_sha256=('b'*64)
  codex_package_root='C:\fake'; codex_parent_version='1.2.3'
  codex_platform_dependency='@openai/codex-win32-x64'
  codex_platform_package_name='@openai/codex'
  codex_platform_version='1.2.3-win32-x64'; auth_mode='api_key'; auth_source=$null
}}
$hostRejected = $false
try {{ Assert-StrictRuntimeHost $forged 'C:\Users\ExampleUser\change-delivery-plugin' | Out-Null }}
catch {{ $hostRejected = $true }}
if (-not $hostRejected) {{ exit 41 }}
$forged | Add-Member -NotePropertyName extra -NotePropertyValue 'attack'
$extraRejected = $false
try {{ Assert-ExactJsonProperties $forged $script:RuntimeHostFields }}
catch {{ $extraRejected = $true }}
if (-not $extraRejected) {{ exit 42 }}
"""
    result = run_ps(command)
    assert result.returncode == 0, result.stderr + result.stdout


def test_mismatch_path_cannot_reach_register_overwrite() -> None:
    script = SCHEDULED.read_text(encoding="utf-8")

    assert "Unregister-ScheduledTask" not in script
    assert "Register-ScheduledTask -Force" not in script
    assert script.index("Test-StrictTaskDefinition") < script.index(
        "Register-ScheduledTask"
    )
    assert script.index("Assert-FixedSourceRoot") < script.index(
        "New-ScheduledTaskAction"
    )
