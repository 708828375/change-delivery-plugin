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
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
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


def test_fixed_codex_and_python_anchors_do_not_use_path_or_overrides() -> None:
    contract = SECURITY.read_text(encoding="utf-8")
    scheduled = SCHEDULED.read_text(encoding="utf-8")

    assert "C:\\softs\\nodejs\\node_global\\node_modules\\@openai\\codex" in contract
    assert "C:\\softs\\python-3.12.10\\python.exe" in contract
    assert "Get-Command npm" not in contract
    assert "Get-Command python" not in contract
    assert "npm root" not in contract
    assert "NpmGlobalRoot" not in scheduled
    assert "PythonPath" not in scheduled


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
