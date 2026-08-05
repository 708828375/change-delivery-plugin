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
