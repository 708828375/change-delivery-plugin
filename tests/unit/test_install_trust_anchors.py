from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[2]
SECURITY = ROOT / "scripts" / "host-security.ps1"
PERSONAL = ROOT / "scripts" / "install-personal-plugin.ps1"
SCHEDULED = ROOT / "scripts" / "install-scheduled-task.ps1"
TICK = ROOT / "scripts" / "run-tick.ps1"


def run_ps(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )


def test_alternate_plugins_root_is_rejected_before_any_command(tmp_path: Path) -> None:
    marker = tmp_path / "called.txt"
    command = rf"""
function python {{ Set-Content -LiteralPath '{marker}' -Value python; exit 90 }}
function codex {{ Set-Content -LiteralPath '{marker}' -Value codex; exit 91 }}
& '{PERSONAL}' -PluginsRoot '{tmp_path}'
if ($LASTEXITCODE -eq 0) {{ exit 92 }}
if (Test-Path -LiteralPath '{marker}') {{ exit 93 }}
"""
    result = run_ps(command)
    assert result.returncode == 0, result.stderr + result.stdout
    assert not marker.exists()


def test_discovered_tools_and_private_python_are_pinned_without_overrides() -> None:
    contract = SECURITY.read_text(encoding="utf-8")
    scheduled = SCHEDULED.read_text(encoding="utf-8")

    assert "C:\\softs\\nodejs\\node_global\\node_modules\\@openai\\codex" not in contract
    assert "C:\\softs\\python-3.12.10\\python.exe" not in contract
    assert "Join-Path $layout.venv_root 'Scripts\\python.exe'" in contract
    assert "C:\\Program Files\\GitHub CLI\\gh.exe" not in contract
    assert "Get-Command npm" not in contract
    assert "Get-Command python" not in contract
    assert "Get-Command codex.cmd" in contract
    assert "Get-Command gh.exe" in contract
    assert "npm root" not in contract
    assert "NpmGlobalRoot" not in scheduled
    assert "PythonPath" not in scheduled
    assert "$gh = Resolve-InstalledGitHubCliIdentity" in contract
    assert "$Config.gh_path -cne $gh.path" in contract
    assert "$Config.gh_sha256 -cne $gh.sha256" in contract


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_regular_file_validation_rejects_hardlinks(tmp_path: Path) -> None:
    original = tmp_path / "python.exe"
    linked = tmp_path / "linked-python.exe"
    original.write_bytes(b"safe-looking")
    try:
        os.link(original, linked)
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error}")
    command = rf"""
. '{SECURITY}'
$rejected = $false
try {{ Resolve-RegularFile '{original}' '' | Out-Null }} catch {{ $rejected = $true }}
if (-not $rejected) {{ exit 60 }}
"""
    result = run_ps(command)
    assert result.returncode == 0, result.stderr + result.stdout


def test_alternate_self_consistent_codex_package_root_is_rejected(tmp_path: Path) -> None:
    fake = tmp_path / "node_modules" / "@openai" / "codex"
    fake.mkdir(parents=True)
    (fake / "package.json").write_text("{}", encoding="utf-8")
    command = rf"""
. '{SECURITY}'
$rejected = $false
try {{ Resolve-CodexIdentityAtRoot '{tmp_path / 'node_modules'}' | Out-Null }}
catch {{ $rejected = $true }}
if (-not $rejected) {{ exit 61 }}
"""
    result = run_ps(command)
    assert result.returncode == 0, result.stderr + result.stdout


def test_module_manifest_detects_added_replaced_and_missing_files(tmp_path: Path) -> None:
    module = tmp_path / "change_delivery"
    module.mkdir()
    first = module / "first.py"
    second = module / "second.py"
    first.write_text("ONE = 1\n", encoding="utf-8")
    second.write_text("TWO = 2\n", encoding="utf-8")
    command = rf"""
. '{SECURITY}'
$expected = Get-ModuleTreeManifest '{module}'
Set-Content -LiteralPath '{first}' -Value 'ONE = 99'
$replaceRejected = $false
try {{ Assert-ModuleTreeManifest '{module}' $expected }} catch {{ $replaceRejected = $true }}
if (-not $replaceRejected) {{ exit 62 }}
Set-Content -LiteralPath '{first}' -Value 'ONE = 1'
Set-Content -LiteralPath (Join-Path '{module}' 'added.py') -Value 'ADDED = 1'
$addRejected = $false
try {{ Assert-ModuleTreeManifest '{module}' $expected }} catch {{ $addRejected = $true }}
if (-not $addRejected) {{ exit 63 }}
Remove-Item -LiteralPath (Join-Path '{module}' 'added.py')
Remove-Item -LiteralPath '{second}'
$missingRejected = $false
try {{ Assert-ModuleTreeManifest '{module}' $expected }} catch {{ $missingRejected = $true }}
if (-not $missingRejected) {{ exit 64 }}
"""
    result = run_ps(command)
    assert result.returncode == 0, result.stderr + result.stdout


def test_checked_in_module_manifest_matches_the_runtime_source_tree() -> None:
    command = rf"""
. '{SECURITY}'
$expected = @(Read-TrustedModuleManifest)
Assert-ModuleTreeManifest '{ROOT / "src" / "change_delivery"}' $expected
"""

    result = run_ps(command)

    assert result.returncode == 0, result.stderr + result.stdout


def test_stable_trusted_file_read_fails_closed_on_replacement_race(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "trusted-module-manifest.json"
    replacement = tmp_path / "replacement.json"
    parked = tmp_path / "parked.json"
    manifest.write_text('[{"trusted":true}]', encoding="utf-8")
    replacement.write_text('[{"trusted":false}]', encoding="utf-8")
    digest = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"(Get-FileHash -LiteralPath '{manifest}' -Algorithm SHA256).Hash.ToLowerInvariant()",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    command = rf"""
. '{SECURITY}'
$script:StableManifestReadProbe = {{
  param($lockedPath)
  Move-Item -LiteralPath $lockedPath -Destination '{parked}' -ErrorAction Stop
  Move-Item -LiteralPath '{replacement}' -Destination $lockedPath -ErrorAction Stop
}}
$rejected = $false
try {{
  Read-StableUtf8File '{manifest}' '{digest}' 'trusted-module-manifest.json' | Out-Null
}}
catch {{ $rejected = $true }}
if (-not $rejected) {{ exit 71 }}
if (-not (Test-Path -LiteralPath '{manifest}' -PathType Leaf)) {{ exit 72 }}
if (Test-Path -LiteralPath '{parked}') {{ exit 73 }}
"""

    result = run_ps(command)

    assert result.returncode == 0, result.stderr + result.stdout


def test_module_tree_rejects_python_bytecode_and_cache_directories(
    tmp_path: Path,
) -> None:
    module = tmp_path / "change_delivery"
    module.mkdir()
    (module / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    cache = module / "__pycache__"
    cache.mkdir()
    (cache / "module.cpython-312.pyc").write_bytes(b"forged-bytecode")
    command = rf"""
. '{SECURITY}'
$command = Get-Command Assert-NoPythonBytecode -ErrorAction SilentlyContinue
if ($null -eq $command -or $command.CommandType -ne 'Function') {{ exit 65 }}
$rejected = $false
try {{ Assert-NoPythonBytecode '{module}' }} catch {{ $rejected = $true }}
if (-not $rejected) {{ exit 66 }}
Remove-Item -LiteralPath '{cache}' -Recurse
[IO.File]::WriteAllBytes((Join-Path '{module}' 'standalone.pyc'), [byte[]](1,2,3))
$standaloneRejected = $false
try {{ Assert-NoPythonBytecode '{module}' }} catch {{ $standaloneRejected = $true }}
if (-not $standaloneRejected) {{ exit 67 }}
"""
    result = run_ps(command)
    assert result.returncode == 0, result.stderr + result.stdout


def test_closed_module_root_accepts_exact_manifest_tree(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    package = source_root / "change_delivery"
    adapters = package / "adapters"
    adapters.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (adapters / "__init__.py").write_text("\n", encoding="utf-8")
    command = rf"""
. '{SECURITY}'
$contract = Get-Command Assert-ClosedPythonModuleRoot -ErrorAction SilentlyContinue
if ($null -eq $contract -or $contract.CommandType -ne 'Function') {{ exit 68 }}
$expected = Get-ModuleTreeManifest '{package}'
Assert-ClosedPythonModuleRoot '{source_root}' $expected
"""

    result = run_ps(command)

    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.parametrize(
    ("relative_path", "directory"),
    (
        ("sqlite3.py", False),
        ("change_delivery/evil.pyd", False),
        ("change_delivery/extra", True),
    ),
)
def test_closed_module_root_rejects_unmanifested_import_entries(
    tmp_path: Path,
    relative_path: str,
    directory: bool,
) -> None:
    source_root = tmp_path / "src"
    package = source_root / "change_delivery"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    attack = source_root / relative_path
    if directory:
        attack.mkdir(parents=True)
    else:
        attack.parent.mkdir(parents=True, exist_ok=True)
        attack.write_bytes(b"attacker-controlled")
    command = rf"""
. '{SECURITY}'
$contract = Get-Command Assert-ClosedPythonModuleRoot -ErrorAction SilentlyContinue
if ($null -eq $contract -or $contract.CommandType -ne 'Function') {{ exit 69 }}
$expected = Get-ModuleTreeManifest '{package}'
$rejected = $false
try {{ Assert-ClosedPythonModuleRoot '{source_root}' $expected }}
catch {{ $rejected = $true }}
if (-not $rejected) {{ exit 70 }}
"""

    result = run_ps(command)

    assert result.returncode == 0, result.stderr + result.stdout


def test_pinned_module_archive_ignores_source_tree_insertion_after_final_check(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src"
    package = source_root / "change_delivery"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("\n", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import sqlite3\n"
        "assert not hasattr(sqlite3, 'PWNED')\n"
        "print('SAFE_ARCHIVE')\n",
        encoding="utf-8",
    )
    archive = tmp_path / "trusted.zip"
    command = rf"""
. '{SECURITY}'
$expected = Get-ModuleTreeManifest '{package}'
Assert-ClosedPythonModuleRoot '{source_root}' $expected
$streams = @()
$bundle = $null
try {{
  foreach ($entry in $expected) {{
    $candidate = Join-Path '{package}' ([string] $entry.relative_path)
    $streams += [IO.File]::Open(
      $candidate, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
    )
  }}
  $bundle = New-TrustedModuleArchive '{archive}' $streams $expected
  Set-Content -LiteralPath (Join-Path '{source_root}' 'sqlite3.py') -Value 'PWNED = True'
  [IO.File]::WriteAllBytes(
    (Join-Path '{package}' 'evil.pyd'), [byte[]](1,2,3)
  )
  New-Item -ItemType Directory -Path (Join-Path '{package}' 'extra') | Out-Null
  $bootstrap = "import runpy,sys;sys.dont_write_bytecode=True;" +
    "sys.implementation.cache_tag=None;archive=sys.argv.pop(1);" +
    "sys.path.insert(0,archive);" +
    "runpy.run_module('change_delivery',run_name='__main__')"
  $output = & 'C:\softs\python-3.12.10\python.exe' -I -B -c $bootstrap '{archive}'
  if ($LASTEXITCODE -ne 0 -or ($output -join '') -cne 'SAFE_ARCHIVE') {{ exit 74 }}
}}
finally {{
  if ($null -ne $bundle) {{ $bundle.stream.Dispose() }}
  foreach ($stream in $streams) {{ $stream.Dispose() }}
}}
"""

    result = run_ps(command)

    assert result.returncode == 0, result.stderr + result.stdout


def test_tick_pins_helper_and_holds_all_module_handles() -> None:
    tick = TICK.read_text(encoding="utf-8")
    scheduled = SCHEDULED.read_text(encoding="utf-8")

    assert "TrustedHostSecuritySha256" in tick
    assert "Open-StableTrustedHelper" in tick
    assert "module_manifest" in tick
    assert "Open-ModuleTreeHandles" in tick
    assert "Assert-NoPythonBytecode" in tick
    assert "$env:PYTHONDONTWRITEBYTECODE = '1'" in tick
    assert "& $pythonPath -I -B" in tick
    assert "sys.implementation.cache_tag=None" in tick
    assert "Assert-NoPythonBytecode" in scheduled
    assert scheduled.count("Assert-NoPythonBytecode") >= 2
    assert "Assert-ClosedPythonModuleRoot" in tick
    assert "Assert-ClosedPythonModuleRoot" in scheduled
    assert "New-TrustedModuleArchive" in tick
    assert "& $pythonPath -I -B -c $bootstrap $moduleArchivePath" in tick
    assert "sys.path.insert(0,archive)" in tick
    assert "sys.path.insert(0,root)" not in tick
