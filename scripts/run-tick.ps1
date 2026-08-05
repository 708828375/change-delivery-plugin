$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$TrustedHostSecuritySha256 = 'fbaeb713cb1a04125771d976f0c3134f36f1ee088cbefeaa1cd2ce4d46ffaf24'
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
$entryRoot = (
    Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')
).ProviderPath
$fixedSourceRoot = Assert-FixedSourceRoot $entryRoot
if ($fixedSourceRoot -cne $layout.host_root) {
    throw 'Tick 入口不属于当前用户的已安装 host。'
}
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
$runtimeRoot = $layout.data_root
$hostConfigPath = Join-Path $runtimeRoot 'runtime-host.json'
$configStream = Open-StableFile $hostConfigPath
$pythonStream = $null
$moduleStream = $null
$codexStream = $null
$ghStream = $null
$authStream = $null
$moduleStreams = @()
$moduleArchiveStream = $null
$moduleArchivePath = $null
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
    $trustedModuleTree = Join-Path $fixedModuleRoot 'change_delivery'
    Assert-NoPythonBytecode $trustedModuleTree
    Assert-ClosedPythonModuleRoot $fixedModuleRoot @($hostConfig.module_manifest)
    $moduleStreams = @(Open-ModuleTreeHandles $trustedModuleTree @($hostConfig.module_manifest))
    Assert-NoPythonBytecode $trustedModuleTree
    Assert-ClosedPythonModuleRoot $fixedModuleRoot @($hostConfig.module_manifest)


    $pythonPath = [string] $hostConfig.python_path
    $moduleRoot = [string] $hostConfig.module_root
    $codexPath = [string] $hostConfig.codex_path
    $ghPath = [string] $hostConfig.gh_path
    $authMode = [string] $hostConfig.auth_mode
    $moduleEntry = Join-Path $moduleRoot 'change_delivery\__init__.py'
    $pythonStream = Open-StableFile $pythonPath
    $moduleStream = Open-StableFile $moduleEntry
    $codexStream = Open-StableFile $codexPath
    $ghStream = Open-StableFile $ghPath

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
    $env:CHANGE_DELIVERY_GH_EXECUTABLE = $ghPath
    $env:CHANGE_DELIVERY_GH_SHA256 = [string] $hostConfig.gh_sha256
    $env:PYTHONPATH = $null
    $env:PYTHONHOME = $null
    $env:PYTHONUTF8 = '1'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $bootstrap = (
        "import runpy,sys;" +
        "sys.dont_write_bytecode=True;sys.implementation.cache_tag=None;" +
        "archive=sys.argv.pop(1);sys.path.insert(0,archive);" +
        "runpy.run_module('change_delivery',run_name='__main__')"
    )
    Assert-NoPythonBytecode $trustedModuleTree
    Assert-ClosedPythonModuleRoot $fixedModuleRoot @($hostConfig.module_manifest)
    $moduleArchivePath = Join-Path $runtimeRoot (
        '.trusted-module.' + [Guid]::NewGuid().ToString('N') + '.zip'
    )
    $moduleArchive = New-TrustedModuleArchive `
        $moduleArchivePath `
        $moduleStreams `
        @($hostConfig.module_manifest)
    $moduleArchiveStream = $moduleArchive.stream
    & $pythonPath -I -B -c $bootstrap $moduleArchivePath tick `
        --plugin-root $runtimeRoot --json
    exit $LASTEXITCODE
}
finally {
    if ($null -ne $moduleArchiveStream) {
        $moduleArchiveStream.Dispose()
    }
    if ($null -ne $moduleArchivePath -and (Test-Path -LiteralPath $moduleArchivePath)) {
        [IO.File]::Delete($moduleArchivePath)
    }
    foreach ($stream in $moduleStreams) { $stream.Dispose() }
    if ($null -ne $authStream) {
        $authStream.Dispose()
    }
    if ($null -ne $codexStream) {
        $codexStream.Dispose()
    }
    if ($null -ne $ghStream) {
        $ghStream.Dispose()
    }
    if ($null -ne $moduleStream) {
        $moduleStream.Dispose()
    }
    if ($null -ne $pythonStream) {
        $pythonStream.Dispose()
    }
    $configStream.Dispose()
    $helperStream.Dispose()
    $layoutStream.Dispose()
}
