$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:HostSecurityRoot = $PSScriptRoot
$script:TrustedModuleManifestSha256 = 'bf50df356316df0426e0af64947dc187e7af7019e1869e34585a61b74d698014'
$script:StableManifestReadProbe = $null

if ($null -eq ('ChangeDelivery.NativeFileIdentity' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace ChangeDelivery {
    public static class NativeFileIdentity {
        [StructLayout(LayoutKind.Sequential)]
        private struct BY_HANDLE_FILE_INFORMATION {
            public uint FileAttributes;
            public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
            public uint VolumeSerialNumber;
            public uint FileSizeHigh;
            public uint FileSizeLow;
            public uint NumberOfLinks;
            public uint FileIndexHigh;
            public uint FileIndexLow;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFileW(
            string name, uint access, uint share, IntPtr security,
            uint creation, uint flags, IntPtr template);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandle(
            SafeFileHandle handle, out BY_HANDLE_FILE_INFORMATION information);

        public static uint GetLinkCount(string path) {
            using (SafeFileHandle handle = CreateFileW(
                path, 0x80000000, 1, IntPtr.Zero, 3, 0x80, IntPtr.Zero)) {
                if (handle.IsInvalid) {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                BY_HANDLE_FILE_INFORMATION information;
                if (!GetFileInformationByHandle(handle, out information)) {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                return information.NumberOfLinks;
            }
        }
    }
}
'@
}

$script:RuntimeHostFields = @(
    'schema_version',
    'python_path',
    'python_sha256',
    'module_root',
    'codex_path',
    'codex_sha256',
    'gh_path',
    'gh_sha256',
    'codex_package_root',
    'codex_parent_version',
    'codex_platform_dependency',
    'codex_platform_package_name',
    'codex_platform_version',
    'module_manifest',
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
    if ([ChangeDelivery.NativeFileIdentity]::GetLinkCount($item.FullName) -ne 1) {
        throw 'Expected a single-link ordinary file.'
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
        @(Compare-Object $actual $expected).Count -ne 0
    ) {
        throw 'JSON object fields do not match the strict schema.'
    }
}

function Get-FileSha256([string] $Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Read-StableUtf8File(
    [string] $Path,
    [string] $ExpectedSha256,
    [string] $ExpectedName
) {
    if ($ExpectedSha256 -notmatch '^[0-9a-f]{64}$') {
        throw 'Trusted UTF-8 file hash is invalid.'
    }
    $file = Resolve-RegularFile $Path $ExpectedName
    $stream = [IO.File]::Open(
        $file,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    try {
        $algorithm = [Security.Cryptography.SHA256]::Create()
        try {
            $actualHash = [BitConverter]::ToString(
                $algorithm.ComputeHash($stream)
            ).Replace('-', '').ToLowerInvariant()
        }
        finally {
            $algorithm.Dispose()
        }
        if ($actualHash -cne $ExpectedSha256) {
            throw 'Trusted UTF-8 file hash differs from its trust anchor.'
        }
        if ($null -ne $script:StableManifestReadProbe) {
            & $script:StableManifestReadProbe $file
        }
        $stream.Position = 0
        $reader = [IO.StreamReader]::new(
            $stream,
            [Text.UTF8Encoding]::new($false, $true),
            $true,
            4096,
            $true
        )
        try {
            $text = $reader.ReadToEnd()
        }
        finally {
            $reader.Dispose()
        }
        if ($stream.Position -ne $stream.Length) {
            throw 'Trusted UTF-8 file was not read completely.'
        }
        [void](Resolve-RegularFile $file $ExpectedName)
        return $text
    }
    finally {
        $stream.Dispose()
    }
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
    $commands = @(Get-Command codex.cmd -CommandType Application -All `
        -ErrorAction Stop)
    if ($commands.Count -ne 1) {
        throw 'PATH 中必须恰好存在一个 Codex CLI cmd 入口。'
    }
    $shim = Resolve-RegularFile ([string] $commands[0].Source) 'codex.cmd'
    $globalRoot = Join-Path (Split-Path -Parent $shim) 'node_modules'
    return Resolve-CodexIdentityAtRoot $globalRoot
}

function Get-HostSecurityLayout() {
    $layoutCommand = Get-Command Get-SharedInstallLayout `
        -CommandType Function -ErrorAction SilentlyContinue
    if ($null -ne $layoutCommand) {
        return Get-SharedInstallLayout
    }
    $knownProfile = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::UserProfile
    )
    if (
        [string]::IsNullOrWhiteSpace($knownProfile) -or
        [string]::IsNullOrWhiteSpace($env:USERPROFILE)
    ) {
        throw 'Windows 用户目录不可用。'
    }
    $known = [IO.Path]::GetFullPath($knownProfile).TrimEnd('\')
    $environment = [IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\')
    if (-not [string]::Equals(
        $known,
        $environment,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'USERPROFILE 与 Windows Known Folder 不一致。'
    }
    $dataRoot = Join-Path $known '.codex\change-delivery'
    return [PSCustomObject]@{
        data_root = $dataRoot
        host_root = Join-Path $dataRoot 'host'
        venv_root = Join-Path $dataRoot 'runtime\venv'
    }
}

function Resolve-TrustedPythonIdentity() {
    $layout = Get-HostSecurityLayout
    $path = Resolve-RegularFile (
        Join-Path $layout.venv_root 'Scripts\python.exe'
    ) 'python.exe'
    $contractText = & $path -I -c (
        'import json,sys;print(json.dumps(dict(executable=sys.executable,' +
        'major=sys.version_info.major,minor=sys.version_info.minor),' +
        'sort_keys=True))'
    )
    if ($LASTEXITCODE -ne 0 -or @($contractText).Count -ne 1) {
        throw 'Fixed Python failed its direct identity contract.'
    }
    $contract = ConvertFrom-Json ([string] $contractText)
    if (
        $contract.executable -cne $path -or
        $contract.major -ne 3 -or
        $contract.minor -lt 12
    ) {
        throw '专用 Python 的身份或版本与信任锚不一致。'
    }
    return [PSCustomObject]@{
        path = $path
        sha256 = Get-FileSha256 $path
    }
}

function Resolve-InstalledGitHubCliIdentity() {
    $commands = @(Get-Command gh.exe -CommandType Application -All `
        -ErrorAction Stop)
    if ($commands.Count -ne 1) {
        throw 'PATH 中必须恰好存在一个 GitHub CLI 原生入口。'
    }
    $path = Resolve-RegularFile ([string] $commands[0].Source) 'gh.exe'
    $versionOutput = @(& $path --version)
    if (
        $LASTEXITCODE -ne 0 -or
        $versionOutput.Count -lt 1 -or
        [string] $versionOutput[0] -notmatch '^gh version [0-9]+\.[0-9]+\.[0-9]+'
    ) {
        throw 'GitHub CLI failed its direct native version contract.'
    }
    return [PSCustomObject]@{
        path = $path
        sha256 = Get-FileSha256 $path
    }
}

function Assert-NoPythonBytecode([string] $ModuleRoot) {
    $root = (Resolve-Path -LiteralPath $ModuleRoot -ErrorAction Stop).ProviderPath
    Assert-NoReparseChain $root
    $forbidden = @(
        Get-ChildItem -LiteralPath $root -Recurse -Force -ErrorAction Stop |
        Where-Object {
            ($_.PSIsContainer -and $_.Name -ceq '__pycache__') -or
            (-not $_.PSIsContainer -and $_.Extension -ieq '.pyc')
        }
    )
    if ($forbidden.Count -ne 0) {
        throw 'Trusted Python module tree contains forbidden bytecode or cache directories.'
    }
}

function Get-ModuleTreeManifest([string] $ModuleRoot) {
    $root = (Resolve-Path -LiteralPath $ModuleRoot -ErrorAction Stop).ProviderPath
    Assert-NoReparseChain $root
    $files = @(
        Get-ChildItem -LiteralPath $root -Recurse -File -Filter '*.py' |
        Sort-Object FullName
    )
    if ($files.Count -eq 0) {
        throw 'Trusted Python module tree is empty.'
    }
    return @(
        $files | ForEach-Object {
            $path = Resolve-RegularFile $_.FullName $_.Name
            $relative = $path.Substring($root.Length).TrimStart('\').Replace('\', '/')
            [PSCustomObject][ordered]@{
                relative_path = $relative
                sha256 = Get-FileSha256 $path
                size = [int64] $_.Length
            }
        }
    )
}

function ConvertTo-CanonicalModuleManifest([object[]] $Manifest) {
    $normalized = @(
        $Manifest | ForEach-Object {
            Assert-ExactJsonProperties $_ @('relative_path', 'sha256', 'size')
            if (
                $_.relative_path -isnot [string] -or
                $_.relative_path -notmatch '^[A-Za-z0-9_./-]+\.py$' -or
                $_.relative_path.Contains('..') -or
                $_.sha256 -isnot [string] -or
                $_.sha256 -notmatch '^[0-9a-f]{64}$' -or
                -not ($_.size -is [int] -or $_.size -is [long]) -or
                [int64] $_.size -lt 0
            ) {
                throw 'Module manifest entry is invalid.'
            }
            [ordered]@{
                relative_path = [string] $_.relative_path
                sha256 = [string] $_.sha256
                size = [int64] $_.size
            }
        }
    )
    return ConvertTo-Json $normalized -Compress
}

function Assert-ClosedPythonModuleRoot(
    [string] $ModuleRoot,
    [object[]] $Expected
) {
    $root = (Resolve-Path -LiteralPath $ModuleRoot -ErrorAction Stop).ProviderPath
    Assert-NoReparseChain $root
    [void](ConvertTo-CanonicalModuleManifest $Expected)

    $rootEntries = @(Get-ChildItem -LiteralPath $root -Force -ErrorAction Stop)
    if (
        $rootEntries.Count -ne 1 -or
        $rootEntries[0] -isnot [IO.DirectoryInfo] -or
        $rootEntries[0].Name -cne 'change_delivery' -or
        ($rootEntries[0].Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw 'Trusted src root must contain only the ordinary change_delivery directory.'
    }

    $packageRoot = (Resolve-Path -LiteralPath (
        Join-Path $root 'change_delivery'
    ) -ErrorAction Stop).ProviderPath
    Assert-NoReparseChain $packageRoot
    Assert-NoPythonBytecode $packageRoot

    $expectedFiles = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::Ordinal
    )
    $expectedDirectories = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::Ordinal
    )
    foreach ($entry in $Expected) {
        $relative = [string] $entry.relative_path
        if (-not $expectedFiles.Add($relative)) {
            throw 'Trusted module manifest contains duplicate paths.'
        }
        $segments = @($relative.Split('/'))
        for ($index = 1; $index -lt $segments.Count; $index++) {
            [void] $expectedDirectories.Add(
                (($segments[0..($index - 1)]) -join '/')
            )
        }
    }

    $actualEntries = @(
        Get-ChildItem -LiteralPath $packageRoot -Recurse -Force -ErrorAction Stop
    )
    if (
        $actualEntries.Count -ne
        ($expectedFiles.Count + $expectedDirectories.Count)
    ) {
        throw 'Trusted Python package tree contains unmanifested entries.'
    }
    foreach ($item in $actualEntries) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Trusted Python package tree contains a reparse point.'
        }
        $relative = $item.FullName.Substring($packageRoot.Length).TrimStart('\')
        $relative = $relative.Replace('\', '/')
        if ($item -is [IO.DirectoryInfo]) {
            if (-not $expectedDirectories.Contains($relative)) {
                throw 'Trusted Python package tree contains an unexpected directory.'
            }
            Assert-NoReparseChain $item.FullName
            continue
        }
        if (
            $item -isnot [IO.FileInfo] -or
            -not $expectedFiles.Contains($relative)
        ) {
            throw 'Trusted Python package tree contains an unexpected file.'
        }
        [void](Resolve-RegularFile $item.FullName $item.Name)
    }
    Assert-ModuleTreeManifest $packageRoot $Expected
}

function New-TrustedModuleArchive(
    [string] $ArchivePath,
    [object[]] $ModuleStreams,
    [object[]] $Expected
) {
    Add-Type -AssemblyName System.IO.Compression -ErrorAction Stop
    if (-not [IO.Path]::IsPathRooted($ArchivePath)) {
        throw 'Trusted module archive path must be absolute.'
    }
    [void](ConvertTo-CanonicalModuleManifest $Expected)
    if ($ModuleStreams.Count -ne $Expected.Count -or $Expected.Count -eq 0) {
        throw 'Trusted module archive inputs do not match the manifest.'
    }
    $parent = Split-Path -Parent $ArchivePath
    Assert-NoReparseChain $parent
    if (Test-Path -LiteralPath $ArchivePath) {
        throw 'Trusted module archive path already exists.'
    }
    $stream = [IO.File]::Open(
        $ArchivePath,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::Read
    )
    try {
        $archive = [IO.Compression.ZipArchive]::new(
            $stream,
            [IO.Compression.ZipArchiveMode]::Create,
            $true,
            [Text.Encoding]::UTF8
        )
        try {
            for ($index = 0; $index -lt $Expected.Count; $index++) {
                $source = $ModuleStreams[$index]
                if ($source -isnot [IO.FileStream] -or -not $source.CanRead) {
                    throw 'Trusted module archive source is not a readable stable handle.'
                }
                $source.Position = 0
                $entryName = 'change_delivery/' + [string] $Expected[$index].relative_path
                $entry = $archive.CreateEntry(
                    $entryName,
                    [IO.Compression.CompressionLevel]::NoCompression
                )
                $target = $entry.Open()
                try {
                    $source.CopyTo($target)
                }
                finally {
                    $target.Dispose()
                }
                $source.Position = 0
            }
        }
        finally {
            $archive.Dispose()
        }
        $stream.Flush($true)
        $stream.Position = 0
        return [PSCustomObject]@{
            path = $ArchivePath
            stream = $stream
        }
    }
    catch {
        $stream.Dispose()
        if (Test-Path -LiteralPath $ArchivePath) {
            [IO.File]::Delete($ArchivePath)
        }
        throw
    }
}

function Assert-ModuleTreeManifest([string] $ModuleRoot, [object[]] $Expected) {
    $actual = Get-ModuleTreeManifest $ModuleRoot
    if (
        (ConvertTo-CanonicalModuleManifest $actual) -cne
        (ConvertTo-CanonicalModuleManifest $Expected)
    ) {
        throw 'Python module tree differs from the trusted manifest.'
    }
}

function Read-TrustedModuleManifest() {
    if ($script:TrustedModuleManifestSha256 -notmatch '^[0-9a-f]{64}$') {
        throw 'Trusted module manifest has not been finalized.'
    }
    $path = Join-Path $script:HostSecurityRoot 'trusted-module-manifest.json'
    $value = Read-StableUtf8File `
        $path `
        $script:TrustedModuleManifestSha256 `
        'trusted-module-manifest.json' |
        ConvertFrom-Json
    $manifest = @($value)
    [void](ConvertTo-CanonicalModuleManifest $manifest)
    return $manifest
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
    $layout = Get-HostSecurityLayout
    $fixed = (Resolve-Path -LiteralPath $layout.host_root).ProviderPath
    $actual = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).ProviderPath
    if (-not [string]::Equals(
        $actual.TrimEnd('\'),
        $fixed.TrimEnd('\'),
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'SourceRoot 必须是已安装的 Change Delivery host。'
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
        if ($name -in @('schema_version', 'auth_source', 'module_manifest')) {
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
        $Config.codex_sha256 -notmatch '^[0-9a-f]{64}$' -or
        $Config.gh_sha256 -notmatch '^[0-9a-f]{64}$'
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
    $trustedManifest = @(Read-TrustedModuleManifest)
    if ((ConvertTo-CanonicalModuleManifest @($Config.module_manifest)) -cne
        (ConvertTo-CanonicalModuleManifest $trustedManifest)) {
        throw 'Runtime host module manifest differs from the independent trust anchor.'
    }
    Assert-ModuleTreeManifest (Join-Path $actualModule 'change_delivery') $trustedManifest
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
    $gh = Resolve-InstalledGitHubCliIdentity
    $ghPath = Resolve-RegularFile ([string] $Config.gh_path) 'gh.exe'
    if (
        $Config.gh_path -cne $ghPath -or
        $Config.gh_path -cne $gh.path -or
        $Config.gh_sha256 -cne $gh.sha256 -or
        $Config.gh_sha256 -cne (Get-FileSha256 $ghPath)
    ) {
        throw 'Runtime GitHub CLI identity differs from its installed trust anchor.'
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
        gh = [PSCustomObject]@{
            path = $ghPath
            sha256 = [string] $Config.gh_sha256
        }
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

function Install-AtomicRuntimeHostFile {
    param(
        [string] $Temporary,
        [string] $Destination,
        [switch] $Replace
    )
    if (-not $Replace) {
        [IO.File]::Move($Temporary, $Destination)
        return
    }
    $backup = $Destination + '.backup-' + [Guid]::NewGuid().ToString('N')
    try {
        [IO.File]::Replace($Temporary, $Destination, $backup, $true)
    }
    catch {
        if (
            (Test-Path -LiteralPath $backup) -and
            -not (Test-Path -LiteralPath $Destination)
        ) {
            [IO.File]::Move($backup, $Destination)
        }
        throw
    }
    if (Test-Path -LiteralPath $backup) {
        [IO.File]::Delete($backup)
    }
}

function Write-RuntimeHost {
    param(
        [string] $RuntimeRoot,
        [object] $Configuration,
        [string] $SourceRoot,
        [switch] $AllowManagedReplacement
    )
    # Validate an in-memory JSON round trip before creating any directory or file.
    $candidate = ConvertFrom-Json (
        ConvertTo-Json $Configuration -Depth 100 -Compress
    )
    Assert-StrictRuntimeHost $candidate $SourceRoot | Out-Null
    $json = ConvertTo-CanonicalRuntimeHostJson $candidate

    $parent = Split-Path -Parent $RuntimeRoot
    Assert-NoReparseChain $parent
    if (-not (Test-Path -LiteralPath $RuntimeRoot)) {
        New-Item -ItemType Directory -Path $RuntimeRoot -ErrorAction Stop | Out-Null
    }
    Assert-NoReparseChain $RuntimeRoot
    $runtimeItem = Get-Item -LiteralPath $RuntimeRoot -Force
    if ($runtimeItem -isnot [IO.DirectoryInfo]) {
        throw 'Runtime root must be an ordinary directory.'
    }

    $destination = Join-Path $RuntimeRoot 'runtime-host.json'
    $replaceExisting = $false
    if (Test-Path -LiteralPath $destination) {
        $existing = Read-StrictJsonObject $destination 16KB
        $current = [IO.File]::ReadAllText(
            (Resolve-RegularFile $destination 'runtime-host.json'),
            [Text.UTF8Encoding]::new($false, $true)
        )
        if ($current -ceq $json) {
            Assert-StrictRuntimeHost $existing $SourceRoot | Out-Null
            return
        }
        if (-not $AllowManagedReplacement) {
            throw 'Existing runtime-host.json differs; refusing to overwrite it.'
        }
        Assert-ExactJsonProperties $existing $script:RuntimeHostFields
        if (
            $existing.schema_version -ne 1 -or
            [string] $existing.module_root -cne [string] $candidate.module_root -or
            [string] $existing.auth_mode -cne [string] $candidate.auth_mode -or
            [string] $existing.auth_source -cne [string] $candidate.auth_source -or
            (ConvertTo-CanonicalRuntimeHostJson $existing) -cne $current
        ) {
            throw 'Existing runtime-host.json is not a replaceable managed configuration.'
        }
        $replaceExisting = $true
    }

    $temporary = Join-Path $RuntimeRoot (
        '.runtime-host.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    )
    try {
        $stream = [IO.File]::Open(
            $temporary,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        try {
            $bytes = [Text.UTF8Encoding]::new($false, $true).GetBytes($json)
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        }
        finally {
            $stream.Dispose()
        }
        $roundTrip = Read-StrictJsonObject $temporary 16KB
        Assert-StrictRuntimeHost $roundTrip $SourceRoot | Out-Null
        if ((ConvertTo-CanonicalRuntimeHostJson $roundTrip) -cne $json) {
            throw 'Runtime host JSON round trip is not canonical.'
        }
        Install-AtomicRuntimeHostFile `
            -Temporary $temporary `
            -Destination $destination `
            -Replace:$replaceExisting
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            [IO.File]::Delete($temporary)
        }
    }
}
