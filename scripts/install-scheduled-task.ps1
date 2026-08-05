[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('auth_file', 'api_key')]
    [string] $AuthMode,
    [string] $AuthSource,
    [switch] $Doctor,
    [string] $SourceRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$TrustedHostSecuritySha256 = '6e1e0e6cfd1d112371aed4fa43bd85ea37b3a93894a30b903a199412c4f19e77'
$TrustedSharedLayoutSha256 = '4e0dc6e7febe6650c9dde5c636b3959a2388f00bcf372411f1ea7fd305f8aff1'
function Open-StableTrustedHelper([string] $Path, [string] $ExpectedHash) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    $cursor = $item
    while ($null -ne $cursor) {
        if (($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Trusted helper path contains a reparse point.'
        }
        $cursor = if ($cursor -is [IO.DirectoryInfo]) { $cursor.Parent } else { $cursor.Directory }
    }
    if ($item -isnot [IO.FileInfo]) { throw 'Trusted helper must be an ordinary file.' }
    $links = @(& 'C:\Windows\System32\fsutil.exe' hardlink list $item.FullName)
    if ($LASTEXITCODE -ne 0 -or $links.Count -ne 1) {
        throw 'Trusted helper must have exactly one hard link.'
    }
    $stream = [IO.File]::Open($item.FullName, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        $algorithm = [Security.Cryptography.SHA256]::Create()
        try { $hash = [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
        finally { $algorithm.Dispose() }
        if ($hash -cne $ExpectedHash) { throw 'Trusted helper hash differs from the embedded anchor.' }
        $stream.Position = 0
        return $stream
    }
    catch { $stream.Dispose(); throw }
}
$layoutPath = Join-Path $PSScriptRoot 'shared-layout.ps1'
$layoutStream = Open-StableTrustedHelper $layoutPath $TrustedSharedLayoutSha256
. $layoutPath
$layout = Get-SharedInstallLayout
$helperPath = Join-Path $PSScriptRoot 'host-security.ps1'
$helperStream = Open-StableTrustedHelper $helperPath $TrustedHostSecuritySha256
. $helperPath

$taskName = 'Codex Change Delivery Tick'
if ([string]::IsNullOrWhiteSpace($SourceRoot)) {
    $SourceRoot = $layout.host_root
}
$fixedRoot = Assert-FixedSourceRoot $SourceRoot
$tickPath = Resolve-RegularFile (
    Join-Path $fixedRoot 'scripts\run-tick.ps1'
) 'run-tick.ps1'
$moduleRoot = (Resolve-Path -LiteralPath (
    Join-Path $fixedRoot 'src'
)).ProviderPath
Assert-NoReparseChain $moduleRoot
[void](Resolve-RegularFile (
    Join-Path $moduleRoot 'change_delivery\__init__.py'
) '__init__.py')
Assert-NoPythonBytecode (Join-Path $moduleRoot 'change_delivery')
$moduleManifest = @(Read-TrustedModuleManifest)
Assert-ModuleTreeManifest (Join-Path $moduleRoot 'change_delivery') $moduleManifest
Assert-NoPythonBytecode (Join-Path $moduleRoot 'change_delivery')
Assert-ClosedPythonModuleRoot $moduleRoot $moduleManifest

$python = Resolve-TrustedPythonIdentity
$expectedPython = Resolve-RegularFile (
    Join-Path $layout.venv_root 'Scripts\python.exe'
) 'python.exe'
if ($python.path -cne $expectedPython) {
    throw '专用 Python 不属于当前用户的共享安装布局。'
}
$codex = Resolve-OfficialCodexIdentity
$gh = Resolve-InstalledGitHubCliIdentity
$auth = Resolve-Authentication $AuthMode $AuthSource

if ($Doctor) {
    [PSCustomObject]@{
        auth_mode = $AuthMode
        available = $true
        native_codex_package = $true
        trusted_github_cli = $true
        trusted_python = $true
        fixed_source = $true
    } | ConvertTo-Json -Compress
    return
}

$runtimeRoot = $layout.data_root
$escapedTickPath = $tickPath.Replace('"', '""')
$arguments = (
    '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass ' +
    '-File "' + $escapedTickPath + '"'
)
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$results = @(
    Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
)
if ($results.Count -gt 1) {
    throw 'Multiple scheduled tasks share the fixed name; refusing to continue.'
}

$timerStart = (Get-Date).AddMinutes(1)
if ($results.Count -eq 1) {
    if ($results[0].TaskName -cne $taskName -or $results[0].TaskPath -cne '\') {
        throw 'Scheduled task exists under a non-default path.'
    }
    $timeTriggers = @(
        $results[0].Triggers |
        Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskTimeTrigger' }
    )
    if (
        $timeTriggers.Count -ne 1 -or
        [string]::IsNullOrWhiteSpace([string] $timeTriggers[0].StartBoundary) -or
        -not [string]::IsNullOrWhiteSpace([string] $timeTriggers[0].EndBoundary)
    ) {
        throw 'Existing timer trigger is missing, ambiguous, or expired.'
    }
    $timerStart = [datetime]::Parse([string] $timeTriggers[0].StartBoundary)
    if ($timerStart -gt (Get-Date).AddMinutes(5)) {
        throw 'Existing timer trigger starts too far in the future.'
    }
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$timerTrigger = New-ScheduledTaskTrigger -Once -At $timerStart `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
# Interactive/Limited serialize as InteractiveToken/LeastPrivilege.
$principal = New-ScheduledTaskPrincipal -UserId $currentUser `
    -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -WakeToRun:$false `
    -MultipleInstances IgnoreNew -StartWhenAvailable
$expectedTask = New-ScheduledTask -Action $action `
    -Trigger @($logonTrigger, $timerTrigger) `
    -Principal $principal -Settings $settings

if ($results.Count -eq 1) {
    if (-not (Test-StrictTaskDefinition $results $expectedTask $taskName $currentUser)) {
        throw 'Scheduled task differs from the complete fixed definition; refusing to overwrite it.'
    }
}

$configuration = [ordered]@{
    schema_version = 1
    python_path = $python.path
    python_sha256 = $python.sha256
    module_root = $moduleRoot
    codex_path = $codex.path
    codex_sha256 = $codex.sha256
    gh_path = $gh.path
    gh_sha256 = $gh.sha256
    codex_package_root = $codex.package_root
    codex_parent_version = $codex.parent_version
    codex_platform_dependency = $codex.platform_dependency
    codex_platform_package_name = $codex.platform_package_name
    codex_platform_version = $codex.platform_version
    module_manifest = $moduleManifest
    auth_mode = $AuthMode
    auth_source = $auth
}
Write-RuntimeHost $runtimeRoot $configuration $fixedRoot `
    -AllowManagedReplacement:($results.Count -eq 1)

if ($results.Count -eq 1) {
    Write-Output 'Scheduled task already has the complete fixed definition.'
    return
}
Register-ScheduledTask -TaskName $taskName -InputObject $expectedTask `
    -ErrorAction Stop | Out-Null
Write-Output 'Scheduled task installed for the current interactive user.'
