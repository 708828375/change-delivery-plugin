[CmdletBinding()]
param([string] $SourceRoot)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ([string]::IsNullOrWhiteSpace($SourceRoot)) {
    $SourceRoot = Split-Path -Parent $PSScriptRoot
}

$layoutHelper = Join-Path $PSScriptRoot 'shared-layout.ps1'
$runtimePublisher = Join-Path $PSScriptRoot 'runtime-publisher.ps1'
$pluginPublisher = Join-Path $PSScriptRoot 'plugin-publisher.ps1'
$hostSecurity = Join-Path $PSScriptRoot 'host-security.ps1'
foreach ($helper in @(
    $layoutHelper, $runtimePublisher, $pluginPublisher, $hostSecurity
)) {
    if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) {
        throw "安装辅助脚本不存在：$helper"
    }
    . $helper
}

function Restore-MarketplaceSnapshot {
    param(
        [string] $CodexPath,
        [string] $MarketplaceName,
        [string] $SnapshotRoot
    )
    $listedJson = & $CodexPath plugin marketplace list --json
    if ($LASTEXITCODE -ne 0) { throw '回滚时无法读取 Marketplace。' }
    $matches = @((ConvertFrom-Json (
        $listedJson -join [Environment]::NewLine
    )).marketplaces | Where-Object {
        [string] $_.name -ceq $MarketplaceName
    })
    if ($matches.Count -gt 1) { throw '回滚时发现 Marketplace 冲突。' }
    if ($matches.Count -eq 1) {
        & $CodexPath plugin marketplace remove $MarketplaceName --json |
            Out-Null
        if ($LASTEXITCODE -ne 0) { throw '移除失败的 Marketplace 快照失败。' }
    }
    & $CodexPath plugin marketplace add $SnapshotRoot --json | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '恢复旧 Marketplace 快照失败。' }
    & $CodexPath plugin add "change-delivery-plugin@$MarketplaceName" |
        Out-Null
    if ($LASTEXITCODE -ne 0) { throw '恢复旧 Plugin 安装失败。' }
}

function Remove-NewMarketplaceInstallation {
    param(
        [string] $CodexPath,
        [string] $MarketplaceName,
        [string] $PluginReference
    )
    $pluginsJson = & $CodexPath plugin list --json
    if ($LASTEXITCODE -ne 0) { throw '回滚首次安装时无法读取 Plugin。' }
    $installed = @((ConvertFrom-Json (
        $pluginsJson -join [Environment]::NewLine
    )).installed | Where-Object {
        [string] $_.pluginId -ceq $PluginReference
    })
    if ($installed.Count -gt 1) { throw '回滚首次安装时发现 Plugin 冲突。' }
    if ($installed.Count -eq 1) {
        & $CodexPath plugin remove $PluginReference --json | Out-Null
        if ($LASTEXITCODE -ne 0) { throw '回滚首次 Plugin 安装失败。' }
    }
    $listedJson = & $CodexPath plugin marketplace list --json
    if ($LASTEXITCODE -ne 0) { throw '回滚首次安装时无法读取 Marketplace。' }
    $matches = @((ConvertFrom-Json (
        $listedJson -join [Environment]::NewLine
    )).marketplaces | Where-Object {
        [string] $_.name -ceq $MarketplaceName
    })
    if ($matches.Count -gt 1) { throw '回滚首次安装时发现 Marketplace 冲突。' }
    if ($matches.Count -eq 1) {
        & $CodexPath plugin marketplace remove $MarketplaceName --json |
            Out-Null
        if ($LASTEXITCODE -ne 0) { throw '回滚首次 Marketplace 安装失败。' }
    }
}

$source = Resolve-CanonicalOrdinaryDirectory `
    -Path $SourceRoot `
    -Label '插件源码根目录'
$codexPath = (Resolve-OfficialCodexIdentity).path
$pluginSource = Resolve-CanonicalOrdinaryDirectory `
    -Path (Join-Path $source 'plugin\change-delivery-plugin') `
    -Label 'Plugin 发布源码目录'
$manifest = Join-Path $pluginSource '.codex-plugin\plugin.json'
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
    throw '插件源码根目录不包含 manifest。'
}
$releaseTag = 'v0.2.2'
$releaseVersion = $releaseTag.Substring(1)
$sourceManifest = ConvertFrom-Json ([IO.File]::ReadAllText(
    $manifest,
    [Text.UTF8Encoding]::new($false, $true)
))
if (
    [string] $sourceManifest.name -cne 'change-delivery-plugin' -or
    [string] $sourceManifest.version -cne $releaseVersion
) {
    throw 'Plugin manifest 身份或版本与共享安装器不一致。'
}
$projectText = [IO.File]::ReadAllText(
    (Join-Path $source 'pyproject.toml'),
    [Text.UTF8Encoding]::new($false, $true)
)
if ($projectText -notmatch (
    '(?ms)^\[project\]\s*.*?^version\s*=\s*["'']' +
    [Regex]::Escape($releaseVersion) + '["'']\s*$'
)) {
    throw 'Python package 版本与共享安装器不一致。'
}

$layout = Get-SharedInstallLayout
foreach ($directory in @(
    $layout.plugins_root,
    $layout.data_root,
    (Split-Path -Parent $layout.venv_root),
    (Split-Path -Parent $layout.launcher_path)
)) {
    if (-not (Test-Path -LiteralPath $directory)) {
        New-Item -ItemType Directory -Path $directory -ErrorAction Stop | Out-Null
    }
    Assert-OrdinaryRuntimeDirectory $directory '共享安装目录'
}

$validator = Join-Path $layout.user_profile (
    '.codex\skills\.system\plugin-creator\scripts\validate_plugin.py'
)
if (-not (Test-Path -LiteralPath $validator -PathType Leaf)) {
    throw 'Codex 官方 Plugin validator 不可用。'
}

$pythonCommand = Get-Command python.exe -CommandType Application `
    -ErrorAction Stop | Select-Object -First 1
$bootstrapPython = $pythonCommand.Source

$runtimeStage = New-CleanRuntimeHostStage `
    -SourceRoot $source `
    -DataRoot $layout.data_root
try {
    & $bootstrapPython $validator $pluginSource
    if ($LASTEXITCODE -ne 0) { throw 'Plugin 校验失败。' }

    Install-PrivateVirtualEnvironment `
        -PythonPath $bootstrapPython `
        -SourceRoot $runtimeStage `
        -VenvRoot $layout.venv_root
    Install-CleanRuntimeHostStage `
        -StageRoot $runtimeStage `
        -TargetRoot $layout.host_root
    $runtimeStage = $null
    Write-StableLauncher `
        -LauncherPath $layout.launcher_path `
        -VenvRoot $layout.venv_root

    & $layout.launcher_path --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '稳定启动器烟雾测试失败。' }
}
finally {
    if ($null -ne $runtimeStage -and (Test-Path -LiteralPath $runtimeStage)) {
        Assert-ClosedRuntimeHostTree $runtimeStage
        [IO.Directory]::Delete($runtimeStage, $true)
    }
}

$pluginStage = New-CleanPluginStage `
    -SourceRoot $pluginSource `
    -PluginsRoot $layout.plugins_root
try {
    & $bootstrapPython $validator $pluginStage
    if ($LASTEXITCODE -ne 0) { throw '已发布 Plugin 校验失败。' }
    Install-CleanPluginStage `
        -StageRoot $pluginStage `
        -TargetRoot $layout.plugin_root `
        -ExpectedLegacySource $source
    $pluginStage = $null
}
finally {
    if ($null -ne $pluginStage -and (Test-Path -LiteralPath $pluginStage)) {
        Assert-ClosedPluginReleaseTree $pluginStage
        [IO.Directory]::Delete($pluginStage, $true)
    }
}

$marketplacePath = Join-Path $source '.agents\plugins\marketplace.json'
if (-not (Test-Path -LiteralPath $marketplacePath -PathType Leaf)) {
    throw '团队 Marketplace 配置不存在。'
}
$marketplace = ConvertFrom-Json ([IO.File]::ReadAllText(
    $marketplacePath,
    [Text.UTF8Encoding]::new($false, $true)
))
$marketplaceName = [string] $marketplace.name
if ($marketplaceName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') {
    throw '团队 Marketplace 名称无效。'
}
$marketplaceSource = '708828375/change-delivery-plugin'

$marketplaceJson = & $codexPath plugin marketplace list --json
if ($LASTEXITCODE -ne 0) { throw '无法读取 Codex Marketplace。' }
$registered = @((ConvertFrom-Json (
    $marketplaceJson -join [Environment]::NewLine
)).marketplaces | Where-Object {
    $null -ne $_.name -and [string] $_.name -ceq $marketplaceName
})
$marketplaceRollbackRoot = $null
if ($registered.Count -eq 1) {
    $registeredRoot = Resolve-CanonicalOrdinaryDirectory `
        -Path ([string] $registered[0].root) `
        -Label '已注册 Marketplace 根目录'
    $registeredMarketplacePath = Join-Path $registeredRoot (
        '.agents\plugins\marketplace.json'
    )
    $registeredManifestPath = Join-Path $registeredRoot (
        'plugin\change-delivery-plugin\.codex-plugin\plugin.json'
    )
    if (
        -not (Test-Path -LiteralPath $registeredMarketplacePath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $registeredManifestPath -PathType Leaf)
    ) {
        throw '同名 Marketplace 不是预期的 Change Delivery 仓库。'
    }
    $registeredMarketplace = ConvertFrom-Json ([IO.File]::ReadAllText(
        $registeredMarketplacePath,
        [Text.UTF8Encoding]::new($false, $true)
    ))
    $registeredManifest = ConvertFrom-Json ([IO.File]::ReadAllText(
        $registeredManifestPath,
        [Text.UTF8Encoding]::new($false, $true)
    ))
    if (
        [string] $registeredMarketplace.name -cne $marketplaceName -or
        [string] $registeredManifest.name -cne 'change-delivery-plugin'
    ) {
        throw '同名 Marketplace 的身份与预期不一致。'
    }
}
elseif ($registered.Count -ne 0) {
    throw '团队 Marketplace 注册信息存在冲突。'
}

if ($registered.Count -eq 1) {
    $stateRoot = Join-Path $layout.data_root 'state'
    if (-not (Test-Path -LiteralPath $stateRoot)) {
        New-Item -ItemType Directory -Path $stateRoot -ErrorAction Stop |
            Out-Null
    }
    Assert-OrdinaryRuntimeDirectory $stateRoot '运行状态目录'
    $marketplaceRollbackRoot = Join-Path $stateRoot (
        '.marketplace.rollback-' + [Guid]::NewGuid().ToString('N')
    )
    New-Item -ItemType Directory -Path $marketplaceRollbackRoot `
        -ErrorAction Stop | Out-Null
    try {
        Copy-Item -LiteralPath (Join-Path $registeredRoot '.agents') `
            -Destination $marketplaceRollbackRoot -Recurse -ErrorAction Stop
        $rollbackPluginParent = Join-Path $marketplaceRollbackRoot 'plugin'
        New-Item -ItemType Directory -Path $rollbackPluginParent `
            -ErrorAction Stop | Out-Null
        Copy-Item -LiteralPath (
            Join-Path $registeredRoot 'plugin\change-delivery-plugin'
        ) -Destination $rollbackPluginParent -Recurse -ErrorAction Stop
        $rollbackPlugin = Join-Path $rollbackPluginParent (
            'change-delivery-plugin'
        )
        Assert-ClosedPluginReleaseTree $rollbackPlugin
        & $bootstrapPython $validator $rollbackPlugin | Out-Null
        if ($LASTEXITCODE -ne 0) { throw '旧 Marketplace 回滚快照校验失败。' }
    }
    catch {
        if (Test-Path -LiteralPath $marketplaceRollbackRoot) {
            [IO.Directory]::Delete($marketplaceRollbackRoot, $true)
        }
        throw
    }
}

$pluginReference = "change-delivery-plugin@$marketplaceName"
try {
    if ($registered.Count -eq 1) {
        & $codexPath plugin marketplace remove $marketplaceName --json |
            Out-Null
        if ($LASTEXITCODE -ne 0) { throw '移除旧 Marketplace 快照失败。' }
    }
    & $codexPath plugin marketplace add $marketplaceSource --ref $releaseTag --json | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '注册团队 Marketplace 失败。' }
    & $codexPath plugin add $pluginReference
    if ($LASTEXITCODE -ne 0) { throw '安装 Codex Plugin 失败。' }
}
catch {
    $installationFailure = $_
    if ($null -ne $marketplaceRollbackRoot) {
        try {
            Restore-MarketplaceSnapshot `
                -CodexPath $codexPath `
                -MarketplaceName $marketplaceName `
                -SnapshotRoot $marketplaceRollbackRoot
        }
        catch {
            throw "共享安装失败，且旧 Marketplace 回滚失败：$($_.Exception.Message)"
        }
    }
    else {
        try {
            Remove-NewMarketplaceInstallation `
                -CodexPath $codexPath `
                -MarketplaceName $marketplaceName `
                -PluginReference $pluginReference
        }
        catch {
            throw "共享安装失败，且首次安装状态回滚失败：$($_.Exception.Message)"
        }
    }
    throw $installationFailure
}
if (
    $null -ne $marketplaceRollbackRoot -and
    (Test-Path -LiteralPath $marketplaceRollbackRoot)
) {
    [IO.Directory]::Delete($marketplaceRollbackRoot, $true)
}

Write-Output "已安装 $pluginReference。"
Write-Output "稳定启动器：$($layout.launcher_path)"
Write-Output "用户数据目录：$($layout.data_root)"
Write-Output '请新建 Codex CLI 会话后使用 $change-delivery。'
