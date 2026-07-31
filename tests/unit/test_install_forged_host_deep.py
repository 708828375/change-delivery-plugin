from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[2]
SECURITY = ROOT / "scripts" / "host-security.ps1"


def test_self_consistent_host_cannot_replace_fixed_python_or_codex() -> None:
    command = rf"""
. '{SECURITY}'
$moduleRoot = (Resolve-Path '{ROOT / 'src'}').ProviderPath
$manifest = @(Read-TrustedModuleManifest)
$python = Resolve-TrustedPythonIdentity
$codex = Resolve-OfficialCodexIdentity
$config = [PSCustomObject][ordered]@{{
  schema_version=1; python_path='C:\fake\python.exe'; python_sha256=('a'*64)
  module_root=$moduleRoot; codex_path=$codex.path; codex_sha256=$codex.sha256
  codex_package_root=$codex.package_root; codex_parent_version=$codex.parent_version
  codex_platform_dependency=$codex.platform_dependency
  codex_platform_package_name=$codex.platform_package_name
  codex_platform_version=$codex.platform_version; module_manifest=$manifest
  auth_mode='api_key'; auth_source=$null
}}
$pythonRejected = $false
try {{ Assert-StrictRuntimeHost $config '{ROOT}' | Out-Null }} catch {{ $pythonRejected = $true }}
if (-not $pythonRejected) {{ exit 80 }}
$config.python_path=$python.path; $config.python_sha256=$python.sha256
$config.codex_path='C:\fake\codex.exe'; $config.codex_sha256=('b'*64)
$codexRejected = $false
try {{ Assert-StrictRuntimeHost $config '{ROOT}' | Out-Null }} catch {{ $codexRejected = $true }}
if (-not $codexRejected) {{ exit 81 }}
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
