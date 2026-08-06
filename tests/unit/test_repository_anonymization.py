from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_tracked_repository_contains_no_project_brand_identifier() -> None:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    tracked = [Path(item) for item in result.stdout.decode().split("\0") if item]
    forbidden = "".join(("mail", "blink"))

    for relative in tracked:
        if not (ROOT / relative).exists():
            continue
        assert forbidden not in str(relative).casefold()
        path = ROOT / relative
        if path.suffix.casefold() in {
            ".md",
            ".json",
            ".py",
            ".ps1",
            ".toml",
            ".yaml",
            ".yml",
        }:
            assert forbidden not in path.read_text(encoding="utf-8").casefold(), relative
