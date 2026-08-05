$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-OrdinaryDirectory([string] $Path, [string] $Label) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (
        $item -isnot [IO.DirectoryInfo] -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $null -ne $item.LinkType
    ) {
        throw "$Label must be an ordinary directory."
    }
}

function Assert-ClosedPluginReleaseTree([string] $Path) {
    Assert-OrdinaryDirectory $Path 'Plugin release root'
    $allowed = @('.codex-plugin', 'skills')
    $entries = @(Get-ChildItem -LiteralPath $Path -Force -ErrorAction Stop)
    if (@($entries | Where-Object { $_.Name -cnotin $allowed }).Count -ne 0) {
        throw 'Plugin release contains an unexpected top-level entry.'
    }
    foreach ($required in $allowed) {
        $requiredPath = Join-Path $Path $required
        Assert-OrdinaryDirectory $requiredPath "Plugin release entry $required"
    }
    foreach ($entry in @(
        Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction Stop
    )) {
        if (
            ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            $null -ne $entry.LinkType
        ) {
            throw 'Plugin release tree contains a link or reparse point.'
        }
    }
}

function New-CleanPluginStage([string] $SourceRoot, [string] $PluginsRoot) {
    Assert-OrdinaryDirectory $SourceRoot 'Plugin source root'
    Assert-OrdinaryDirectory (
        Join-Path $SourceRoot '.codex-plugin'
    ) 'Plugin manifest directory'
    Assert-OrdinaryDirectory (
        Join-Path $SourceRoot 'skills'
    ) 'Plugin skills directory'
    Assert-OrdinaryDirectory $PluginsRoot 'Plugins root'

    $stageName = '.change-delivery-plugin.installing-' + (
        [guid]::NewGuid().ToString('N')
    )
    $stage = Join-Path $PluginsRoot $stageName
    New-Item -ItemType Directory -Path $stage -ErrorAction Stop | Out-Null
    try {
        Copy-Item -LiteralPath (
            Join-Path $SourceRoot '.codex-plugin'
        ) -Destination $stage -Recurse -ErrorAction Stop
        Copy-Item -LiteralPath (
            Join-Path $SourceRoot 'skills'
        ) -Destination $stage -Recurse -ErrorAction Stop
        Assert-ClosedPluginReleaseTree $stage
        return $stage
    }
    catch {
        if (Test-Path -LiteralPath $stage) {
            [IO.Directory]::Delete($stage, $true)
        }
        throw
    }
}

function Assert-ManagedPluginRelease([string] $Path) {
    Assert-ClosedPluginReleaseTree $Path
    $manifestPath = Join-Path $Path '.codex-plugin\plugin.json'
    $manifestText = [IO.File]::ReadAllText(
        $manifestPath,
        [Text.UTF8Encoding]::new($false, $true)
    )
    $manifest = ConvertFrom-Json $manifestText
    if (
        $manifest -isnot [PSCustomObject] -or
        $null -eq $manifest.name -or
        [string] $manifest.name -cne 'change-delivery-plugin'
    ) {
        throw 'Managed plugin release has an unexpected identity.'
    }
}

function Install-CleanPluginStage {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [string] $StageRoot,
        [Parameter(Mandatory)] [string] $TargetRoot,
        [Parameter(Mandatory)] [string] $ExpectedLegacySource
    )

    Assert-ManagedPluginRelease $StageRoot
    $stageItem = Get-Item -LiteralPath $StageRoot -Force -ErrorAction Stop
    $targetFullPath = [IO.Path]::GetFullPath($TargetRoot).TrimEnd('\')
    $parent = Split-Path -Parent $targetFullPath
    Assert-OrdinaryDirectory $parent 'Plugins root'
    if (
        $stageItem.Parent.FullName.TrimEnd('\') -cne $parent -or
        [IO.Path]::GetFileName($targetFullPath) -cne 'change-delivery-plugin' -or
        -not $stageItem.Name.StartsWith(
            '.change-delivery-plugin.installing-',
            [StringComparison]::Ordinal
        )
    ) {
        throw 'Plugin release paths do not match the fixed publishing layout.'
    }

    $backup = Join-Path $parent (
        '.change-delivery-plugin.backup-' + [guid]::NewGuid().ToString('N')
    )
    $legacyTarget = $null

    if (Test-Path -LiteralPath $targetFullPath) {
        $current = Get-Item -LiteralPath $targetFullPath -Force -ErrorAction Stop
        if ($current.LinkType -eq 'Junction') {
            $targets = @($current.Target)
            if ($targets.Count -ne 1) {
                throw 'Legacy plugin junction target is ambiguous.'
            }
            $legacyTarget = (
                Resolve-Path -LiteralPath ([string] $targets[0]) -ErrorAction Stop
            ).ProviderPath
            $expected = (
                Resolve-Path -LiteralPath $ExpectedLegacySource -ErrorAction Stop
            ).ProviderPath
            if (-not [string]::Equals(
                $legacyTarget.TrimEnd('\'),
                $expected.TrimEnd('\'),
                [StringComparison]::OrdinalIgnoreCase
            )) {
                throw 'Legacy plugin junction points to an unexpected source.'
            }
            $current.Delete($false)
        }
        else {
            Assert-ManagedPluginRelease $targetFullPath
            Move-Item -LiteralPath $targetFullPath -Destination $backup -ErrorAction Stop
        }
    }

    try {
        Move-Item -LiteralPath $StageRoot -Destination $targetFullPath -ErrorAction Stop
    }
    catch {
        if (Test-Path -LiteralPath $backup) {
            Move-Item -LiteralPath $backup -Destination $targetFullPath -ErrorAction Stop
        }
        elseif (
            $null -ne $legacyTarget -and
            -not (Test-Path -LiteralPath $targetFullPath)
        ) {
            New-Item -ItemType Junction -Path $targetFullPath `
                -Target $legacyTarget -ErrorAction Stop | Out-Null
        }
        throw
    }

    if (Test-Path -LiteralPath $backup) {
        Assert-ManagedPluginRelease $backup
        [IO.Directory]::Delete($backup, $true)
    }
}
