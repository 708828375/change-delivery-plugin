[CmdletBinding()]
param(
    [string] $SourceRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Resolve-CanonicalPath([string] $Path) {
    return (Resolve-Path -LiteralPath $Path -ErrorAction Stop).ProviderPath
}

$source = Resolve-CanonicalPath $SourceRoot
$sourceItem = Get-Item -LiteralPath $source -Force
if (-not $sourceItem.PSIsContainer) {
    throw 'Plugin source root must be a directory.'
}

$manifest = Join-Path $source '.codex-plugin\plugin.json'
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
    throw 'Plugin source root does not contain a manifest.'
}

$userProfile = Resolve-CanonicalPath (
    [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
)
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

$PluginsRoot = Join-Path $userProfile 'plugins'
if (-not (Test-Path -LiteralPath $PluginsRoot)) {
    New-Item -ItemType Directory -Path $PluginsRoot -ErrorAction Stop | Out-Null
}
$plugins = Resolve-CanonicalPath $PluginsRoot
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
