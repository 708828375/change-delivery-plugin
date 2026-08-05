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


def make_plugin_source(root: Path, skill_text: str = "new skill") -> Path:
    source = root / "source"
    (source / ".codex-plugin").mkdir(parents=True)
    (source / "skills" / "change-delivery").mkdir(parents=True)
    (source / ".codex-plugin" / "plugin.json").write_text(
        '{"name":"change-delivery-plugin"}', encoding="utf-8"
    )
    (source / "skills" / "change-delivery" / "SKILL.md").write_text(
        skill_text, encoding="utf-8"
    )
    return source


def test_clean_stage_contains_only_plugin_runtime_entries(tmp_path: Path) -> None:
    source = make_plugin_source(tmp_path, "skill")
    plugins = tmp_path / "plugins"
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


def test_first_publish_installs_an_ordinary_plugin_directory(tmp_path: Path) -> None:
    source = make_plugin_source(tmp_path)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    target = plugins / "change-delivery-plugin"
    command = rf"""
$ErrorActionPreference = 'Stop'
. '{PUBLISHER}'
$stage = New-CleanPluginStage '{source}' '{plugins}'
Install-CleanPluginStage $stage '{target}' '{source}'
"""

    result = run_ps(command)

    assert result.returncode == 0, result.stderr + result.stdout
    assert target.is_dir()
    assert not target.is_junction()
    assert sorted(item.name for item in target.iterdir()) == [
        ".codex-plugin",
        "skills",
    ]


def test_regular_update_replaces_the_managed_plugin_tree(tmp_path: Path) -> None:
    source = make_plugin_source(tmp_path, "new skill")
    plugins = tmp_path / "plugins"
    target = plugins / "change-delivery-plugin"
    (target / ".codex-plugin").mkdir(parents=True)
    (target / "skills" / "change-delivery").mkdir(parents=True)
    (target / ".codex-plugin" / "plugin.json").write_text(
        '{"name":"change-delivery-plugin"}', encoding="utf-8"
    )
    old_skill = target / "skills" / "change-delivery" / "old.txt"
    old_skill.write_text("old", encoding="utf-8")
    command = rf"""
$ErrorActionPreference = 'Stop'
. '{PUBLISHER}'
$stage = New-CleanPluginStage '{source}' '{plugins}'
Install-CleanPluginStage $stage '{target}' '{source}'
"""

    result = run_ps(command)

    assert result.returncode == 0, result.stderr + result.stdout
    assert not old_skill.exists()
    assert (target / "skills" / "change-delivery" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "new skill"


def test_junction_migration_preserves_the_development_source(tmp_path: Path) -> None:
    source = make_plugin_source(tmp_path)
    sentinel = source / "source-sentinel.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    target = plugins / "change-delivery-plugin"
    command = rf"""
$ErrorActionPreference = 'Stop'
. '{PUBLISHER}'
New-Item -ItemType Junction -Path '{target}' -Target '{source}' | Out-Null
$stage = New-CleanPluginStage '{source}' '{plugins}'
Install-CleanPluginStage $stage '{target}' '{source}'
"""

    result = run_ps(command)

    assert result.returncode == 0, result.stderr + result.stdout
    assert source.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert target.is_dir()
    assert not target.is_junction()


def test_junction_to_an_unexpected_source_is_preserved(tmp_path: Path) -> None:
    source = make_plugin_source(tmp_path)
    unexpected = tmp_path / "unexpected"
    unexpected.mkdir()
    sentinel = unexpected / "sentinel.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    target = plugins / "change-delivery-plugin"
    command = rf"""
$ErrorActionPreference = 'Stop'
. '{PUBLISHER}'
New-Item -ItemType Junction -Path '{target}' -Target '{unexpected}' | Out-Null
$stage = New-CleanPluginStage '{source}' '{plugins}'
try {{ Install-CleanPluginStage $stage '{target}' '{source}' }}
catch {{ exit 44 }}
exit 45
"""

    result = run_ps(command)

    assert result.returncode == 44, result.stderr + result.stdout
    assert target.is_junction()
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_plugin_named_as_a_different_product_is_preserved(tmp_path: Path) -> None:
    source = make_plugin_source(tmp_path)
    plugins = tmp_path / "plugins"
    target = plugins / "change-delivery-plugin"
    (target / ".codex-plugin").mkdir(parents=True)
    (target / "skills").mkdir()
    (target / ".codex-plugin" / "plugin.json").write_text(
        '{"name":"different-plugin"}', encoding="utf-8"
    )
    sentinel = target / "skills" / "sentinel.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    command = rf"""
$ErrorActionPreference = 'Stop'
. '{PUBLISHER}'
$stage = New-CleanPluginStage '{source}' '{plugins}'
try {{ Install-CleanPluginStage $stage '{target}' '{source}' }}
catch {{ exit 46 }}
exit 47
"""

    result = run_ps(command)

    assert result.returncode == 46, result.stderr + result.stdout
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_plugin_tree_with_an_extra_entry_is_preserved(tmp_path: Path) -> None:
    source = make_plugin_source(tmp_path)
    plugins = tmp_path / "plugins"
    target = plugins / "change-delivery-plugin"
    (target / ".codex-plugin").mkdir(parents=True)
    (target / "skills").mkdir()
    (target / ".codex-plugin" / "plugin.json").write_text(
        '{"name":"change-delivery-plugin"}', encoding="utf-8"
    )
    extra = target / "user-data.txt"
    extra.write_text("preserve", encoding="utf-8")
    command = rf"""
$ErrorActionPreference = 'Stop'
. '{PUBLISHER}'
$stage = New-CleanPluginStage '{source}' '{plugins}'
try {{ Install-CleanPluginStage $stage '{target}' '{source}' }}
catch {{ exit 48 }}
exit 49
"""

    result = run_ps(command)

    assert result.returncode == 48, result.stderr + result.stdout
    assert extra.read_text(encoding="utf-8") == "preserve"


def test_plugin_tree_with_a_nested_junction_is_preserved(tmp_path: Path) -> None:
    source = make_plugin_source(tmp_path)
    plugins = tmp_path / "plugins"
    target = plugins / "change-delivery-plugin"
    external = tmp_path / "external"
    (target / ".codex-plugin").mkdir(parents=True)
    (target / "skills").mkdir()
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    (target / ".codex-plugin" / "plugin.json").write_text(
        '{"name":"change-delivery-plugin"}', encoding="utf-8"
    )
    nested = target / "skills" / "linked"
    command = rf"""
$ErrorActionPreference = 'Stop'
. '{PUBLISHER}'
New-Item -ItemType Junction -Path '{nested}' -Target '{external}' | Out-Null
$stage = New-CleanPluginStage '{source}' '{plugins}'
try {{ Install-CleanPluginStage $stage '{target}' '{source}' }}
catch {{ exit 50 }}
exit 51
"""

    result = run_ps(command)

    assert result.returncode == 50, result.stderr + result.stdout
    assert nested.is_junction()
    assert sentinel.read_text(encoding="utf-8") == "preserve"
