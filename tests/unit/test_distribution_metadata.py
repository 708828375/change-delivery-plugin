from __future__ import annotations

import json
import re
import tomllib
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_marketplace_is_a_versioned_git_catalog() -> None:
    marketplace = json.loads(
        (ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8")
    )

    assert marketplace["name"] == "joysun-team"
    assert marketplace["interface"]["displayName"] == "JoySun Team Plugins"
    assert len(marketplace["plugins"]) == 1
    plugin = marketplace["plugins"][0]
    assert plugin == {
        "name": "change-delivery-plugin",
        "source": {
            "source": "local",
            "path": "./plugin/change-delivery-plugin",
        },
        "policy": {
            "installation": "AVAILABLE",
            "authentication": "ON_INSTALL",
        },
        "category": "Productivity",
    }

    installer = (ROOT / "scripts/install-shared-plugin.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "708828375/change-delivery-plugin" in installer
    assert "--ref $releaseTag" in installer
    assert "$releaseTag = 'v0.2.1'" in installer


def test_distribution_versions_are_consistent() -> None:
    manifest = json.loads(
        (ROOT / "plugin/change-delivery-plugin/.codex-plugin/plugin.json").read_text(
            encoding="utf-8-sig"
        )
    )
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]

    assert manifest["version"] == "0.2.1"
    assert project["version"] == "0.2.1"
    module = ast.parse(
        (ROOT / "src/change_delivery/__init__.py").read_text(encoding="utf-8")
    )
    version_assignment = next(
        node
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        )
    )
    assert isinstance(version_assignment.value, ast.Constant)
    assert version_assignment.value.value == "0.2.1"
    assert manifest["license"] == "UNLICENSED"


def test_readme_documents_the_shared_lifecycle() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for required in (
        "Python 3.12",
        "安装",
        "升级",
        "诊断",
        "$change-delivery",
        "卸载",
        "新建 Codex CLI 会话",
        "仅限获授权团队成员",
        "gh auth login",
        "v0.2.1",
    ):
        assert required in readme
    assert re.search(r"install-shared-plugin\.ps1", readme)
    assert re.search(r"uninstall-shared-plugin\.ps1", readme)
