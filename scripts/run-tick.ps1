$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$fixedSourceRoot = (
    Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')
).ProviderPath
if ($fixedSourceRoot -cne 'C:\Users\ExampleUser\change-delivery-plugin') {
    throw 'Tick entrypoint is not running from the fixed source root.'
}
. (Join-Path $PSScriptRoot 'host-security.ps1')
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

$runtimeRoot = Join-Path (
    [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
) '.codex\change-delivery'
$hostConfigPath = Join-Path $runtimeRoot 'runtime-host.json'
$configStream = Open-StableFile $hostConfigPath
$pythonStream = $null
$moduleStream = $null
$codexStream = $null
$authStream = $null
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
}
