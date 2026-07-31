[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('auth_file', 'api_key')]
    [string] $AuthMode,
    [string] $AuthSource,
    [switch] $Doctor,
    [string] $SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string] $NpmGlobalRoot,
    [string] $PythonPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$taskName = 'Codex Change Delivery Tick'
$utf8 = [Text.UTF8Encoding]::new($false, $true)

function Assert-NoReparseChain([string] $Path) {
    if (-not [IO.Path]::IsPathRooted($Path)) {
        throw 'A security-sensitive path is not absolute.'
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    while ($null -ne $item) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'A security-sensitive path contains a reparse point.'
        }
        $item = $item.Parent
    }
}

function Resolve-RegularFile([string] $Path, [string] $ExpectedName) {
    if (-not [IO.Path]::IsPathRooted($Path)) {
        throw 'Executable or credential path must be absolute.'
    }
    Assert-NoReparseChain $Path
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.PSIsContainer -or $item.LinkType) {
        throw 'Expected an ordinary non-link file.'
    }
    if (
        $ExpectedName -and
        -not [string]::Equals(
            [IO.Path]::GetFileName($item.FullName),
            $ExpectedName,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "Expected file basename $ExpectedName."
    }
    return $item.FullName
}

function Find-NativeCodex([string] $GlobalRoot) {
    if (-not $GlobalRoot) {
        $rootOutput = & npm root -g
        if ($LASTEXITCODE -ne 0 -or -not $rootOutput) {
            throw 'Unable to discover the global npm package root.'
        }
        $GlobalRoot = [string] $rootOutput[-1]
    }
    $global = (Resolve-Path -LiteralPath $GlobalRoot -ErrorAction Stop).ProviderPath
    $packageRoot = Join-Path $global '@openai\codex'
    Assert-NoReparseChain $packageRoot
    $package = (Resolve-Path -LiteralPath $packageRoot -ErrorAction Stop).ProviderPath
    $platformRoot = Join-Path $package 'node_modules\@openai'
    $candidates = @(
        Get-ChildItem -LiteralPath $platformRoot -Recurse -File `
            -Filter 'codex.exe' -ErrorAction Stop |
        Where-Object {
            $_.FullName -match (
                '(?i)\\node_modules\\@openai\\codex-[^\\]+' +
                '\\vendor\\[^\\]+\\bin\\codex\.exe$'
            )
        }
    )
    if ($candidates.Count -ne 1) {
        throw 'Exactly one official native Codex binary is required.'
    }
    $native = Resolve-RegularFile $candidates[0].FullName 'codex.exe'
    if (-not $native.StartsWith($package + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Native Codex binary escaped the official package root.'
    }
    $version = & $native --version
    if ($LASTEXITCODE -ne 0 -or -not (($version -join '') -match '^codex-cli\s+\S+')) {
        throw 'Native Codex binary failed its version contract.'
    }
    return $native
}

function Resolve-Authentication([string] $Mode, [string] $Source) {
    if ($Mode -eq 'auth_file') {
        if (-not $Source) {
            throw 'auth_file mode requires AuthSource.'
        }
        $auth = Resolve-RegularFile $Source 'auth.json'
        $item = Get-Item -LiteralPath $auth -Force
        if ($item.Length -gt 1MB -or $item.Length -eq 0) {
            throw 'auth.json has an invalid size.'
        }
        $stream = [IO.File]::Open(
            $auth, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
        )
        try {
            $reader = [IO.StreamReader]::new($stream, $utf8, $true, 4096, $true)
            try {
                $parsed = ConvertFrom-Json $reader.ReadToEnd()
            }
            finally {
                $reader.Dispose()
            }
            if ($null -eq $parsed -or $parsed -isnot [PSCustomObject]) {
                throw 'auth.json must contain one JSON object.'
            }
        }
        finally {
            $stream.Dispose()
        }
        return $auth
    }
    if ($Source) {
        throw 'api_key mode forbids AuthSource.'
    }
    if (
        [string]::IsNullOrWhiteSpace(
            [Environment]::GetEnvironmentVariable('OPENAI_API_KEY', 'Process')
        )
    ) {
        throw 'api_key mode requires OPENAI_API_KEY in the current process.'
    }
    return $null
}

function Resolve-Python([string] $RequestedPath) {
    if (-not $RequestedPath) {
        $RequestedPath = (
            Get-Command python.exe -CommandType Application -ErrorAction Stop
        ).Source
    }
    $python = Resolve-RegularFile $RequestedPath ''
    if ([IO.Path]::GetFileName($python) -notmatch '^(?i:python(?:3(?:\.\d+)?)?\.exe)$') {
        throw 'Python interpreter basename is not approved.'
    }
    return $python
}

function Write-RuntimeHost(
    [string] $RuntimeRoot,
    [System.Collections.IDictionary] $Configuration
) {
    $parent = Split-Path -Parent $RuntimeRoot
    Assert-NoReparseChain $parent
    if (-not (Test-Path -LiteralPath $RuntimeRoot)) {
        New-Item -ItemType Directory -Path $RuntimeRoot -ErrorAction Stop | Out-Null
    }
    Assert-NoReparseChain $RuntimeRoot
    $destination = Join-Path $RuntimeRoot 'runtime-host.json'
    $json = (ConvertTo-Json $Configuration -Compress) + [Environment]::NewLine
    if (Test-Path -LiteralPath $destination) {
        $existing = Resolve-RegularFile $destination 'runtime-host.json'
        $current = [IO.File]::ReadAllText($existing, $utf8)
        if ($current -cne $json) {
            throw 'Existing runtime-host.json differs; refusing to overwrite it.'
        }
        return
    }
    $temporary = Join-Path $RuntimeRoot (
        '.runtime-host.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    )
    $stream = [IO.File]::Open(
        $temporary,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $bytes = $utf8.GetBytes($json)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
    try {
        [IO.File]::Move($temporary, $destination)
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            [IO.File]::Delete($temporary)
        }
    }
    $roundTrip = ConvertFrom-Json ([IO.File]::ReadAllText($destination, $utf8))
    foreach ($field in @('python_path', 'module_root', 'codex_path', 'auth_mode')) {
        if ($null -eq $roundTrip.$field) {
            throw 'Runtime host verification failed.'
        }
    }
}

function Test-ScheduledTaskDefinition(
    [object] $Existing,
    [string] $ExpectedArguments,
    [string] $ExpectedUser
) {
    $actions = @($Existing.Actions)
    $triggers = @($Existing.Triggers)
    if (
        $actions.Count -ne 1 -or
        $actions[0].Execute -ne 'powershell.exe' -or
        $actions[0].Arguments -ne $ExpectedArguments -or
        $actions[0].WorkingDirectory
    ) {
        return $false
    }
    if (
        $Existing.Principal.UserId -ne $ExpectedUser -or
        [int] $Existing.Principal.LogonType -ne 3 -or
        [int] $Existing.Principal.RunLevel -ne 0
    ) {
        return $false
    }
    if (
        $Existing.Settings.WakeToRun -ne $false -or
        $Existing.Settings.MultipleInstances -ne 'IgnoreNew'
    ) {
        return $false
    }
    $logon = @(
        $triggers |
        Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' }
    )
    $timer = @(
        $triggers |
        Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskTimeTrigger' }
    )
    return (
        $logon.Count -eq 1 -and
        $timer.Count -eq 1 -and
        $timer[0].Repetition.Interval -eq 'PT5M' -and
        $timer[0].Enabled -ne $false
    )
}

$source = (Resolve-Path -LiteralPath $SourceRoot -ErrorAction Stop).ProviderPath
Assert-NoReparseChain $source
$tickPath = Resolve-RegularFile (
    Join-Path $source 'scripts\run-tick.ps1'
) 'run-tick.ps1'
$moduleRoot = (Resolve-Path -LiteralPath (Join-Path $source 'src')).ProviderPath
Assert-NoReparseChain $moduleRoot
[void](Resolve-RegularFile (
    Join-Path $moduleRoot 'change_delivery\__init__.py'
) '__init__.py')
$codexPath = Find-NativeCodex $NpmGlobalRoot
$python = Resolve-Python $PythonPath
$auth = Resolve-Authentication $AuthMode $AuthSource

if ($Doctor) {
    [PSCustomObject]@{
        auth_mode = $AuthMode
        available = $true
        native_codex = $true
        python = $true
    } | ConvertTo-Json -Compress
    return
}

$userProfile = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
$runtimeRoot = Join-Path $userProfile '.codex\change-delivery'
$arguments = (
    '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass ' +
    '-File "' + $tickPath + '"'
)
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    if (-not (Test-ScheduledTaskDefinition $existing $arguments $currentUser)) {
        throw 'Scheduled task exists with a different definition; refusing to overwrite it.'
    }
}

$configuration = [ordered]@{
    python_path = $python
    module_root = $moduleRoot
    codex_path = $codexPath
    auth_mode = $AuthMode
    auth_source = $auth
}
Write-RuntimeHost $runtimeRoot $configuration
if ($null -ne $existing) {
    Write-Output 'Scheduled task already has the required definition.'
    return
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$timerTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
# The ScheduledTasks cmdlet names the InteractiveToken/LeastPrivilege XML
# contract values Interactive and Limited respectively.
$principal = New-ScheduledTaskPrincipal -UserId $currentUser `
    -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -WakeToRun:$false `
    -MultipleInstances IgnoreNew -StartWhenAvailable
$task = New-ScheduledTask -Action $action -Trigger @($logonTrigger, $timerTrigger) `
    -Principal $principal -Settings $settings
Register-ScheduledTask -TaskName $taskName -InputObject $task -ErrorAction Stop |
    Out-Null
Write-Output 'Scheduled task installed for the current interactive user.'
