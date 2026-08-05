from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).parents[2]
SECURITY = ROOT / "scripts" / "host-security.ps1"
PERSONAL = ROOT / "scripts" / "install-personal-plugin.ps1"
SHARED_INSTALLER = ROOT / "scripts" / "install-shared-plugin.ps1"
SHARED_LAYOUT = ROOT / "scripts" / "shared-layout.ps1"
SCHEDULED = ROOT / "scripts" / "install-scheduled-task.ps1"


def run_ps(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )


def test_runtime_host_configuration_carries_the_trusted_module_manifest() -> None:
    script = SCHEDULED.read_text(encoding="utf-8")

    assert "module_manifest = $moduleManifest" in script
    assert script.index("module_manifest = $moduleManifest") < script.index(
        "Write-RuntimeHost"
    )
    security = SECURITY.read_text(encoding="utf-8-sig")
    assert "[switch] $AllowManagedReplacement" in security
    assert "-AllowManagedReplacement:($results.Count -eq 1)" in script
    assert "Install-AtomicRuntimeHostFile" in security
    assert "[IO.File]::Replace($Temporary, $Destination, $backup, $true)" in security


def test_invalid_runtime_host_has_zero_filesystem_side_effects(tmp_path: Path) -> None:
    runtime_root = tmp_path / "missing" / "runtime"
    command = rf"""
. '{SECURITY}'
if ($null -eq (Get-Command Write-RuntimeHost -ErrorAction SilentlyContinue)) {{ exit 69 }}
$invalid = [PSCustomObject]@{{ schema_version = 1 }}
$rejected = $false
try {{ Write-RuntimeHost '{runtime_root}' $invalid '{ROOT}' }}
catch {{ $rejected = $true }}
if (-not $rejected) {{ exit 70 }}
if (Test-Path -LiteralPath '{runtime_root}') {{ exit 71 }}
if (@(Get-ChildItem -LiteralPath '{tmp_path}' -Recurse -Force).Count -ne 0) {{ exit 72 }}
"""
    result = run_ps(command)
    assert result.returncode == 0, result.stderr + result.stdout
    assert not runtime_root.exists()


def test_managed_runtime_host_can_be_atomically_rotated_for_upgrade(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    command = rf"""
. '{SECURITY}'
function Assert-StrictRuntimeHost {{}}
$old = [ordered]@{{}}
foreach ($field in $script:RuntimeHostFields) {{ $old[$field] = 'value' }}
$old.schema_version = 1
$old.module_root = 'C:\managed\host\src'
$old.auth_mode = 'api_key'
$old.auth_source = $null
$old.module_manifest = @()
$old.python_sha256 = ('a' * 64)
Write-RuntimeHost '{runtime_root}' $old 'C:\managed\host'
$new = [ordered]@{{}}
foreach ($field in $script:RuntimeHostFields) {{ $new[$field] = $old[$field] }}
$new.python_sha256 = ('b' * 64)
Write-RuntimeHost '{runtime_root}' $new 'C:\managed\host' -AllowManagedReplacement
$loaded = Read-StrictJsonObject (Join-Path '{runtime_root}' 'runtime-host.json') 16KB
if ($loaded.python_sha256 -cne ('b' * 64)) {{ exit 68 }}
if (@(Get-ChildItem -LiteralPath '{runtime_root}' -Filter '*.backup-*').Count -ne 0) {{ exit 69 }}
"""

    result = run_ps(command)

    assert result.returncode == 0, result.stderr + result.stdout


def test_real_first_install_flow_roundtrips_host_before_mock_registration(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    marker = tmp_path / "registered.txt"
    venv_root = tmp_path / "venv"
    venv_python = venv_root / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    shutil.copy2(sys.executable, venv_python)
    command = rf"""
$script = [IO.File]::ReadAllText(
  '{SCHEDULED}', [Text.UTF8Encoding]::new($false, $true)
)
$injected = @'
function Assert-ModuleTreeManifest {{}}
function Assert-NoPythonBytecode {{}}
function Assert-ClosedPythonModuleRoot {{}}
function Assert-FixedSourceRoot {{ return '{ROOT}' }}
function Resolve-TrustedPythonIdentity {{
  [PSCustomObject]@{{ path='{venv_python}'; sha256=('a'*64) }}
}}
function Resolve-OfficialCodexIdentity {{
  [PSCustomObject]@{{
    path='C:\fixed\codex.exe'; sha256=('b'*64)
    package_root='C:\fixed\package'; parent_version='1.2.3'
    platform_dependency='@openai/codex-win32-x64'
    platform_package_name='@openai/codex'
    platform_version='1.2.3-win32-x64'
  }}
}}
function Resolve-Authentication {{ return $null }}
function Get-ScheduledTask {{ return }}
function New-ScheduledTaskAction {{ [PSCustomObject]@{{ kind='action' }} }}
function New-ScheduledTaskTrigger {{ [PSCustomObject]@{{ kind='trigger' }} }}
function New-ScheduledTaskPrincipal {{ [PSCustomObject]@{{ kind='principal' }} }}
function New-ScheduledTaskSettingsSet {{ [PSCustomObject]@{{ kind='settings' }} }}
function New-ScheduledTask {{ [PSCustomObject]@{{ kind='task' }} }}
function Register-ScheduledTask {{
  param($TaskName, $InputObject)
  $hostPath = Join-Path '{runtime_root}' 'runtime-host.json'
  if (-not (Test-Path -LiteralPath $hostPath -PathType Leaf)) {{ exit 76 }}
  $roundTrip = Read-StrictJsonObject $hostPath 16KB
  Assert-StrictRuntimeHost $roundTrip '{ROOT}' | Out-Null
  if (@($roundTrip.module_manifest).Count -eq 0) {{ exit 77 }}
  Set-Content -LiteralPath '{marker}' -Value $TaskName
}}
'@
$script = $script.Replace(
  ". `$helperPath",
  ". `$helperPath" + "`r`n" + $injected + "`r`n"
)
$script = $script.Replace(
  '$layoutPath = Join-Path $PSScriptRoot ''shared-layout.ps1''',
  '$layoutPath = ''{SHARED_LAYOUT}'''
)
$script = $script.Replace(
  '$helperPath = Join-Path $PSScriptRoot ''host-security.ps1''',
  '$helperPath = ''{SECURITY}'''
)
$script = $script.Replace(
  '$layout = Get-SharedInstallLayout',
  '$layout = [PSCustomObject]@{{ host_root=''{ROOT}''; data_root=''{runtime_root}''; venv_root=''{venv_root}'' }}'
)
$env:OPENAI_API_KEY = 'test-only'
& ([scriptblock]::Create($script)) -AuthMode api_key -SourceRoot '{ROOT}'
if ($LASTEXITCODE -ne 0) {{ exit $LASTEXITCODE }}
if (-not (Test-Path -LiteralPath '{marker}' -PathType Leaf)) {{ exit 78 }}
if (@(Get-ChildItem -LiteralPath '{runtime_root}' -Filter '*.tmp' -Force).Count -ne 0) {{ exit 79 }}
"""
    result = run_ps(command)
    assert result.returncode == 0, result.stderr + result.stdout
    assert marker.exists(), result.stderr + result.stdout + repr(result.returncode)
    assert marker.read_text(encoding="utf-8").strip() == "Codex Change Delivery Tick"


def test_plugins_root_reparse_is_rejected_without_install_side_effects(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    plugins = tmp_path / "plugins"
    marker = tmp_path / "called.txt"
    command = rf"""
. '{SHARED_LAYOUT}'
New-Item -ItemType Junction -Path '{plugins}' -Target '{real}' | Out-Null
function python {{ Set-Content -LiteralPath '{marker}' -Value python }}
function codex {{ Set-Content -LiteralPath '{marker}' -Value codex }}
$rejected = $false
try {{ Resolve-CanonicalOrdinaryDirectory '{plugins}' 'Plugins root' | Out-Null }}
catch {{ $rejected = $true }}
if (-not $rejected) {{ exit 73 }}
if (Test-Path -LiteralPath '{marker}') {{ exit 74 }}
if (Test-Path -LiteralPath (Join-Path '{plugins}' 'change-delivery-plugin')) {{ exit 75 }}
"""
    result = run_ps(command)
    assert result.returncode == 0, result.stderr + result.stdout
    assert not marker.exists()


def test_security_checks_precede_plugin_validation_and_registration() -> None:
    shared = SHARED_INSTALLER.read_text(encoding="utf-8")
    scheduled = SCHEDULED.read_text(encoding="utf-8")

    assert shared.index("$layout = Get-SharedInstallLayout") < shared.index(
        "validate_plugin.py"
    )
    assert shared.index("$source = Resolve-CanonicalOrdinaryDirectory") < shared.index(
        "validate_plugin.py"
    )
    assert scheduled.index("Write-RuntimeHost $runtimeRoot $configuration") < (
        scheduled.index("Register-ScheduledTask")
    )


def test_clean_release_is_validated_before_activation_and_registration() -> None:
    shared = SHARED_INSTALLER.read_text(encoding="utf-8")

    assert shared.index("New-CleanPluginStage") < shared.index(
        "& $bootstrapPython $validator $pluginStage"
    )
    assert shared.index("& $bootstrapPython $validator $pluginStage") < shared.index(
        "Install-CleanPluginStage"
    )
    assert shared.index("Install-CleanPluginStage") < shared.rindex(
        "& $codexPath plugin add $pluginReference"
    )
