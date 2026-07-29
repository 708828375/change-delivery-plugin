$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-NoReparseChain([string] $Path) {
    $item = Get-Item -LiteralPath $Path -Force
    while ($null -ne $item) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Runtime host path contains a reparse point."
        }
        $item = $item.Parent
    }
}

function Open-StableFile([string] $Path) {
    if (-not [IO.Path]::IsPathRooted($Path)) {
        throw "Runtime host paths must be absolute."
    }
    Assert-NoReparseChain $Path
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer) {
        throw "Runtime host file must be a regular file."
    }
    return [IO.File]::Open(
        $item.FullName,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
}

$runtimeRoot = Join-Path (
    [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
) '.codex\change-delivery'
$hostConfigPath = Join-Path $runtimeRoot 'runtime-host.json'
$configStream = Open-StableFile $hostConfigPath
$pythonStream = $null
$moduleStream = $null
try {
    $reader = [IO.StreamReader]::new(
        $configStream,
        [Text.UTF8Encoding]::new($false, $true),
        $true,
        4096,
        $true
    )
    try {
        $hostConfig = ConvertFrom-Json $reader.ReadToEnd()
    }
    finally {
        $reader.Dispose()
    }
    if (
        $null -eq $hostConfig.python_path -or
        $null -eq $hostConfig.module_root
    ) {
        throw "Runtime host configuration is incomplete."
    }
    $pythonPath = [string] $hostConfig.python_path
    $moduleRoot = [string] $hostConfig.module_root
    if (
        -not [IO.Path]::IsPathRooted($pythonPath) -or
        -not [IO.Path]::IsPathRooted($moduleRoot)
    ) {
        throw "Runtime host configuration contains a relative path."
    }
    if ([IO.Path]::GetFileName($pythonPath) -notmatch '^(?i:python(?:3(?:\.\d+)?)?\.exe)$') {
        throw "Runtime host interpreter is not an approved Python executable."
    }
    Assert-NoReparseChain $moduleRoot
    $moduleItem = Get-Item -LiteralPath $moduleRoot -Force
    if (-not $moduleItem.PSIsContainer) {
        throw "Runtime module root is not a directory."
    }
    $moduleEntry = Join-Path $moduleItem.FullName 'change_delivery\__init__.py'
    $pythonStream = Open-StableFile $pythonPath
    $moduleStream = Open-StableFile $moduleEntry

    $env:PYTHONPATH = $null
    $env:PYTHONHOME = $null
    $env:PYTHONUTF8 = '1'
    $bootstrap = "import runpy,sys; root=sys.argv.pop(1); sys.path.insert(0,root); runpy.run_module('change_delivery',run_name='__main__')"
    & $pythonPath -I -c $bootstrap $moduleItem.FullName tick --plugin-root $runtimeRoot --json
    exit $LASTEXITCODE
}
finally {
    if ($null -ne $moduleStream) {
        $moduleStream.Dispose()
    }
    if ($null -ne $pythonStream) {
        $pythonStream.Dispose()
    }
    $configStream.Dispose()
}
