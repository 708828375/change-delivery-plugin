from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


SECURITY = Path(__file__).parents[2] / "scripts" / "host-security.ps1"


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_module_manifest_rejects_hardlinked_python_file(tmp_path: Path) -> None:
    module = tmp_path / "change_delivery"
    module.mkdir()
    original = module / "original.py"
    linked = module / "linked.py"
    original.write_text("VALUE = 1\n", encoding="utf-8")
    try:
        os.link(original, linked)
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error}")

    command = rf"""
. '{SECURITY}'
$rejected = $false
try {{ Get-ModuleTreeManifest '{module}' | Out-Null }} catch {{ $rejected = $true }}
if (-not $rejected) {{ exit 70 }}
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
