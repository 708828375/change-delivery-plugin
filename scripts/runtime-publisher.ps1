$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-OrdinaryRuntimeDirectory {
    param([string] $Path, [string] $Label)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (
        $item -isnot [IO.DirectoryInfo] -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $null -ne $item.LinkType
    ) {
        throw "$Label 必须是普通目录。"
    }
}

function Assert-OrdinaryRuntimeFile {
    param([string] $Path, [string] $Label)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (
        $item -isnot [IO.FileInfo] -or
        ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $null -ne $item.LinkType
    ) {
        throw "$Label 必须是普通文件。"
    }
}

function Assert-ManagedVirtualEnvironment([string] $Path) {
    Assert-OrdinaryRuntimeDirectory $Path 'Python 虚拟环境目录'
    Assert-OrdinaryRuntimeFile (
        Join-Path $Path 'Scripts\python.exe'
    ) 'Python 虚拟环境解释器'
    foreach ($entry in @(Get-ChildItem -LiteralPath $Path -Recurse -Force)) {
        if (
            ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            $null -ne $entry.LinkType
        ) {
            throw 'Python 虚拟环境不能包含链接或 reparse point。'
        }
    }
}

function Assert-ClosedRuntimeHostTree([string] $Path) {
    Assert-OrdinaryRuntimeDirectory $Path '运行主机根目录'
    $allowed = @('pyproject.toml', 'schemas', 'scripts', 'src')
    $entries = @(Get-ChildItem -LiteralPath $Path -Force -ErrorAction Stop)
    if (@($entries | Where-Object { $_.Name -cnotin $allowed }).Count -ne 0) {
        throw '运行主机包含非预期的顶层内容。'
    }
    foreach ($required in @('schemas', 'scripts', 'src')) {
        Assert-OrdinaryRuntimeDirectory `
            (Join-Path $Path $required) `
            "运行主机目录 $required"
    }
    $projectFile = Get-Item -LiteralPath (
        Join-Path $Path 'pyproject.toml'
    ) -Force -ErrorAction Stop
    if (
        $projectFile -isnot [IO.FileInfo] -or
        ($projectFile.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $null -ne $projectFile.LinkType
    ) {
        throw '运行主机 pyproject.toml 必须是普通文件。'
    }
    $projectText = [IO.File]::ReadAllText(
        $projectFile.FullName,
        [Text.UTF8Encoding]::new($false, $true)
    )
    if ($projectText -notmatch '(?m)^name\s*=\s*["'']change-delivery["'']\s*$') {
        throw '运行主机的 Python 包身份不正确。'
    }
    foreach ($entry in @(Get-ChildItem -LiteralPath $Path -Recurse -Force)) {
        if (
            ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            $null -ne $entry.LinkType
        ) {
            throw '运行主机内容不能包含链接或 reparse point。'
        }
        if (
            ($entry -is [IO.DirectoryInfo] -and $entry.Name -ceq '__pycache__') -or
            ($entry -is [IO.FileInfo] -and $entry.Extension -ieq '.pyc')
        ) {
            throw '运行主机内容不能包含 Python bytecode 或缓存目录。'
        }
    }
}

function New-CleanRuntimeHostStage {
    param([string] $SourceRoot, [string] $DataRoot)

    Assert-OrdinaryRuntimeDirectory $SourceRoot '运行时源码根目录'
    Assert-OrdinaryRuntimeDirectory $DataRoot '运行数据目录'
    foreach ($required in @('schemas', 'scripts', 'src')) {
        Assert-OrdinaryRuntimeDirectory `
            (Join-Path $SourceRoot $required) `
            "源码目录 $required"
    }
    $stage = Join-Path $DataRoot (
        '.host.installing-' + [Guid]::NewGuid().ToString('N')
    )
    New-Item -ItemType Directory -Path $stage -ErrorAction Stop | Out-Null
    try {
        foreach ($directory in @('schemas', 'scripts', 'src')) {
            Copy-Item -LiteralPath (Join-Path $SourceRoot $directory) `
                -Destination $stage -Recurse -ErrorAction Stop
        }
        Copy-Item -LiteralPath (Join-Path $SourceRoot 'pyproject.toml') `
            -Destination $stage -ErrorAction Stop
        Assert-ClosedRuntimeHostTree $stage
        return $stage
    }
    catch {
        if (Test-Path -LiteralPath $stage) {
            [IO.Directory]::Delete($stage, $true)
        }
        throw
    }
}

function Install-CleanRuntimeHostStage {
    param([string] $StageRoot, [string] $TargetRoot)

    Assert-ClosedRuntimeHostTree $StageRoot
    $stageItem = Get-Item -LiteralPath $StageRoot -Force -ErrorAction Stop
    $target = [IO.Path]::GetFullPath($TargetRoot).TrimEnd('\')
    $parent = Split-Path -Parent $target
    Assert-OrdinaryRuntimeDirectory $parent '运行数据目录'
    if (
        $stageItem.Parent.FullName.TrimEnd('\') -cne $parent -or
        [IO.Path]::GetFileName($target) -cne 'host' -or
        -not $stageItem.Name.StartsWith(
            '.host.installing-',
            [StringComparison]::Ordinal
        )
    ) {
        throw '运行主机发布路径不符合固定布局。'
    }

    $backup = Join-Path $parent (
        '.host.backup-' + [Guid]::NewGuid().ToString('N')
    )
    if (Test-Path -LiteralPath $target) {
        Assert-ClosedRuntimeHostTree $target
        Move-Item -LiteralPath $target -Destination $backup -ErrorAction Stop
    }
    try {
        Move-Item -LiteralPath $StageRoot -Destination $target -ErrorAction Stop
        Assert-ClosedRuntimeHostTree $target
    }
    catch {
        if (Test-Path -LiteralPath $target) {
            Assert-ClosedRuntimeHostTree $target
            [IO.Directory]::Delete($target, $true)
        }
        if (Test-Path -LiteralPath $backup) {
            Move-Item -LiteralPath $backup -Destination $target -ErrorAction Stop
        }
        throw
    }
    if (Test-Path -LiteralPath $backup) {
        Assert-ClosedRuntimeHostTree $backup
        [IO.Directory]::Delete($backup, $true)
    }
}

function Install-PrivateVirtualEnvironment {
    param([string] $PythonPath, [string] $SourceRoot, [string] $VenvRoot)

    $python = (Resolve-Path -LiteralPath $PythonPath -ErrorAction Stop).ProviderPath
    $pythonItem = Get-Item -LiteralPath $python -Force -ErrorAction Stop
    if (
        $pythonItem -isnot [IO.FileInfo] -or
        ($pythonItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $null -ne $pythonItem.LinkType
    ) {
        throw '引导 Python 必须是普通文件。'
    }
    $version = @(& $python --version 2>&1) -join ''
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^Python 3\.(?<minor>[0-9]+)\.') {
        throw '无法验证引导 Python 版本。'
    }
    if ([int] $Matches.minor -lt 12) {
        throw '共享版本要求 Python 3.12 或更高版本。'
    }

    $target = [IO.Path]::GetFullPath($VenvRoot).TrimEnd('\')
    $parent = Split-Path -Parent $target
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -ErrorAction Stop | Out-Null
    }
    Assert-OrdinaryRuntimeDirectory $parent '虚拟环境父目录'
    $stage = Join-Path $parent (
        '.venv.installing-' + [Guid]::NewGuid().ToString('N')
    )
    $backup = Join-Path $parent (
        '.venv.backup-' + [Guid]::NewGuid().ToString('N')
    )
    try {
        & $python -m venv $stage
        if ($LASTEXITCODE -ne 0) { throw '创建专用 Python 虚拟环境失败。' }
        $stagePython = Join-Path $stage 'Scripts\python.exe'
        & $stagePython -m pip install --disable-pip-version-check $SourceRoot
        if ($LASTEXITCODE -ne 0) { throw '安装 Change Delivery 运行时失败。' }
        & $stagePython -m change_delivery --help | Out-Null
        if ($LASTEXITCODE -ne 0) { throw '运行时烟雾测试失败。' }
        Assert-ManagedVirtualEnvironment $stage
        if (Test-Path -LiteralPath $target) {
            Assert-ManagedVirtualEnvironment $target
            Move-Item -LiteralPath $target -Destination $backup -ErrorAction Stop
        }
        Move-Item -LiteralPath $stage -Destination $target -ErrorAction Stop
        $stage = $null
        Assert-ManagedVirtualEnvironment $target
        if (Test-Path -LiteralPath $backup) {
            [IO.Directory]::Delete($backup, $true)
        }
    }
    catch {
        if ($null -ne $stage -and (Test-Path -LiteralPath $stage)) {
            [IO.Directory]::Delete($stage, $true)
        }
        if (
            (Test-Path -LiteralPath $backup) -and
            -not (Test-Path -LiteralPath $target)
        ) {
            Move-Item -LiteralPath $backup -Destination $target -ErrorAction Stop
        }
        throw
    }
}

function Write-StableLauncher {
    param([string] $LauncherPath, [string] $VenvRoot)

    $launcher = [IO.Path]::GetFullPath($LauncherPath)
    $parent = Split-Path -Parent $launcher
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -ErrorAction Stop | Out-Null
    }
    Assert-OrdinaryRuntimeDirectory $parent '启动器目录'
    if (Test-Path -LiteralPath $launcher) {
        Assert-OrdinaryRuntimeFile $launcher '现有稳定启动器'
    }
    $venvPython = Join-Path ([IO.Path]::GetFullPath($VenvRoot)) (
        'Scripts\python.exe'
    )
    $quotedPython = $venvPython.Replace("'", "''")
    $content = @"
`$ErrorActionPreference = 'Stop'
`$pythonPath = '$quotedPython'
if (-not (Test-Path -LiteralPath `$pythonPath -PathType Leaf)) {
    throw 'Change Delivery 专用 Python 不存在，请重新安装插件。'
}
& `$pythonPath -m change_delivery @args
exit `$LASTEXITCODE
"@
    $temporary = $launcher + '.installing-' + [Guid]::NewGuid().ToString('N')
    try {
        [IO.File]::WriteAllText(
            $temporary,
            $content,
            [Text.UTF8Encoding]::new($true)
        )
        Assert-OrdinaryRuntimeFile $temporary '临时稳定启动器'
        Move-Item -LiteralPath $temporary -Destination $launcher `
            -Force -ErrorAction Stop
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Assert-OrdinaryRuntimeFile $temporary '临时稳定启动器'
            [IO.File]::Delete($temporary)
        }
    }
}
