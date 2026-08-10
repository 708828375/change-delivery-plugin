from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[2]
PUBLISHER = ROOT / "scripts" / "runtime-publisher.ps1"
EXPECTED_SKILLS = (
    "change-delivery",
    "delivery-commit",
    "delivery-implement",
    "delivery-local-validation",
    "delivery-planning",
    "delivery-pr",
    "delivery-ready-check",
    "delivery-review",
    "delivery-review-fix",
    "delivery-test-deploy",
    "project-test-acceptance",
    "production-release",
)


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


def ps_literal(path: Path) -> str:
    return str(path).replace("'", "''")


def make_runtime_source(root: Path, value: str = "new") -> Path:
    source = root / "source"
    (source / "src" / "change_delivery").mkdir(parents=True)
    (source / "schemas").mkdir()
    (source / "scripts").mkdir()
    for skill in EXPECTED_SKILLS:
        skill_root = source / "plugin" / "change-delivery-plugin" / "skills" / skill
        skill_root.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text(
            f"---\nname: {skill}\n---\n",
            encoding="utf-8",
        )
    review_references = (
        source
        / "plugin"
        / "change-delivery-plugin"
        / "skills"
        / "delivery-review"
        / "references"
    )
    review_references.mkdir()
    (review_references / "team-review.md").write_text(
        f"review-reference:{value}", encoding="utf-8"
    )
    (source / "src" / "change_delivery" / "__init__.py").write_text(
        f"VALUE = {value!r}\n", encoding="utf-8"
    )
    (source / "schemas" / "project.schema.json").write_text(
        "{}", encoding="utf-8"
    )
    (source / "scripts" / "run-tick.ps1").write_text(
        "Write-Output tick\n", encoding="utf-8"
    )
    (source / "pyproject.toml").write_text(
        "[project]\nname='change-delivery'\nversion='0.2.0'\n",
        encoding="utf-8",
    )
    return source


def test_runtime_stage_contains_only_the_closed_host_tree(tmp_path: Path) -> None:
    source = make_runtime_source(tmp_path)
    (source / ".git").mkdir()
    (source / ".pytest_cache").mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir()

    result = run_ps(
        rf"""
$ErrorActionPreference='Stop'
. '{ps_literal(PUBLISHER)}'
$stage = New-CleanRuntimeHostStage '{ps_literal(source)}' '{ps_literal(data_root)}'
$names = @((Get-ChildItem -LiteralPath $stage -Force).Name | Sort-Object)
if (($names -join ',') -cne 'pyproject.toml,schemas,scripts,skills,src') {{ exit 51 }}
if (-not (Test-Path -LiteralPath (Join-Path $stage 'skills\delivery-review\SKILL.md') -PathType Leaf)) {{ exit 56 }}
Assert-ClosedRuntimeHostTree $stage
"""
    )

    assert result.returncode == 0, result.stderr + result.stdout

def test_runtime_publish_atomically_replaces_a_managed_host(tmp_path: Path) -> None:
    source = make_runtime_source(tmp_path, "new")
    data_root = tmp_path / "data"
    data_root.mkdir()
    host = data_root / "host"

    first = run_ps(
        rf"""
$ErrorActionPreference='Stop'
. '{ps_literal(PUBLISHER)}'
$stage = New-CleanRuntimeHostStage '{ps_literal(source)}' '{ps_literal(data_root)}'
Install-CleanRuntimeHostStage $stage '{ps_literal(host)}'
"""
    )
    assert first.returncode == 0, first.stderr + first.stdout

    (source / "src" / "change_delivery" / "__init__.py").write_text(
        "VALUE = 'updated'\n", encoding="utf-8"
    )
    second = run_ps(
        rf"""
$ErrorActionPreference='Stop'
. '{ps_literal(PUBLISHER)}'
$stage = New-CleanRuntimeHostStage '{ps_literal(source)}' '{ps_literal(data_root)}'
Install-CleanRuntimeHostStage $stage '{ps_literal(host)}'
"""
    )

    assert second.returncode == 0, second.stderr + second.stdout
    assert "updated" in (host / "src" / "change_delivery" / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert not list(data_root.glob(".host.backup-*"))
    assert {
        path.name
        for path in (host / "skills").iterdir()
        if (path / "SKILL.md").is_file()
    } == set(EXPECTED_SKILLS)
    assert not list(host.rglob("__pycache__"))
    assert not list(host.rglob("*.pyc"))
    assert (
        host / "skills" / "delivery-review" / "references" / "team-review.md"
    ).read_text(encoding="utf-8") == "review-reference:new"


def test_runtime_publish_migrates_the_v020_host_layout(tmp_path: Path) -> None:
    source = make_runtime_source(tmp_path)
    data_root = tmp_path / "data"
    data_root.mkdir()
    host = data_root / "host"
    host.mkdir()
    for directory in ("schemas", "scripts", "src"):
        (host / directory).mkdir()
    (host / "schemas" / "project.schema.json").write_text("{}", encoding="utf-8")
    (host / "scripts" / "run-tick.ps1").write_text("Write-Output tick\n", encoding="utf-8")
    (host / "src" / "change_delivery").mkdir()
    (host / "src" / "change_delivery" / "__init__.py").write_text(
        "VALUE = 'legacy'\n", encoding="utf-8"
    )
    (host / "pyproject.toml").write_text(
        "[project]\nname='change-delivery'\nversion='0.2.0'\n", encoding="utf-8"
    )

    result = run_ps(
        rf"""
$ErrorActionPreference='Stop'
. '{ps_literal(PUBLISHER)}'
$stage = New-CleanRuntimeHostStage '{ps_literal(source)}' '{ps_literal(data_root)}'
Install-CleanRuntimeHostStage $stage '{ps_literal(host)}'
"""
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert (host / "skills" / "delivery-review" / "SKILL.md").is_file()
    assert "new" in (host / "src" / "change_delivery" / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert not list(data_root.glob(".host.backup-*"))


def test_unmanaged_host_is_preserved(tmp_path: Path) -> None:
    source = make_runtime_source(tmp_path)
    data_root = tmp_path / "data"
    data_root.mkdir()
    host = data_root / "host"
    host.mkdir()
    sentinel = host / "user-file.txt"
    sentinel.write_text("preserve", encoding="utf-8")

    result = run_ps(
        rf"""
$ErrorActionPreference='Stop'
. '{ps_literal(PUBLISHER)}'
$stage = New-CleanRuntimeHostStage '{ps_literal(source)}' '{ps_literal(data_root)}'
try {{ Install-CleanRuntimeHostStage $stage '{ps_literal(host)}' }} catch {{ exit 52 }}
exit 53
"""
    )

    assert result.returncode == 52, result.stderr + result.stdout
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_runtime_stage_rejects_python_bytecode_and_cleans_stage(tmp_path: Path) -> None:
    source = make_runtime_source(tmp_path)
    cache = source / "src" / "change_delivery" / "__pycache__"
    cache.mkdir()
    (cache / "forged.pyc").write_bytes(b"forged")
    data_root = tmp_path / "data"
    data_root.mkdir()

    result = run_ps(
        rf"""
$ErrorActionPreference='Stop'
. '{ps_literal(PUBLISHER)}'
$rejected=$false
try {{ New-CleanRuntimeHostStage '{ps_literal(source)}' '{ps_literal(data_root)}' }}
catch {{ $rejected=$true }}
if (-not $rejected) {{ exit 54 }}
if (@(Get-ChildItem -LiteralPath '{ps_literal(data_root)}' -Force).Count -ne 0) {{ exit 55 }}
"""
    )

    assert result.returncode == 0, result.stderr + result.stdout


def test_launcher_uses_only_the_private_venv_python(tmp_path: Path) -> None:
    launcher = tmp_path / "bin" / "change-delivery.ps1"
    venv = tmp_path / "runtime" / "venv"

    result = run_ps(
        rf"""
$ErrorActionPreference='Stop'
. '{ps_literal(PUBLISHER)}'
Write-StableLauncher '{ps_literal(launcher)}' '{ps_literal(venv)}'
"""
    )

    assert result.returncode == 0, result.stderr + result.stdout
    script = launcher.read_text(encoding="utf-8-sig")
    assert str(venv / "Scripts" / "python.exe") in script
    assert "-m change_delivery @args" in script
    assert "Get-Command python" not in script


def test_venv_and_launcher_reject_existing_link_trees_before_replacement() -> None:
    publisher = PUBLISHER.read_text(encoding="utf-8-sig")

    assert "function Assert-ManagedVirtualEnvironment" in publisher
    assert "Assert-ManagedVirtualEnvironment $target" in publisher
    assert "Assert-ManagedVirtualEnvironment $stage" in publisher
    assert "Assert-OrdinaryRuntimeFile $launcher" in publisher
