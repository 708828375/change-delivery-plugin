"""Concrete local runtime ports used by the default CLI process."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from uuid import uuid4

from change_delivery.adapters.codex import CodexAdapter, StageExecutionRequest
from change_delivery.adapters.git import GitAdapter
from change_delivery.domain import (
    ProjectConfig,
    RunRecord,
    StageResult,
    stage_result_schema_json,
)
from change_delivery.process import ProcessRunner
from change_delivery.repository import RunRepository


_STAGE_SKILLS = {
    "design": "writing-plans",
    "implementation": "delivery-implement",
    "internal_review": "delivery-review",
    "review_fix": "delivery-implement",
    "acceptance": "project-test-acceptance",
}


def environment_secrets(names: tuple[str, ...]) -> dict[str, str]:
    return {
        name: value
        for name in names
        if (value := os.environ.get(name)) is not None
    }


class RuntimeStageExecutor:
    """Run controlled Codex stages and register host-observed evidence."""

    def __init__(
        self,
        repository: RunRepository,
        plugin_root: Path,
        *,
        runner: ProcessRunner | None = None,
        codex_factory: object | None = None,
    ) -> None:
        self._repository = repository
        self._plugin_root = plugin_root.resolve()
        self._runner = runner or ProcessRunner(environment_secrets)
        self._codex_factory = codex_factory
        self._codex_adapter: CodexAdapter | None = None

    def execute(
        self,
        stage: str,
        run: RunRecord,
        config: ProjectConfig,
        run_root: Path,
        *,
        operation_identity: str | None = None,
        tested_tree_sha: str | None = None,
        checks_identity: str | None = None,
    ) -> StageResult:
        if stage == "local_validation":
            return self._run_local_validation(
                run,
                config,
                operation_identity,
                tested_tree_sha,
                checks_identity,
            )
        worktree = (
            run_root
            if stage == "design"
            else self._worktree(run.run_id)
        )
        request = StageExecutionRequest(
            run_id=run.run_id,
            stage=stage,
            instructions=(
                self._instructions(stage)
                + " Use only this non-authoritative placeholder evidence in "
                "the JSON result: evidence_id=untrusted-codex-output, "
                "kind=untrusted_stage_output, sha256="
                + ("0" * 64)
                + ". The host replaces it after verification."
            ),
            worktree=worktree,
            head_sha=run.head_sha or self._design_head(config),
            approval_scope=self._approval_scope(stage, run_root),
            automation_limit="Stop at READY_TO_MERGE; never merge or release.",
            allowed_actions=self._allowed_actions(stage),
            forbidden_actions=(
                "merge pull requests",
                "deploy production",
                "write databases",
                "expand approved scope",
            ),
            expected_skill=_STAGE_SKILLS[stage],
            result_schema_json=stage_result_schema_json(),
            status_policy=self._status_policy(stage),
            role=(
                "read_only"
                if stage == "internal_review"
                else "writer"
            ),
            attempt=1,
            timeout_seconds=config.codex_timeout_seconds,
        )
        result = self._codex().run_stage(request)
        kind = (
            (
                "internal_review_source"
                if stage == "internal_review"
                else f"{stage}_passed"
            )
            if result.status == "passed"
            else f"{stage}_result"
        )
        payload = {
            "sha": run.head_sha,
            "passed": result.status == "passed",
            "stage": stage,
            "result_sha256": hashlib.sha256(
                result.model_dump_json().encode("utf-8")
            ).hexdigest(),
        }
        if stage == "design" and result.status == "passed":
            payload["artifact_hashes"] = {
                name: hashlib.sha256((run_root / name).read_bytes()).hexdigest()
                for name in (
                    "requirement.md",
                    "design.md",
                    "implementation-plan.md",
                )
            }
        reference = self._repository.register_evidence(
            run.run_id,
            f"codex-stage:{run.run_id}:{stage}:{uuid4().hex}",
            kind,
            payload,
        )
        return result.model_copy(update={"evidence": (reference,)})

    def _run_local_validation(
        self,
        run: RunRecord,
        config: ProjectConfig,
        operation_identity: str | None,
        tested_tree_sha: str | None,
        checks_identity: str | None,
    ) -> StageResult:
        if (
            operation_identity is None
            or tested_tree_sha is None
            or checks_identity is None
        ):
            raise ValueError("local validation execution identity is incomplete")
        worktree = self._worktree(run.run_id)
        command_facts: list[dict[str, object]] = []
        passed = True
        for configured in config.required_pre_pr_checks:
            relative = configured.workdir.resolve().relative_to(
                config.repo_path.resolve()
            )
            workdir = (worktree / relative).resolve()
            workdir.relative_to(worktree)
            result = self._runner.run(
                configured.model_copy(update={"workdir": workdir})
            )
            command_facts.append(
                {
                    "command_id": result.command_id,
                    "exit_code": result.exit_code,
                }
            )
            if result.exit_code != 0:
                passed = False
                break
        payload = {
            "sha": run.head_sha,
            "passed": passed,
            "operation_identity": operation_identity,
            "tested_tree_sha": tested_tree_sha,
            "checks_identity": checks_identity,
            "commands": command_facts,
        }
        reference = self._repository.register_evidence(
            run.run_id,
            (
                f"local-validation:{run.run_id}:"
                f"{operation_identity.rsplit(':', 1)[-1]}"
            ),
            (
                "local_validation_passed"
                if passed
                else "local_validation_failed"
            ),
            payload,
        )
        return StageResult(
            status="passed" if passed else "failed",
            summary=(
                "required local checks passed"
                if passed
                else "a required local check failed"
            ),
            evidence=(reference,),
            findings=(),
            next_action="continue" if passed else "fix",
        )

    def _codex(self) -> CodexAdapter:
        if self._codex_adapter is not None:
            return self._codex_adapter
        if self._codex_factory is not None:
            adapter = self._codex_factory()  # type: ignore[operator]
            if not callable(getattr(adapter, "run_stage", None)):
                raise TypeError("codex factory returned an invalid adapter")
            return adapter  # type: ignore[return-value]
        source_root = Path(__file__).resolve().parents[2]
        runtime_root = self._safe_directory(self._plugin_root / "runtime")
        result_root = self._safe_directory(runtime_root / "codex-results")
        home_root = self._safe_directory(runtime_root / "codex-homes")
        schema = runtime_root / "stage-result.schema.json"
        schema_bytes = json.dumps(
            StageResult.model_json_schema(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self._write_runtime_file(schema, schema_bytes)
        executable_text = shutil.which("codex.exe") or shutil.which("codex")
        if executable_text is None:
            raise RuntimeError("Codex CLI executable is unavailable")
        skills = self._skill_registry(source_root)
        auth_file = (
            Path(os.environ.get("USERPROFILE", str(Path.home())))
            / ".codex"
            / "auth.json"
        )
        if os.environ.get("OPENAI_API_KEY"):
            auth_mode = "api_key"
            auth_source = None
        elif auth_file.is_file():
            auth_mode = "auth_file"
            auth_source = auth_file
        else:
            raise RuntimeError("Codex authentication is unavailable")
        self._codex_adapter = CodexAdapter(
            self._runner,
            schema.resolve(strict=True),
            result_root,
            Path(executable_text).resolve(),
            home_root,
            auth_mode,
            auth_source=auth_source,
            approved_skill_registry=skills,
        )
        return self._codex_adapter

    def _skill_registry(self, source_root: Path) -> dict[str, Path]:
        registry: dict[str, Path] = {}
        for name in set(_STAGE_SKILLS.values()):
            source = source_root / "skills" / name
            if not (source / "SKILL.md").is_file():
                raise RuntimeError(f"approved Codex skill is unavailable: {name}")
            registry[name] = source.resolve()
        return registry

    def _worktree(self, run_id: str) -> Path:
        path = self._checked_worktree_path(run_id)
        if not path.is_dir():
            raise RuntimeError("run worktree is unavailable")
        return path

    def _checked_worktree_path(self, run_id: str) -> Path:
        root = self._plugin_root / "worktrees"
        candidate = root / run_id
        for path in (root, candidate):
            if not path.exists():
                continue
            metadata = path.lstat()
            if path.is_symlink() or (
                getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                raise ValueError("worktree path is a link or reparse point")
        path = candidate.resolve()
        path.relative_to(self._plugin_root)
        return path

    def _design_head(self, config: ProjectConfig) -> str:
        adapter = GitAdapter(
            config.repo_path,
            remote=config.remote,
            base_branch=config.base_branch,
            test_branch=config.test_branch,
            expected_fetch_url=config.remote_fetch_url,
            expected_push_url=config.remote_push_url,
        )
        adapter.fetch(config.remote)
        return adapter.remote_sha(config.remote, config.base_branch)

    @staticmethod
    def _safe_directory(path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        metadata = path.lstat()
        if (
            path.is_symlink()
            or getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise ValueError("runtime path must be a non-reparse directory")
        return path.resolve(strict=True)

    @staticmethod
    def _write_runtime_file(path: Path, data: bytes) -> None:
        if path.exists():
            metadata = path.lstat()
            if (
                path.is_symlink()
                or getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                or path.read_bytes() != data
            ):
                raise ValueError("runtime schema identity changed")
            return
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _instructions(stage: str) -> str:
        return {
            "design": (
                "Read requirement.md and produce complete design.md and "
                "implementation-plan.md within this run directory."
            ),
            "implementation": (
                "Implement the approved design and plan in the isolated worktree."
            ),
            "internal_review": (
                "Review the pending worktree diff for correctness, security, "
                "tests, and approved-scope compliance. Do not modify files."
            ),
            "review_fix": (
                "Apply only actionable CI and review fixes within approved scope."
            ),
            "acceptance": (
                "Verify ExampleProject test acceptance for the exact current head. "
                "Use only approved public API data and exact cleanup."
            ),
        }[stage]

    @staticmethod
    def _allowed_actions(stage: str) -> tuple[str, ...]:
        if stage == "internal_review":
            return ("read files", "inspect diff", "run read-only checks")
        if stage == "design":
            return (
                "read requirement.md",
                "write design.md",
                "write implementation-plan.md",
            )
        if stage == "acceptance":
            return (
                "read registered test SHA facts",
                "use approved public API temporary data",
                "clean exact run-owned resource IDs",
            )
        return ("edit worktree files", "run approved local commands")

    @staticmethod
    def _status_policy(stage: str) -> str:
        if stage == "acceptance":
            return (
                "passed=all SHA-bound assertions and exact cleanup passed; "
                "failed=an assertion or cleanup failed; "
                "blocked=missing authority/dependency/schema or "
                "ENVIRONMENT_PREEMPTED=blocked"
            )
        if stage == "internal_review":
            return (
                "passed=no actionable critical/important finding; "
                "failed=actionable changes required; "
                "blocked=missing tree/schema/evidence or a decision is required"
            )
        return (
            "passed=approved stage work completed; "
            "failed=stage work or validation failed; "
            "blocked=missing authority/dependency/schema or scope conflict"
        )

    @staticmethod
    def _approval_scope(stage: str, run_root: Path) -> str:
        if stage == "design":
            return "Draft artifacts only; explicit approval is still required."
        artifacts: dict[str, str] = {}
        for name in (
            "requirement.md",
            "design.md",
            "implementation-plan.md",
        ):
            artifacts[name] = (run_root / name).read_text(encoding="utf-8")
        scope = json.dumps(
            artifacts,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(scope) > 20_000:
            raise ValueError("approved artifacts exceed Codex scope limit")
        return scope


class RuntimeAcceptancePort:
    """Reconcile SHA-bound acceptance facts produced by the acceptance Skill."""

    def __init__(
        self,
        repository: RunRepository,
        plugin_root: Path,
        stage_executor: RuntimeStageExecutor,
    ) -> None:
        self._repository = repository
        self._plugin_root = plugin_root.resolve()
        self._stage_executor = stage_executor

    def query(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> dict[str, object] | None:
        del config
        for evidence in reversed(self._repository.list_evidence(run.run_id)):
            if evidence.kind not in {"acceptance_passed", "acceptance_result"}:
                continue
            try:
                payload = json.loads(evidence.payload_json)
            except json.JSONDecodeError:
                return None
            if (
                type(payload) is dict
                and payload.get("stage") == "acceptance"
                and payload.get("sha") == run.head_sha
                and type(payload.get("passed")) is bool
            ):
                return payload
        return None

    def execute(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> dict[str, object]:
        run_root = self._plugin_root / "runs" / run.run_id
        result = self._stage_executor.execute(
            "acceptance",
            run,
            config,
            run_root,
        )
        if len(result.evidence) != 1:
            raise RuntimeError("acceptance result has no authoritative host evidence")
        evidence = self._repository.get_evidence(
            result.evidence[0].evidence_id
        )
        payload = json.loads(evidence.payload_json)
        if type(payload) is not dict:
            raise RuntimeError("acceptance evidence payload is invalid")
        return {
            **payload,
            "stage_status": result.status,
            "summary": result.summary,
        }


class RuntimeGitWorkspacePort:
    """Prepare, attest, commit, and clean one run's isolated worktree."""

    def __init__(self, repository: RunRepository, plugin_root: Path) -> None:
        self._repository = repository
        self._plugin_root = plugin_root.resolve()

    def query(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> dict[str, object] | None:
        path = self._worktree(run.run_id)
        if not path.exists():
            return None
        adapter = self._adapter(config)
        branch = adapter.worktree_branch(path)
        head = adapter.worktree_head(path)
        return {
            "sha": head,
            "passed": (
                head == run.head_sha
                and branch == f"feature/{run.run_id}"
            ),
        }

    def execute(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> dict[str, object]:
        if run.head_sha is None:
            raise ValueError("run head SHA is missing")
        adapter = self._adapter(config)
        adapter.fetch(config.remote)
        observed = adapter.remote_sha(config.remote, config.base_branch)
        if observed != run.head_sha:
            raise ValueError("approved base SHA is no longer current")
        result = adapter.create_worktree(
            f"{config.remote}/{config.base_branch}",
            f"feature/{run.run_id}",
            self._worktree(run.run_id),
            expected_base_sha=run.head_sha,
        )
        return {
            "sha": result.base_sha,
            "passed": result.base_sha == run.head_sha,
        }

    def validation_tree(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> str:
        return self._adapter(config).staged_tree(
            self._worktree(run.run_id),
            stage_all=True,
        )

    def review_tree(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> str:
        return self._adapter(config).staged_tree(
            self._worktree(run.run_id),
            stage_all=True,
        )

    def query_commit(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> dict[str, object] | None:
        if run.head_sha is None:
            return None
        adapter = self._adapter(config)
        path = self._worktree(run.run_id)
        if adapter.worktree_head(path) == run.head_sha:
            return None
        fact = adapter.head_commit_fact(path)
        if (
            fact["parent_sha"] != run.head_sha
            or fact["message"] != self._commit_message(run.run_id)
        ):
            return None
        return {
            **fact,
            "marker_run_id": run.run_id,
            "passed": True,
        }

    def execute_commit(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> dict[str, object]:
        if run.head_sha is None:
            raise ValueError("run head SHA is missing")
        adapter = self._adapter(config)
        path = self._worktree(run.run_id)
        expected_tree = self._reviewed_tree(run.run_id)
        new_sha = adapter.commit_tree_cas(
            path,
            self._commit_message(run.run_id),
            expected_parent=run.head_sha,
            expected_tree=expected_tree,
        )
        fact = adapter.head_commit_fact(path)
        if fact["new_head_sha"] != new_sha:
            raise RuntimeError("created commit is not current worktree head")
        return {
            **fact,
            "marker_run_id": run.run_id,
            "passed": True,
        }

    def cleanup_query(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> dict[str, bool]:
        path = self._worktree(run.run_id)
        if not path.exists():
            return {"clean": True, "removed": True}
        clean = not self._adapter(config).status_short(path).strip()
        return {"clean": clean, "removed": False}

    def cleanup_execute(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> dict[str, bool]:
        adapter = self._adapter(config)
        path = self._worktree(run.run_id)
        adapter.remove_clean_worktree(path)
        adapter.delete_merged_local_branch(f"feature/{run.run_id}")
        return {"clean": True, "removed": True}

    def _reviewed_tree(self, run_id: str) -> str:
        attestations = [
            evidence
            for evidence in self._repository.list_evidence(run_id)
            if evidence.kind == "internal_review_attestation"
        ]
        if not attestations:
            raise ValueError("internal review attestation is missing")
        payload = json.loads(attestations[-1].payload_json)
        tree = payload.get("reviewed_tree_sha")
        if not isinstance(tree, str):
            raise ValueError("internal review tree is invalid")
        return tree

    def _adapter(self, config: ProjectConfig) -> GitAdapter:
        return GitAdapter(
            config.repo_path,
            remote=config.remote,
            base_branch=config.base_branch,
            test_branch=config.test_branch,
            expected_fetch_url=config.remote_fetch_url,
            expected_push_url=config.remote_push_url,
        )

    def _worktree(self, run_id: str) -> Path:
        root = self._plugin_root / "worktrees"
        candidate = root / run_id
        for component in (root, candidate):
            if not component.exists():
                continue
            metadata = component.lstat()
            if component.is_symlink() or (
                getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                raise ValueError("worktree path is a link or reparse point")
        path = candidate.resolve()
        path.relative_to(self._plugin_root)
        return path

    @staticmethod
    def _commit_message(run_id: str) -> str:
        return f"chore(delivery): {run_id}"


class RuntimeCommitPort:
    def __init__(self, workspace: RuntimeGitWorkspacePort) -> None:
        self._workspace = workspace

    def validation_tree(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> str:
        return self._workspace.validation_tree(run, config)

    def review_tree(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> str:
        return self._workspace.review_tree(run, config)

    def query(self, run: RunRecord, config: ProjectConfig) -> object:
        return self._workspace.query_commit(run, config)

    def execute(self, run: RunRecord, config: ProjectConfig) -> object:
        return self._workspace.execute_commit(run, config)


class RuntimeCleanupPort:
    def __init__(self, workspace: RuntimeGitWorkspacePort) -> None:
        self._workspace = workspace

    def query(self, run: RunRecord, config: ProjectConfig) -> object:
        return self._workspace.cleanup_query(run, config)

    def execute(self, run: RunRecord, config: ProjectConfig) -> object:
        return self._workspace.cleanup_execute(run, config)
