$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:RuntimeHostFields = @(
    'schema_version',
    'python_path',
    'python_sha256',
    'module_root',
    'codex_path',
    'codex_sha256',
    'codex_package_root',
    'codex_parent_version',
    'codex_platform_dependency',
    'codex_platform_package_name',
    'codex_platform_version',
    'auth_mode',
    'auth_source'
)

function Assert-NoReparseChain([string] $Path) {
    if (-not [IO.Path]::IsPathRooted($Path)) {
        throw 'A security-sensitive path is not absolute.'
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    while ($null -ne $item) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'A security-sensitive path contains a reparse point.'
        }
        if ($item -is [IO.DirectoryInfo]) {
            $item = $item.Parent
        }
        else {
            $item = $item.Directory
        }
    }
}

function Resolve-RegularFile([string] $Path, [string] $ExpectedName) {
    if (-not [IO.Path]::IsPathRooted($Path)) {
        throw 'Executable or credential path must be absolute.'
    }
    Assert-NoReparseChain $Path
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (
        $item.PSIsContainer -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
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

function Read-StrictJsonObject([string] $Path, [int64] $MaximumBytes = 1MB) {
    $file = Resolve-RegularFile $Path ([IO.Path]::GetFileName($Path))
    $item = Get-Item -LiteralPath $file -Force
    if ($item.Length -le 0 -or $item.Length -gt $MaximumBytes) {
        throw 'JSON file has an invalid size.'
    }
    $text = [IO.File]::ReadAllText(
        $file,
        [Text.UTF8Encoding]::new($false, $true)
    )
    $value = ConvertFrom-Json $text
    if ($null -eq $value -or $value -isnot [PSCustomObject]) {
        throw 'JSON content must be one object.'
    }
    return $value
}

function Assert-ExactJsonProperties([object] $Value, [string[]] $Names) {
    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
    $expected = @($Names | Sort-Object)
    if (
        $actual.Count -ne $expected.Count -or
        (Compare-Object $actual $expected).Count -ne 0
    ) {
        throw 'JSON object fields do not match the strict schema.'
    }
}

function Get-FileSha256([string] $Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-PlatformContract() {
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw '32-bit Windows is not supported.'
    }
    if ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture -eq 'Arm64') {
        return [PSCustomObject]@{
            dependency = '@openai/codex-win32-arm64'
            cpu = 'arm64'
            triple = 'aarch64-pc-windows-msvc'
        }
    }
    if ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne 'X64') {
        throw 'Unsupported Windows architecture.'
    }
    return [PSCustomObject]@{
        dependency = '@openai/codex-win32-x64'
        cpu = 'x64'
        triple = 'x86_64-pc-windows-msvc'
    }
}

function Resolve-CodexIdentityAtRoot([string] $GlobalRoot) {
    $root = (Resolve-Path -LiteralPath $GlobalRoot -ErrorAction Stop).ProviderPath
    Assert-NoReparseChain $root
    $packageRoot = Join-Path $root '@openai\codex'
    Assert-NoReparseChain $packageRoot
    $parentManifest = Read-StrictJsonObject (
        Join-Path $packageRoot 'package.json'
    )
    if (
        $parentManifest.name -cne '@openai/codex' -or
        [string]::IsNullOrWhiteSpace([string] $parentManifest.version)
    ) {
        throw 'Invalid @openai/codex parent package identity.'
    }
    $contract = Get-PlatformContract
    $dependencyProperty = $parentManifest.optionalDependencies.PSObject.Properties[
        $contract.dependency
    ]
    if ($null -eq $dependencyProperty) {
        throw 'Parent package does not lock the current Windows platform package.'
    }
    $dependencyValue = [string] $dependencyProperty.Value
    if ($dependencyValue -notmatch '^npm:(?<name>@openai/codex)@(?<version>[0-9A-Za-z.+-]+)$') {
        throw 'Platform package alias is invalid.'
    }
    $expectedName = [string] $Matches.name
    $expectedVersion = [string] $Matches.version
    if (-not $expectedVersion.StartsWith(
        ([string] $parentManifest.version) + '-win32-',
        [StringComparison]::Ordinal
    )) {
        throw 'Platform package version is not locked to the parent version.'
    }
    $directoryName = $contract.dependency.Split('/')[-1]
    $platformRoot = Join-Path (
        Join-Path $packageRoot 'node_modules\@openai'
    ) $directoryName
    Assert-NoReparseChain $platformRoot
    $platformManifest = Read-StrictJsonObject (
        Join-Path $platformRoot 'package.json'
    )
    if (
        $platformManifest.name -cne $expectedName -or
        $platformManifest.version -cne $expectedVersion -or
        @($platformManifest.os).Count -ne 1 -or
        @($platformManifest.os)[0] -cne 'win32' -or
        @($platformManifest.cpu).Count -ne 1 -or
        @($platformManifest.cpu)[0] -cne $contract.cpu
    ) {
        throw 'Platform package name, version, OS, or CPU does not match its lock.'
    }
    $vendorRoot = Join-Path $platformRoot 'vendor'
    Assert-NoReparseChain $vendorRoot
    $expectedBinary = Join-Path (
        Join-Path (Join-Path $vendorRoot $contract.triple) 'bin'
    ) 'codex.exe'
    $binary = Resolve-RegularFile $expectedBinary 'codex.exe'
    $allCandidates = @(
        Get-ChildItem -LiteralPath $vendorRoot -Recurse -File `
            -Filter 'codex.exe' -ErrorAction Stop
    )
    if (
        $allCandidates.Count -ne 1 -or
        -not [string]::Equals(
            $allCandidates[0].FullName,
            $binary,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw 'Vendor package must contain exactly one native codex.exe at the fixed path.'
    }
    $versionOutput = & $binary --version
    if (
        $LASTEXITCODE -ne 0 -or
        ($versionOutput -join '') -cne (
            'codex-cli ' + [string] $parentManifest.version
        )
    ) {
        throw 'Native Codex version contract does not match its signed package graph.'
    }
    return [PSCustomObject]@{
        path = $binary
        sha256 = Get-FileSha256 $binary
        package_root = (Resolve-Path $packageRoot).ProviderPath
        parent_version = [string] $parentManifest.version
        platform_dependency = [string] $contract.dependency
        platform_package_name = $expectedName
        platform_version = $expectedVersion
    }
}

function Resolve-OfficialCodexIdentity() {
    $npm = Get-Command npm.cmd -CommandType Application -ErrorAction Stop
    $npmPath = Resolve-RegularFile $npm.Source 'npm.cmd'
    $rootOutput = & $npmPath root -g
    if ($LASTEXITCODE -ne 0 -or @($rootOutput).Count -ne 1) {
        throw 'Unable to discover one global npm package root.'
    }
    return Resolve-CodexIdentityAtRoot ([string] $rootOutput)
}

function Resolve-TrustedPythonIdentity() {
    $trusted = @(
        Get-Command python.exe -CommandType Application -All -ErrorAction Stop |
        ForEach-Object {
            try {
                $candidate = Resolve-RegularFile $_.Source ''
                if (
                    [IO.Path]::GetFileName($candidate) -match
                    '^(?i:python(?:3(?:\.\d+)?)?\.exe)$'
                ) {
                    $candidate
                }
            }
            catch {
                # App execution aliases and reparse candidates are not trusted.
            }
        } |
        Sort-Object -Unique
    )
    if ($trusted.Count -ne 1) {
        throw 'Exactly one trusted Python application must resolve for the installer.'
    }
    $path = [string] $trusted[0]
    return [PSCustomObject]@{
        path = $path
        sha256 = Get-FileSha256 $path
    }
}

function Resolve-Authentication([string] $Mode, [string] $Source) {
    if ($Mode -eq 'auth_file') {
        if (-not $Source) {
            throw 'auth_file mode requires AuthSource.'
        }
        $auth = Resolve-RegularFile $Source 'auth.json'
        [void](Read-StrictJsonObject $auth)
        return $auth
    }
    if ($Mode -ne 'api_key') {
        throw 'Authentication mode is invalid.'
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

function Assert-FixedSourceRoot([string] $Path) {
    $fixed = (
        Resolve-Path -LiteralPath 'C:\Users\ExampleUser\change-delivery-plugin'
    ).ProviderPath
    $actual = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).ProviderPath
    if (-not [string]::Equals(
        $actual.TrimEnd('\'),
        $fixed.TrimEnd('\'),
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'SourceRoot must be the fixed Change Delivery repository.'
    }
    Assert-NoReparseChain $actual
    return $actual
}

function Convert-CimValue([object] $Value) {
    if ($null -eq $Value) {
        return $null
    }
    if ($Value -is [Microsoft.Management.Infrastructure.CimInstance]) {
        $result = [ordered]@{}
        foreach ($property in @($Value.CimInstanceProperties | Sort-Object Name)) {
            $result[$property.Name] = Convert-CimValue $property.Value
        }
        return $result
    }
    if ($Value -is [Array]) {
        return @($Value | ForEach-Object { Convert-CimValue $_ })
    }
    if ($Value -is [datetime]) {
        return $Value.ToUniversalTime().ToString('o')
    }
    return [string] $Value
}

function Get-CimFingerprint([object] $Value) {
    return ConvertTo-Json (Convert-CimValue $Value) -Depth 30 -Compress
}

function Get-TaskDefinitionFingerprint([object] $Task) {
    $definition = [ordered]@{}
    foreach ($property in @($Task.CimInstanceProperties | Sort-Object Name)) {
        if ($property.Name -in @('TaskName', 'TaskPath', 'State')) {
            continue
        }
        if ($property.Name -eq 'Triggers') {
            $definition[$property.Name] = @(
                $property.Value |
                Sort-Object { $_.CimClass.CimClassName } |
                ForEach-Object { Convert-CimValue $_ }
            )
        }
        else {
            $definition[$property.Name] = Convert-CimValue $property.Value
        }
    }
    return ConvertTo-Json $definition -Depth 30 -Compress
}

function Test-StrictTaskDefinition(
    [object[]] $ExistingResults,
    [object] $Expected,
    [string] $ExpectedName,
    [string] $ExpectedUser
) {
    if ($ExistingResults.Count -ne 1) {
        return $false
    }
    $existing = $ExistingResults[0]
    if (
        $null -ne $existing.PSObject.Properties['TaskName'] -and
        $null -ne $existing.TaskName -and
        (
            $existing.TaskName -cne $ExpectedName -or
            $existing.TaskPath -cne '\'
        )
    ) {
        return $false
    }
    if (
        $null -ne $existing.PSObject.Properties['State'] -and
        [string] $existing.State -eq 'Disabled'
    ) {
        return $false
    }
    if (
        $existing.Settings.Enabled -ne $true -or
        $existing.Principal.UserId -cne $ExpectedUser
    ) {
        return $false
    }
    $triggers = @($existing.Triggers)
    if ($triggers.Count -ne 2 -or @($triggers | Where-Object { $_.Enabled -ne $true }).Count) {
        return $false
    }
    foreach ($trigger in $triggers) {
        if (
            -not [string]::IsNullOrWhiteSpace([string] $trigger.EndBoundary) -or
            (
                -not [string]::IsNullOrWhiteSpace([string] $trigger.StartBoundary) -and
                [datetime]::Parse([string] $trigger.StartBoundary) -gt (Get-Date).AddMinutes(5)
            )
        ) {
            return $false
        }
    }
    return (
        (Get-TaskDefinitionFingerprint $existing) -ceq
        (Get-TaskDefinitionFingerprint $Expected)
    )
}

function Assert-StrictRuntimeHost([object] $Config, [string] $SourceRoot) {
    Assert-ExactJsonProperties $Config $script:RuntimeHostFields
    if ($Config.schema_version -isnot [int] -or $Config.schema_version -ne 1) {
        throw 'Runtime host schema version is invalid.'
    }
    foreach ($name in $script:RuntimeHostFields) {
        if ($name -in @('schema_version', 'auth_source')) {
            continue
        }
        if (
            $Config.$name -isnot [string] -or
            [string]::IsNullOrWhiteSpace([string] $Config.$name)
        ) {
            throw "Runtime host field $name must be a non-empty string."
        }
    }
    if (
        $Config.python_sha256 -notmatch '^[0-9a-f]{64}$' -or
        $Config.codex_sha256 -notmatch '^[0-9a-f]{64}$'
    ) {
        throw 'Runtime executable identity hash is invalid.'
    }
    $fixedModule = (
        Resolve-Path -LiteralPath (Join-Path $SourceRoot 'src')
    ).ProviderPath
    $actualModule = (
        Resolve-Path -LiteralPath ([string] $Config.module_root)
    ).ProviderPath
    if (-not [string]::Equals(
        $fixedModule,
        $actualModule,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'Runtime module root differs from $PSScriptRoot\..\src.'
    }
    $python = Resolve-TrustedPythonIdentity
    if (
        $Config.python_path -cne $python.path -or
        $Config.python_sha256 -cne $python.sha256
    ) {
        throw 'Runtime Python identity differs from the trusted installer source.'
    }
    $codex = Resolve-OfficialCodexIdentity
    $pairs = @{
        codex_path = 'path'
        codex_sha256 = 'sha256'
        codex_package_root = 'package_root'
        codex_parent_version = 'parent_version'
        codex_platform_dependency = 'platform_dependency'
        codex_platform_package_name = 'platform_package_name'
        codex_platform_version = 'platform_version'
    }
    foreach ($pair in $pairs.GetEnumerator()) {
        if ($Config.($pair.Key) -cne $codex.($pair.Value)) {
            throw "Runtime Codex package identity field $($pair.Key) differs."
        }
    }
    if ($Config.auth_mode -eq 'auth_file') {
        if ($Config.auth_source -isnot [string]) {
            throw 'auth_file runtime host requires one auth source.'
        }
    }
    elseif ($Config.auth_mode -eq 'api_key') {
        if ($null -ne $Config.auth_source) {
            throw 'api_key runtime host forbids auth source.'
        }
    }
    else {
        throw 'Runtime host authentication mode is invalid.'
    }
    return [PSCustomObject]@{
        python = $python
        codex = $codex
        module_root = $actualModule
    }
}

function ConvertTo-CanonicalRuntimeHostJson([object] $Config) {
    Assert-ExactJsonProperties $Config $script:RuntimeHostFields
    $ordered = [ordered]@{}
    foreach ($field in $script:RuntimeHostFields) {
        $ordered[$field] = $Config.$field
    }
    return (ConvertTo-Json $ordered -Compress) + [Environment]::NewLine
}
