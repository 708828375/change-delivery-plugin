[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [switch] $PurgeData,
    [string] $MarketplaceName = 'joysun-team'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$scriptRoot = Split-Path -Parent $PSCommandPath
. (Join-Path $scriptRoot 'shared-layout.ps1')
. (Join-Path $scriptRoot 'runtime-publisher.ps1')
. (Join-Path $scriptRoot 'plugin-publisher.ps1')
. (Join-Path $scriptRoot 'host-security.ps1')

function Assert-OrdinaryManagedFile([string] $Path, [string] $Label) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (
        $item -isnot [IO.FileInfo] -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $null -ne $item.LinkType
    ) {
        throw "$Label 必须是普通文件。"
    }
}

function Assert-PurgeableDataTree([string] $Path) {
    $root = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (
        $root -isnot [IO.DirectoryInfo] -or
        ($root.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $null -ne $root.LinkType
    ) {
        throw '运行数据根目录必须是普通目录。'
    }
    $allowed = @(
        'projects', 'runs', 'logs', 'state',
        'runtime', 'host', 'bin', 'runtime-host.json'
    )
    $entries = @(Get-ChildItem -LiteralPath $Path -Force -ErrorAction Stop)
    if (@($entries | Where-Object { $_.Name -cnotin $allowed }).Count -ne 0) {
        throw '运行数据目录包含非预期的顶层内容，拒绝整体删除。'
    }
    foreach ($entry in @(Get-ChildItem -LiteralPath $Path -Recurse -Force)) {
        if (
            ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            $null -ne $entry.LinkType
        ) {
            throw '运行数据目录不能包含链接或 reparse point。'
        }
    }
}

$layout = Get-SharedInstallLayout
$codexPath = (Resolve-OfficialCodexIdentity).path
$pluginReference = "change-delivery-plugin@$MarketplaceName"

$pluginListJson = & $codexPath plugin list --json
if ($LASTEXITCODE -ne 0) { throw '无法读取 Codex 插件列表。' }
$pluginCatalog = ConvertFrom-Json (
    $pluginListJson -join [Environment]::NewLine
)
$installed = @($pluginCatalog.installed | Where-Object {
    $serialized = ConvertTo-Json $_ -Depth 8 -Compress
    $serialized.Contains('change-delivery-plugin') -and
    $serialized.Contains($MarketplaceName)
})
if ($installed.Count -gt 1) {
    throw '发现多个同名的已安装插件，拒绝自动移除。'
}
if ($installed.Count -eq 1) {
    if ($PSCmdlet.ShouldProcess($pluginReference, '从 Codex 移除插件注册')) {
        & $codexPath plugin remove $pluginReference --json | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw 'Codex 插件移除失败。'
        }
    }
}

$taskName = 'Codex Change Delivery Tick'
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -ne $task) {
    $tasks = @($task)
    if ($tasks.Count -ne 1 -or [string] $tasks[0].TaskPath -cne '\') {
        throw '发现同名但身份不明确的计划任务，拒绝删除。'
    }
    $expectedTick = Join-Path $layout.host_root 'scripts\run-tick.ps1'
    $escapedTick = $expectedTick.Replace('"', '""')
    $expectedArguments = (
        '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass ' +
        '-File "' + $escapedTick + '"'
    )
    $actions = @($tasks[0].Actions)
    $arguments = if ($actions.Count -eq 1) {
        [string] $actions[0].Arguments
    }
    else { '' }
    if (
        $actions.Count -ne 1 -or
        [string] $actions[0].Execute -cne 'powershell.exe' -or
        $arguments -cne $expectedArguments
    ) {
        throw '计划任务未指向受管运行主机，拒绝删除。'
    }
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $timeTriggers = @($tasks[0].Triggers | Where-Object {
        $_.CimClass.CimClassName -eq 'MSFT_TaskTimeTrigger'
    })
    if (
        $timeTriggers.Count -ne 1 -or
        [string]::IsNullOrWhiteSpace(
            [string] $timeTriggers[0].StartBoundary
        )
    ) {
        throw '计划任务的定时触发器身份不正确。'
    }
    $timerStart = [datetime]::Parse([string] $timeTriggers[0].StartBoundary)
    $expectedAction = New-ScheduledTaskAction `
        -Execute 'powershell.exe' `
        -Argument $expectedArguments
    $expectedLogonTrigger = New-ScheduledTaskTrigger `
        -AtLogOn `
        -User $currentUser
    $expectedTimerTrigger = New-ScheduledTaskTrigger `
        -Once `
        -At $timerStart `
        -RepetitionInterval (New-TimeSpan -Minutes 5)
    $expectedPrincipal = New-ScheduledTaskPrincipal `
        -UserId $currentUser `
        -LogonType Interactive `
        -RunLevel Limited
    $expectedSettings = New-ScheduledTaskSettingsSet `
        -WakeToRun:$false `
        -MultipleInstances IgnoreNew `
        -StartWhenAvailable
    $expectedTask = New-ScheduledTask `
        -Action $expectedAction `
        -Trigger @($expectedLogonTrigger, $expectedTimerTrigger) `
        -Principal $expectedPrincipal `
        -Settings $expectedSettings
    if (-not (Test-StrictTaskDefinition `
        $tasks $expectedTask $taskName $currentUser
    )) {
        throw '计划任务与完整受管定义不一致，拒绝删除。'
    }
    if ($PSCmdlet.ShouldProcess($taskName, '注销计划任务')) {
        Unregister-ScheduledTask -TaskName $taskName -TaskPath '\' -Confirm:$false
    }
}

$runtimeHostPath = Join-Path $layout.data_root 'runtime-host.json'
if (Test-Path -LiteralPath $runtimeHostPath) {
    Assert-OrdinaryManagedFile $runtimeHostPath '运行主机配置'
    $runtimeHostItem = Get-Item -LiteralPath $runtimeHostPath -Force
    if ($runtimeHostItem.Length -gt 16KB) {
        throw '运行主机配置超过允许大小。'
    }
    $runtimeHost = ConvertFrom-Json ([IO.File]::ReadAllText(
        $runtimeHostPath,
        [Text.UTF8Encoding]::new($false, $true)
    ))
    $expectedModuleRoot = Join-Path $layout.host_root 'src'
    if (
        $runtimeHost -isnot [PSCustomObject] -or
        $runtimeHost.schema_version -ne 1 -or
        [string] $runtimeHost.module_root -cne $expectedModuleRoot
    ) {
        throw '运行主机配置身份不正确，拒绝删除。'
    }
    if ($PSCmdlet.ShouldProcess($runtimeHostPath, '删除可重建的运行主机配置')) {
        [IO.File]::Delete($runtimeHostPath)
    }
}

if (Test-Path -LiteralPath $layout.plugin_root) {
    Assert-ManagedPluginRelease $layout.plugin_root
    if ($PSCmdlet.ShouldProcess($layout.plugin_root, '删除受管插件发布目录')) {
        [IO.Directory]::Delete($layout.plugin_root, $true)
    }
}

if (Test-Path -LiteralPath $layout.host_root) {
    Assert-ClosedRuntimeHostTree $layout.host_root
    if ($PSCmdlet.ShouldProcess($layout.host_root, '删除受信任运行主机')) {
        [IO.Directory]::Delete($layout.host_root, $true)
    }
}

if (Test-Path -LiteralPath $layout.venv_root) {
    Assert-OrdinaryRuntimeDirectory $layout.venv_root 'Python 虚拟环境目录'
    Assert-OrdinaryManagedFile `
        (Join-Path $layout.venv_root 'Scripts\python.exe') `
        'Python 虚拟环境解释器'
    foreach ($entry in @(
        Get-ChildItem -LiteralPath $layout.venv_root -Recurse -Force
    )) {
        if (
            ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            $null -ne $entry.LinkType
        ) {
            throw 'Python 虚拟环境不能包含链接或 reparse point。'
        }
    }
    if ($PSCmdlet.ShouldProcess($layout.venv_root, '删除专用 Python 虚拟环境')) {
        [IO.Directory]::Delete($layout.venv_root, $true)
    }
}

if (Test-Path -LiteralPath $layout.launcher_path) {
    Assert-OrdinaryManagedFile $layout.launcher_path '稳定启动器'
    if ($PSCmdlet.ShouldProcess($layout.launcher_path, '删除稳定启动器')) {
        [IO.File]::Delete($layout.launcher_path)
    }
}

if ($PurgeData) {
    if (Test-Path -LiteralPath $layout.data_root) {
        Assert-PurgeableDataTree $layout.data_root
        if ($PSCmdlet.ShouldProcess(
            $layout.data_root,
            '删除 projects、runs、logs、state 等全部插件数据'
        )) {
            [IO.Directory]::Delete($layout.data_root, $true)
        }
    }
}
else {
    Write-Host "已保留用户数据目录：$($layout.data_root)"
    Write-Host '如需一并清理 projects、runs、logs、state，请使用 -PurgeData。'
}
