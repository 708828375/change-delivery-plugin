from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[2]
PUBLISHER = ROOT / "scripts" / "plugin-publisher.ps1"


def run_ps(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_clean_stage_contains_only_plugin_runtime_entries(tmp_path: Path) -> None:
    source = tmp_path / "source"
    plugins = tmp_path / "plugins"
    (source / ".codex-plugin").mkdir(parents=True)
    (source / "skills" / "change-delivery").mkdir(parents=True)
    (source / ".codex-plugin" / "plugin.json").write_text(
        '{"name":"change-delivery-plugin"}', encoding="utf-8"
    )
    (source / "skills" / "change-delivery" / "SKILL.md").write_text(
        "skill", encoding="utf-8"
    )
    (source / ".git").mkdir()
    (source / ".pytest-denied").mkdir()
    plugins.mkdir()

    command = rf"""
$ErrorActionPreference = 'Stop'
. '{PUBLISHER}'
$stage = New-CleanPluginStage '{source}' '{plugins}'
$names = @((Get-ChildItem -LiteralPath $stage -Force).Name | Sort-Object)
if (($names -join ',') -cne '.codex-plugin,skills') {{ exit 31 }}
if (Test-Path -LiteralPath (Join-Path $stage '.git')) {{ exit 32 }}
if (Test-Path -LiteralPath (Join-Path $stage '.pytest-denied')) {{ exit 33 }}
Assert-ClosedPluginReleaseTree $stage
"""
    result = run_ps(command)

    assert result.returncode == 0, result.stderr + result.stdout
