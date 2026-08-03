import os
from pathlib import Path
import subprocess
import sys

import pytest

from change_delivery.cli import CliServices, CliUsageError, _github_fixed_environment


def test_python_module_help_is_available() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "change_delivery", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Change Delivery" in result.stdout


def test_github_config_is_pinned_under_trusted_userprofile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "profile"
    appdata = profile / "AppData" / "Roaming"
    config = appdata / "GitHub CLI"
    config.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("APPDATA", str(appdata))

    assert _github_fixed_environment(("GH_CONFIG_DIR",)) == {
        "GH_CONFIG_DIR": str(config.resolve())
    }
    assert _github_fixed_environment(("GH_TOKEN",)) == {}


def test_github_config_rejects_appdata_outside_userprofile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "profile"
    (profile / "AppData" / "Roaming" / "GitHub CLI").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("APPDATA", str(outside))

    with pytest.raises(CliUsageError, match="APPDATA"):
        _github_fixed_environment(("GH_CONFIG_DIR",))


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_cli_services_rejects_real_root_junction(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "root-junction"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        with pytest.raises(ValueError, match="ancestry"):
            CliServices(plugin_root=junction)
    finally:
        os.rmdir(junction)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_cli_services_rejects_real_parent_junction(tmp_path: Path) -> None:
    target = tmp_path / "target-parent"
    runtime = target / "runtime"
    runtime.mkdir(parents=True)
    junction = tmp_path / "parent-junction"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        with pytest.raises(ValueError, match="ancestry"):
            CliServices(plugin_root=junction / "runtime")
    finally:
        os.rmdir(junction)
