from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[2]
SHARED = ROOT / "scripts" / "install-shared-plugin.ps1"
PERSONAL = ROOT / "scripts" / "install-personal-plugin.ps1"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def test_shared_installer_is_portable_and_uses_all_publishers() -> None:
    script = text(SHARED)

    assert "C:\\Users\\JoySun" not in script
    assert "shared-layout.ps1" in script
    assert "runtime-publisher.ps1" in script
    assert "plugin-publisher.ps1" in script
    assert "host-security.ps1" in script
    assert "Get-SharedInstallLayout" in script
    assert "Install-PrivateVirtualEnvironment" in script
    assert "New-CleanRuntimeHostStage" in script
    assert "Install-CleanRuntimeHostStage" in script
    assert "Write-StableLauncher" in script
    assert "New-CleanPluginStage" in script
    assert "Install-CleanPluginStage" in script
    assert "validate_plugin.py" in script
    assert "plugin marketplace list --json" in script
    assert "plugin marketplace remove $marketplaceName --json" in script
    assert "plugin marketplace add $marketplaceSource --ref $releaseTag --json" in script
    assert "plugin add" in script
    assert "$codexPath = (Resolve-OfficialCodexIdentity).path" in script
    assert "& codex " not in script
    assert "[string] $sourceManifest.version -cne $releaseVersion" in script
    assert script.index("$sourceManifest = ConvertFrom-Json") < script.index(
        "New-CleanRuntimeHostStage"
    )
    assert "$marketplaceRollbackRoot" in script
    assert "Restore-MarketplaceSnapshot" in script
    assert "Remove-NewMarketplaceInstallation" in script
    assert "catch" in script


def test_shared_installer_resolves_default_source_after_parameter_binding() -> None:
    script = text(SHARED)

    assert "[string] $SourceRoot = (Split-Path -Parent $PSScriptRoot)" not in script
    assert "if ([string]::IsNullOrWhiteSpace($SourceRoot))" in script
    assert "$SourceRoot = Split-Path -Parent $PSScriptRoot" in script


def test_personal_installer_is_only_a_compatibility_wrapper() -> None:
    script = text(PERSONAL)

    assert "install-shared-plugin.ps1" in script
    assert "@PSBoundParameters" in script
    assert "C:\\Users\\JoySun" not in script
    assert "New-CleanPluginStage" not in script


def test_new_install_scripts_parse_as_powershell() -> None:
    paths = (
        ROOT / "scripts" / "shared-layout.ps1",
        ROOT / "scripts" / "runtime-publisher.ps1",
        SHARED,
        PERSONAL,
    )
    quoted = ", ".join(f"'{path}'" for path in paths)
    command = (
        f"$files=@({quoted}); foreach($file in $files){{"
        "[void][scriptblock]::Create("
        "[IO.File]::ReadAllText($file,[Text.UTF8Encoding]::new($false,$true)))"
        "}"
    )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
