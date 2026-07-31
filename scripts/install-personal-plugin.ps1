[CmdletBinding()]
param(
    [string] $SourceRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Resolve-CanonicalPath([string] $Path) {
    return (Resolve-Path -LiteralPath $Path -ErrorAction Stop).ProviderPath
}

function Assert-SafePluginsRoot([string] $Path, [string] $ExpectedRoot) {
    $lexical = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $expected = [IO.Path]::GetFullPath($ExpectedRoot).TrimEnd('\')
    if (-not [string]::Equals(
        $lexical, $expected, [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'Plugins root differs from the fixed personal path.'
    }

    $cursor = $lexical
    while (-not (Test-Path -LiteralPath $cursor)) {
        $parent = Split-Path -Parent $cursor
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -ceq $cursor) {
            throw 'Plugins root has no existing trusted ancestor.'
        }
        $cursor = $parent
    }
    $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
    while ($null -ne $item) {
        if (
            $item -isnot [IO.DirectoryInfo] -or
            ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            $null -ne $item.LinkType
        ) {
            throw 'Plugins root path contains a link or reparse point.'
        }
        $item = $item.Parent
    }

    if (Test-Path -LiteralPath $lexical) {
        $rootItem = Get-Item -LiteralPath $lexical -Force -ErrorAction Stop
        if (
            $rootItem -isnot [IO.DirectoryInfo] -or
            ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            $null -ne $rootItem.LinkType
        ) {
            throw 'Plugins root must be an ordinary non-link directory.'
        }
        $canonical = (Resolve-Path -LiteralPath $lexical -ErrorAction Stop).ProviderPath
        if (-not [string]::Equals(
            $canonical.TrimEnd('\'), $expected,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw 'Plugins root canonical path differs from the fixed personal path.'
        }
    }
    return $lexical
}

$fixedPluginsRoot = 'C:\Users\ExampleUser\plugins'
$plugins = Assert-SafePluginsRoot $fixedPluginsRoot $fixedPluginsRoot
$fixedUserProfile = 'C:\Users\ExampleUser'
$lexicalUserProfile = [IO.Path]::GetFullPath(
    [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
).TrimEnd('\')
if (-not [string]::Equals(
    $lexicalUserProfile,
    $fixedUserProfile,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw 'Current user profile differs from the fixed personal trust anchor.'
}
# Safe to canonicalize only after the fixed root and its ancestors passed link checks.
$userProfile = Resolve-CanonicalPath $lexicalUserProfile

$source = Resolve-CanonicalPath $SourceRoot
$sourceItem = Get-Item -LiteralPath $source -Force
if (-not $sourceItem.PSIsContainer) {
    throw 'Plugin source root must be a directory.'
}

$manifest = Join-Path $source '.codex-plugin\plugin.json'
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
    throw 'Plugin source root does not contain a manifest.'
}

$validator = Join-Path $userProfile (
    '.codex\skills\.system\plugin-creator\scripts\validate_plugin.py'
)
if (-not (Test-Path -LiteralPath $validator -PathType Leaf)) {
    throw 'The official Plugin validator is unavailable.'
}
& python $validator $source
if ($LASTEXITCODE -ne 0) {
    throw 'Plugin validation failed.'
}

$marketplaceJson = & codex plugin marketplace list --json
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to read Codex Marketplace metadata.'
}
$marketplaces = @((ConvertFrom-Json ($marketplaceJson -join [Environment]::NewLine)).marketplaces)
$personal = @(
    $marketplaces | Where-Object {
        $null -ne $_.name -and
        $null -ne $_.root -and
        [IO.Path]::GetFullPath([string] $_.root).TrimEnd('\') -eq
            $userProfile.TrimEnd('\')
    }
)
if ($personal.Count -ne 1) {
    throw 'Exactly one default personal Marketplace is required.'
}
$marketplaceName = [string] $personal[0].name
if ($marketplaceName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') {
    throw 'The personal Marketplace name is invalid.'
}

$derivedPluginsRoot = [IO.Path]::GetFullPath(
    (Join-Path $userProfile 'plugins')
).TrimEnd('\')
if (-not [string]::Equals(
    $derivedPluginsRoot,
    $fixedPluginsRoot,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw 'Current user profile does not match the fixed personal Plugin root.'
}
if (-not (Test-Path -LiteralPath $plugins)) {
    New-Item -ItemType Directory -Path $plugins -ErrorAction Stop | Out-Null
    $plugins = Assert-SafePluginsRoot $fixedPluginsRoot $fixedPluginsRoot
}
$target = Join-Path $plugins 'change-delivery-plugin'
if (Test-Path -LiteralPath $target) {
    $targetItem = Get-Item -LiteralPath $target -Force
    if ($targetItem.LinkType -ne 'Junction') {
        throw 'Plugin target exists and is not a junction.'
    }
    $targets = @($targetItem.Target)
    if ($targets.Count -ne 1) {
        throw 'Plugin junction has an ambiguous target.'
    }
    if (-not [IO.Path]::IsPathRooted([string] $targets[0])) {
        throw 'Plugin junction target must be absolute.'
    }
    $resolvedTarget = Resolve-CanonicalPath ([string] $targets[0])
    if ($resolvedTarget.TrimEnd('\') -ne $source.TrimEnd('\')) {
        throw 'Plugin junction points to a different source.'
    }
}
else {
    New-Item -ItemType Junction -Path $target -Target $source -ErrorAction Stop |
        Out-Null
}

$pluginReference = "change-delivery-plugin@$marketplaceName"
& codex plugin add $pluginReference
if ($LASTEXITCODE -ne 0) {
    throw 'Codex Plugin installation failed.'
}

Write-Output "Installed $pluginReference from the validated personal Marketplace."
