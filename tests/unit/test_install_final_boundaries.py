from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[2]
SECURITY = ROOT / "scripts" / "host-security.ps1"
PERSONAL = ROOT / "scripts" / "install-personal-plugin.ps1"
SCHEDULED = ROOT / "scripts" / "install-scheduled-task.ps1"


def run_ps(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
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


def test_real_first_install_flow_roundtrips_host_before_mock_registration(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    marker = tmp_path / "registered.txt"
    command = rf"""
$script = [IO.File]::ReadAllText(
  '{SCHEDULED}', [Text.UTF8Encoding]::new($false, $true)
)
$injected = @'
function Assert-ModuleTreeManifest {{}}
function Assert-NoPythonBytecode {{}}
function Assert-ClosedPythonModuleRoot {{}}
function Resolve-TrustedPythonIdentity {{
  [PSCustomObject]@{{ path='C:\softs\python-3.12.10\python.exe'; sha256=('a'*64) }}
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
  ". `$helperPath`r`n",
  ". `$helperPath`r`n" + $injected + "`r`n"
)
$script = $script.Replace(
  '$runtimeRoot = Join-Path $userProfile ''.codex\change-delivery''',
  '$runtimeRoot = ''{runtime_root}'''
)
$script = $script.Replace(
  '$helperPath = Join-Path $PSScriptRoot ''host-security.ps1''',
  '$helperPath = ''{SECURITY}'''
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
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(
  '{PERSONAL}', [ref]$tokens, [ref]$errors
)
$functionAst = $ast.Find({{
  param($node)
  $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -ceq 'Assert-SafePluginsRoot'
}}, $true)
. ([scriptblock]::Create($functionAst.Extent.Text))
New-Item -ItemType Junction -Path '{plugins}' -Target '{real}' | Out-Null
function python {{ Set-Content -LiteralPath '{marker}' -Value python }}
function codex {{ Set-Content -LiteralPath '{marker}' -Value codex }}
$rejected = $false
try {{ Assert-SafePluginsRoot '{plugins}' '{plugins}' | Out-Null }}
catch {{ $rejected = $true }}
if (-not $rejected) {{ exit 73 }}
if (Test-Path -LiteralPath '{marker}') {{ exit 74 }}
if (Test-Path -LiteralPath (Join-Path '{plugins}' 'change-delivery-plugin')) {{ exit 75 }}
"""
    result = run_ps(command)
    assert result.returncode == 0, result.stderr + result.stdout
    assert not marker.exists()


def test_security_checks_precede_plugin_validation_and_registration() -> None:
    personal = PERSONAL.read_text(encoding="utf-8")
    scheduled = SCHEDULED.read_text(encoding="utf-8")

    assert personal.index(
        "$plugins = Assert-SafePluginsRoot $fixedPluginsRoot $fixedPluginsRoot"
    ) < personal.index("validate_plugin.py")
    assert personal.index("$lexicalUserProfile") < personal.index(
        "validate_plugin.py"
    )
    assert scheduled.index("Write-RuntimeHost $runtimeRoot $configuration") < (
        scheduled.index("Register-ScheduledTask")
    )


def test_clean_release_is_validated_before_activation_and_registration() -> None:
    personal = PERSONAL.read_text(encoding="utf-8")

    assert personal.index("New-CleanPluginStage") < personal.index(
        "& python $validator $stage"
    )
    assert personal.index("& python $validator $stage") < personal.index(
        "Install-CleanPluginStage"
    )
    assert personal.index("Install-CleanPluginStage") < personal.index("plugin add")
