"""Concrete local runtime ports used by the default CLI process."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time
from collections.abc import Callable
from uuid import uuid4

from change_delivery.adapters.codex import (
    CodexAdapter,
    StageExecutionRequest,
    assert_fix_attempt_allowed,
)
from change_delivery.adapters.git import GitAdapter
from change_delivery.adapters.http import ReadOnlyHttpClient
from change_delivery.adapters.project import (
    ExampleProjectAdapter,
    cleanup_matches_run,
)
from change_delivery.domain import (
    AcceptanceCheckResult,
    ProjectConfig,
    RunRecord,
    RunState,
    StageResult,
    stage_result_schema_json,
)
from change_delivery.evidence import sha256_payload
from change_delivery.process import ProcessRunner
from change_delivery.repository import RunRepository


_STAGE_SKILLS = {
    "design": "delivery-planning",
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
        review_fix_scope = (
            self._review_fix_scope(run) if stage == "review_fix" else None
        )
        review_fix_attempt = 1
        if review_fix_scope is not None:
            actionable_feedback = review_fix_scope["actionable_feedback"]
            if not isinstance(actionable_feedback, dict):
                raise ValueError("review fix feedback scope is invalid")
            candidate_attempt = actionable_feedback.get("attempt")
            if type(candidate_attempt) is not int:
                raise ValueError("review fix attempt is invalid")
            review_fix_attempt = candidate_attempt
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
            approval_scope=(
                json.dumps(
                    review_fix_scope,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                if review_fix_scope is not None
                else self._approval_scope(stage, run, run_root)
            ),
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
            attempt=review_fix_attempt,
            timeout_seconds=config.codex_timeout_seconds,
        )
        result = self._codex().run_stage(request)
        kind = (
            (
                "internal_review_source"
                if stage == "internal_review"
                else "acceptance_advisory_passed"
                if stage == "acceptance"
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
        executable_text = os.environ.get("CHANGE_DELIVERY_CODEX_EXECUTABLE")
        if executable_text is None:
            executable_text = shutil.which("codex.exe") or shutil.which("codex")
        if executable_text is None:
            raise RuntimeError("Codex CLI executable is unavailable")
        skills = self._skill_registry(source_root)
        auth_file = (
            Path(os.environ.get("USERPROFILE", str(Path.home())))
            / ".codex"
            / "auth.json"
        )
        configured_auth_mode = os.environ.get("CHANGE_DELIVERY_AUTH_MODE")
        configured_auth_source = os.environ.get("CHANGE_DELIVERY_AUTH_SOURCE")
        if configured_auth_mode == "api_key":
            if configured_auth_source or not os.environ.get("OPENAI_API_KEY"):
                raise RuntimeError("configured api_key authentication is unavailable")
            auth_mode = "api_key"
            auth_source = None
        elif configured_auth_mode == "auth_file":
            if not configured_auth_source:
                raise RuntimeError("configured auth_file authentication is unavailable")
            auth_mode = "auth_file"
            auth_source = Path(configured_auth_source)
        elif configured_auth_mode is not None:
            raise RuntimeError("configured Codex authentication mode is invalid")
        elif os.environ.get("OPENAI_API_KEY"):
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

    def _approval_scope(
        self,
        stage: str,
        run: RunRecord,
        run_root: Path,
    ) -> str:
        if stage == "design":
            return "Draft artifacts only; explicit approval is still required."
        artifacts = {
            name: contents.decode("utf-8")
            for name, contents in self._repository.get_approved_artifacts(
                run.run_id
            ).items()
        }
        scope = json.dumps(
            artifacts,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(scope) > 20_000:
            raise ValueError("approved artifacts exceed Codex scope limit")
        return scope

    def _review_fix_scope(self, run: RunRecord) -> dict[str, object]:
        if run.state is not RunState.FIXING_CI_REVIEW:
            raise ValueError("review fix stage requires its dedicated run state")
        entering = next(
            (
                event
                for event in reversed(self._repository.list_events(run.run_id))
                if event.to_state is RunState.FIXING_CI_REVIEW
                and event.from_state is RunState.MONITORING_CI_REVIEW
            ),
            None,
        )
        if entering is None:
            raise ValueError("review fix request event is missing")
        requests = []
        for evidence_id in entering.evidence_ids:
            evidence = self._repository.get_evidence(evidence_id)
            if evidence.kind == "ci_review_fix_request":
                requests.append(evidence)
        if len(requests) != 1:
            raise ValueError("review fix request evidence is missing or ambiguous")
        payload = json.loads(requests[0].payload_json)
        if not hmac.compare_digest(
            requests[0].sha256, sha256_payload(payload)
        ):
            raise ValueError("review fix request evidence digest mismatch")
        if (
            type(payload) is not dict
            or type(payload.get("attempt")) is not int
            or type(payload.get("issues")) is not list
            or not payload["issues"]
            or type(payload.get("feedback_fingerprint")) is not str
            or type(payload.get("untrusted_data")) is not dict
            or payload.get("head_sha") != run.head_sha
            or payload.get("pr_number") != run.pr_number
        ):
            raise ValueError("review fix request evidence is invalid")
        attempts = []
        for issue in payload["issues"]:
            if (
                type(issue) is not dict
                or set(issue) != {
                    "issue_identity",
                    "issue_fingerprint",
                    "existing_attempts",
                    "attempt",
                }
                or type(issue["issue_identity"]) is not str
                or type(issue["issue_fingerprint"]) is not str
                or type(issue["existing_attempts"]) is not int
                or type(issue["attempt"]) is not int
                or issue["attempt"] != issue["existing_attempts"] + 1
            ):
                raise ValueError("review fix issue evidence is invalid")
            issue_fingerprint = hashlib.sha256(
                issue["issue_identity"].encode("ascii")
            ).hexdigest()
            if not hmac.compare_digest(
                issue["issue_fingerprint"], issue_fingerprint
            ):
                raise ValueError("review fix issue fingerprint is invalid")
            assert_fix_attempt_allowed(
                issue["issue_fingerprint"], issue["existing_attempts"]
            )
            attempts.append(issue["attempt"])
        if payload["attempt"] != max(attempts):
            raise ValueError("review fix aggregate attempt is invalid")
        actual_fingerprint = hashlib.sha256(
            json.dumps(
                payload["untrusted_data"],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(
            payload["feedback_fingerprint"], actual_fingerprint
        ):
            raise ValueError("review fix feedback fingerprint is invalid")
        artifacts = {
            name: contents.decode("utf-8")
            for name, contents in self._repository.get_approved_artifacts(
                run.run_id
            ).items()
        }
        scope = {
            "approved_artifacts": artifacts,
            "actionable_feedback": payload,
        }
        encoded = json.dumps(
            scope,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(encoded) > 20_000:
            raise ValueError("review fix scope exceeds Codex scope limit")
        return scope


class RuntimeAcceptancePort:
    """Reconcile SHA-bound acceptance facts produced by the acceptance Skill."""

    def __init__(
        self,
        repository: RunRepository,
        plugin_root: Path,
        stage_executor: RuntimeStageExecutor,
        *,
        runner: ProcessRunner | None = None,
    ) -> None:
        self._repository = repository
        self._plugin_root = plugin_root.resolve()
        self._stage_executor = stage_executor
        self._runner = runner or ProcessRunner(environment_secrets)

    def query(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> dict[str, object] | None:
        if config.acceptance_check is None:
            return {
                "sha": run.head_sha,
                "passed": False,
                "reason": "MISSING_ACCEPTANCE_CHECK",
            }
        advisory = self._advisory_evidence(run)
        if advisory is None:
            return None
        return self._run_authority("query", run, config)

    def _advisory_evidence(
        self,
        run: RunRecord,
    ) -> dict[str, object] | None:
        for evidence in reversed(self._repository.list_evidence(run.run_id)):
            if evidence.kind != "acceptance_advisory_passed":
                continue
            try:
                payload = json.loads(evidence.payload_json)
            except json.JSONDecodeError:
                return None
            if (
                type(payload) is dict
                and payload.get("stage") == "acceptance"
                and payload.get("sha") == run.head_sha
                and payload.get("passed") is True
            ):
                return payload
        return None

    def execute(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> dict[str, object]:
        if config.acceptance_check is None:
            return {
                "sha": run.head_sha,
                "passed": False,
                "reason": "MISSING_ACCEPTANCE_CHECK",
            }
        run_root = self._plugin_root / "runs" / run.run_id
        result = self._stage_executor.execute(
            "acceptance",
            run,
            config,
            run_root,
        )
        if len(result.evidence) != 1:
            raise RuntimeError("acceptance result has no authoritative host evidence")
        if result.status != "passed":
            return {
                "sha": run.head_sha,
                "passed": False,
                "reason": "ACCEPTANCE_ADVISORY_BLOCKED",
                "stage_status": result.status,
            }
        return self._run_authority("execute", run, config)

    def _run_authority(
        self,
        action: str,
        run: RunRecord,
        config: ProjectConfig,
    ) -> dict[str, object]:
        assert config.acceptance_check is not None
        context = json.dumps(
            {
                "action": action,
                "data_prefix": f"codex-{run.run_id}",
                "project_id": run.project_id,
                "run_id": run.run_id,
                "schema_version": 1,
                "target_sha": run.head_sha,
                "test_branch": config.test_branch,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ) + "\n"
        result = self._runner.run(
            config.acceptance_check,
            stdin_text=context,
        )
        if result.exit_code != 0:
            raise RuntimeError("acceptance authority command failed")
        if len(result.stdout.encode("utf-8")) > 65_536:
            raise RuntimeError("acceptance authority output is too large")
        try:
            raw = json.loads(result.stdout)
            fact = AcceptanceCheckResult.model_validate(raw)
        except (json.JSONDecodeError, ValueError) as error:
            raise RuntimeError("acceptance authority output is invalid") from error
        expected_sha = run.head_sha or ""
        prefix_ok = bool(fact.resource_ids) and all(
            cleanup_matches_run(resource_id, run.run_id)
            for resource_id in fact.resource_ids
        )
        passed = (
            hmac.compare_digest(fact.target_sha, expected_sha)
            and hmac.compare_digest(fact.deployed_sha, expected_sha)
            and fact.assertions_passed
            and fact.cleanup_confirmed
            and prefix_ok
        )
        command_payload = config.acceptance_check.model_dump(mode="json")
        return {
            "sha": run.head_sha,
            "passed": passed,
            "target_sha": fact.target_sha,
            "deployed_sha": fact.deployed_sha,
            "assertions_passed": fact.assertions_passed,
            "resource_ids": list(fact.resource_ids),
            "cleanup_confirmed": fact.cleanup_confirmed,
            "command_id": result.command_id,
            "command_sha256": hashlib.sha256(
                json.dumps(
                    command_payload,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
            "stdout_sha256": hashlib.sha256(
                result.stdout.encode("utf-8")
            ).hexdigest(),
        }


class RuntimeTestEnvironmentPort:
    """Deploy one SHA to the shared test branch with an exact lease."""

    def __init__(
        self,
        repository: RunRepository,
        *,
        git_factory: Callable[[ProjectConfig], object] | None = None,
        http_factory: Callable[[ProjectConfig], object] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._repository = repository
        self._git_factory = git_factory or self._default_git
        self._http_factory = http_factory or (
            lambda config: ReadOnlyHttpClient(
                timeout_seconds=min(10, config.test_deploy_timeout_seconds)
            )
        )
        self._monotonic = monotonic
        self._sleep = sleep

    @staticmethod
    def _default_git(config: ProjectConfig) -> GitAdapter:
        return GitAdapter(
            config.repo_path,
            remote=config.remote,
            base_branch=config.base_branch,
            test_branch=config.test_branch,
            expected_fetch_url=config.remote_fetch_url,
            expected_push_url=config.remote_push_url,
        )

    def _facts(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> tuple[str, str]:
        git = self._git_factory(config)
        git.fetch(config.remote)  # type: ignore[attr-defined]
        branch_sha = git.remote_sha(  # type: ignore[attr-defined]
            config.remote,
            config.test_branch,
        )
        project = ExampleProjectAdapter(
            config,
            git=git,  # type: ignore[arg-type]
            github=object(),
            http=self._http_factory(config),  # type: ignore[arg-type]
        )
        return branch_sha, project.deployed_sha()

    def query(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> dict[str, object] | None:
        try:
            branch_sha, deployed_sha = self._facts(run, config)
        except Exception:
            return None
        if branch_sha == run.head_sha and deployed_sha == run.head_sha:
            return {
                "sha": run.head_sha,
                "passed": True,
                "test_branch_sha": branch_sha,
                "deployed_sha": deployed_sha,
            }
        pushed = any(
            evidence.kind == "test_branch_pushed"
            and json.loads(evidence.payload_json).get("sha") == run.head_sha
            for evidence in self._repository.list_evidence(run.run_id)
        )
        if pushed and branch_sha != run.head_sha:
            return {
                "sha": run.head_sha,
                "passed": False,
                "reason": "ENVIRONMENT_PREEMPTED",
                "test_branch_sha": branch_sha,
                "deployed_sha": deployed_sha,
            }
        return None

    def execute(
        self,
        run: RunRecord,
        config: ProjectConfig,
    ) -> dict[str, object]:
        if run.head_sha is None:
            raise ValueError("run head SHA is missing")
        missing_fields = []
        if not isinstance(config.version_endpoint, str) or not config.version_endpoint:
            missing_fields.append("version_endpoint")
        if config.acceptance_check is None:
            missing_fields.append("acceptance_check")
        if missing_fields:
            return {
                "sha": run.head_sha,
                "passed": False,
                "reason": "DEPLOYMENT_CONFIG_INCOMPLETE",
                "missing_fields": missing_fields,
            }
        git = self._git_factory(config)
        git.fetch(config.remote)  # type: ignore[attr-defined]
        observed = git.remote_sha(  # type: ignore[attr-defined]
            config.remote,
            config.test_branch,
        )
        pushed = git.push_test_with_lease(  # type: ignore[attr-defined]
            config.remote,
            config.test_branch,
            run.head_sha,
            observed,
        )
        self._repository.register_evidence(
            run.run_id,
            f"test-branch-pushed:{run.run_id}:{run.head_sha}",
            "test_branch_pushed",
            {
                "sha": run.head_sha,
                "observed_sha": observed,
                "pushed_sha": pushed,
                "test_branch": config.test_branch,
            },
        )
        deadline = self._monotonic() + config.test_deploy_timeout_seconds
        while self._monotonic() < deadline:
            observed_fact = self.query(run, config)
            if observed_fact is not None:
                return observed_fact
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            self._sleep(
                min(config.test_deploy_poll_interval_seconds, remaining)
            )
        return {
            "sha": run.head_sha,
            "passed": False,
            "reason": "VERSION_NOT_READY",
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
