$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$fixedSourceRoot = (
    Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')
).ProviderPath
if ($fixedSourceRoot -cne 'C:\Users\ExampleUser\change-delivery-plugin') {
    throw 'Tick entrypoint is not running from the fixed source root.'
}
$TrustedHostSecuritySha256 = '3f64c416dd03bd5a8059886604c4652c6fb56cceda8a52a2c97979519ddf348a'
function Open-StableTrustedHelper([string] $Path) {
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
        if ($hash -cne $TrustedHostSecuritySha256) { throw 'Trusted helper hash differs from the embedded anchor.' }
        $stream.Position = 0
        return $stream
    }
    catch { $stream.Dispose(); throw }
}
$helperPath = Join-Path $PSScriptRoot 'host-security.ps1'
$helperStream = Open-StableTrustedHelper $helperPath
. $helperPath
$fixedModuleRoot = (
    Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\src')
).ProviderPath

function Open-StableFile([string] $Path) {
    $file = Resolve-RegularFile $Path ([IO.Path]::GetFileName($Path))
    return [IO.File]::Open(
        $file,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
}

function Open-ModuleTreeHandles([string] $ModuleRoot, [object[]] $Expected) {
    Assert-ModuleTreeManifest $ModuleRoot $Expected
    $root = (Resolve-Path -LiteralPath $ModuleRoot).ProviderPath
    $handles = [Collections.Generic.List[IO.FileStream]]::new()
    try {
        foreach ($entry in $Expected) {
            $candidate = Join-Path $root ([string] $entry.relative_path)
            $path = Resolve-RegularFile $candidate ([IO.Path]::GetFileName($candidate))
            if (-not $path.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Module manifest path escaped the fixed module root.' }
            $handles.Add([IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read))
        }
        Assert-ModuleTreeManifest $ModuleRoot $Expected
        return $handles.ToArray()
    }
    catch { foreach ($handle in $handles) { $handle.Dispose() }; throw }
}
$runtimeRoot = Join-Path (
    [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
) '.codex\change-delivery'
$hostConfigPath = Join-Path $runtimeRoot 'runtime-host.json'
$configStream = Open-StableFile $hostConfigPath
$pythonStream = $null
$moduleStream = $null
$codexStream = $null
$authStream = $null
$moduleStreams = @()
try {
    if ($configStream.Length -le 0 -or $configStream.Length -gt 16KB) {
        throw 'Runtime host configuration has an invalid size.'
    }
    $reader = [IO.StreamReader]::new(
        $configStream,
        [Text.UTF8Encoding]::new($false, $true),
        $true,
        4096,
        $true
    )
    try {
        $rawHostConfig = $reader.ReadToEnd()
        $hostConfig = ConvertFrom-Json $rawHostConfig
    }
    finally {
        $reader.Dispose()
    }
    if ($hostConfig -isnot [PSCustomObject]) {
        throw 'Runtime host configuration must be one strict JSON object.'
    }
    $identity = Assert-StrictRuntimeHost $hostConfig $fixedSourceRoot
    if ($rawHostConfig -cne (ConvertTo-CanonicalRuntimeHostJson $hostConfig)) {
        throw 'Runtime host JSON is not the exact canonical strict schema.'
    }
    if ($identity.module_root -cne $fixedModuleRoot) {
        throw 'Runtime module root does not match $PSScriptRoot\..\src.'
    }
    $moduleStreams = @(Open-ModuleTreeHandles (Join-Path $fixedModuleRoot 'change_delivery') @($hostConfig.module_manifest))


    $pythonPath = [string] $hostConfig.python_path
    $moduleRoot = [string] $hostConfig.module_root
    $codexPath = [string] $hostConfig.codex_path
    $authMode = [string] $hostConfig.auth_mode
    $moduleEntry = Join-Path $moduleRoot 'change_delivery\__init__.py'
    $pythonStream = Open-StableFile $pythonPath
    $moduleStream = Open-StableFile $moduleEntry
    $codexStream = Open-StableFile $codexPath

    # Revalidate path, package graph, version and hashes while replacement is
    # prevented by the stable read handles.
    [void](Assert-StrictRuntimeHost $hostConfig $fixedSourceRoot)

    if ($authMode -eq 'auth_file') {
        $authSource = [string] $hostConfig.auth_source
        $authStream = Open-StableFile $authSource
        [void](Read-StrictJsonObject $authSource)
        $env:OPENAI_API_KEY = $null
        $env:CHANGE_DELIVERY_AUTH_SOURCE = $authSource
    }
    elseif ($authMode -eq 'api_key') {
        if ([string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY)) {
            throw 'Runtime api_key mode requires process OPENAI_API_KEY.'
        }
        $env:CHANGE_DELIVERY_AUTH_SOURCE = $null
    }
    else {
        throw 'Runtime auth mode is invalid.'
    }
    $env:CHANGE_DELIVERY_AUTH_MODE = $authMode
    $env:CHANGE_DELIVERY_CODEX_EXECUTABLE = $codexPath
    $env:PYTHONPATH = $null
    $env:PYTHONHOME = $null
    $env:PYTHONUTF8 = '1'
    $bootstrap = "import runpy,sys; root=sys.argv.pop(1); sys.path.insert(0,root); runpy.run_module('change_delivery',run_name='__main__')"
    & $pythonPath -I -c $bootstrap $moduleRoot tick `
        --plugin-root $runtimeRoot --json
    exit $LASTEXITCODE
}
finally {
    foreach ($stream in $moduleStreams) { $stream.Dispose() }
    if ($null -ne $authStream) {
        $authStream.Dispose()
    }
    if ($null -ne $codexStream) {
        $codexStream.Dispose()
    }
    if ($null -ne $moduleStream) {
        $moduleStream.Dispose()
    }
    if ($null -ne $pythonStream) {
        $pythonStream.Dispose()
    }
    $configStream.Dispose()
    $helperStream.Dispose()
}
