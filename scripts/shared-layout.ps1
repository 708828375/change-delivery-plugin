$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Resolve-CanonicalOrdinaryDirectory {
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [string] $Label
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "$Label 不能为空。"
    }
    $fullPath = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $item = Get-Item -LiteralPath $fullPath -Force -ErrorAction Stop
    if ($item -isnot [IO.DirectoryInfo]) {
        throw "$Label 必须是目录。"
    }
    $cursor = $item
    while ($null -ne $cursor) {
        if (
            ($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            $null -ne $cursor.LinkType
        ) {
            throw "$Label 的路径链不能包含链接或 reparse point。"
        }
        $cursor = $cursor.Parent
    }
    return (Resolve-Path -LiteralPath $fullPath -ErrorAction Stop).ProviderPath.TrimEnd('\')
}

function Assert-PathWithinRoot {
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [string] $Root,
        [Parameter(Mandatory)] [string] $Label
    )

    $candidate = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $boundary = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    if (
        $candidate -cne $boundary -and
        -not $candidate.StartsWith(
            $boundary + '\',
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "$Label 超出当前用户安装根目录。"
    }
    return $candidate
}

function Get-SharedInstallLayout {
    [CmdletBinding()]
    param([string] $UserProfilePath)

    if ([string]::IsNullOrWhiteSpace($UserProfilePath)) {
        $knownProfile = [Environment]::GetFolderPath(
            [Environment+SpecialFolder]::UserProfile
        )
        if ([string]::IsNullOrWhiteSpace($knownProfile)) {
            throw 'Windows Known Folder 未返回用户目录。'
        }
        if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
            throw 'USERPROFILE 不可用。'
        }
        $knownFull = [IO.Path]::GetFullPath($knownProfile).TrimEnd('\')
        $environmentFull = [IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\')
        if (-not [string]::Equals(
            $knownFull,
            $environmentFull,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw 'USERPROFILE 与 Windows Known Folder 不一致。'
        }
        $UserProfilePath = $knownFull
    }

    $profile = Resolve-CanonicalOrdinaryDirectory `
        -Path $UserProfilePath `
        -Label '用户目录'
    $pluginsRoot = Assert-PathWithinRoot `
        -Path (Join-Path $profile 'plugins') `
        -Root $profile `
        -Label '插件目录'
    $pluginRoot = Assert-PathWithinRoot `
        -Path (Join-Path $pluginsRoot 'change-delivery-plugin') `
        -Root $profile `
        -Label '插件发布目录'
    $dataRoot = Assert-PathWithinRoot `
        -Path (Join-Path $profile '.codex\change-delivery') `
        -Root $profile `
        -Label '运行数据目录'

    [PSCustomObject]@{
        user_profile = $profile
        plugins_root = $pluginsRoot
        plugin_root = $pluginRoot
        data_root = $dataRoot
        venv_root = Assert-PathWithinRoot `
            -Path (Join-Path $dataRoot 'runtime\venv') `
            -Root $profile `
            -Label 'Python 虚拟环境目录'
        host_root = Assert-PathWithinRoot `
            -Path (Join-Path $dataRoot 'host') `
            -Root $profile `
            -Label '受信任运行主机目录'
        launcher_path = Assert-PathWithinRoot `
            -Path (Join-Path $dataRoot 'bin\change-delivery.ps1') `
            -Root $profile `
            -Label '稳定启动器'
    }
}
