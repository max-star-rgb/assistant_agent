"""Operator-gated native CodingGraph behavior system-eval runner."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from time import monotonic
from typing import Callable, Mapping

from pydantic import ValidationError

from assistant_agent.coding.config import (
    CodingCommandConfig,
    CodingConfig,
    CodingRepositoryConfig,
)
from assistant_agent.coding.models import CodingWorkspace
from assistant_agent.coding.sandbox import DockerCodingSandboxBackend
from assistant_agent.coding.validation import CodingValidationService
from assistant_agent.coding.workspace import CodingWorkspaceService
from assistant_agent.config import ProviderConfig
from assistant_agent.evaluation.coding_agent_server import (
    CodingBehaviorAgentServerDriver,
    CodingBehaviorDriverResult,
    DriverOutcome,
    FixtureApprovalPolicy,
)
from assistant_agent.evaluation.coding_behavior import (
    SCHEMA_VERSION,
    CodingBehaviorCase,
    CodingBehaviorCaseBinding,
    CodingBehaviorCaseResult,
    CodingBehaviorDryRunReport,
    CodingBehaviorError,
    CodingBehaviorSuite,
    CodingBehaviorSuiteBinding,
    CodingBehaviorSuiteResult,
    build_coding_behavior_dry_run,
    validate_coding_behavior_suite_result,
)
from evals.system.ai_coding_behavior.fixtures import (
    CodingBehaviorFixture,
    CodingBehaviorFixtureStore,
    FixtureCreationError,
    governed_git_environment,
)
from evals.system.ai_coding_behavior.graders import (
    CodingBehaviorGradeInput,
    HeldOutValidationRequest,
    HeldOutValidationResult,
    grade_coding_behavior_case,
)
from evals.system.common.artifacts import create_run_dir, write_json
from evals.system.common.preflight import (
    SystemEvalConfigurationError,
    validate_real_chat_config,
)


BASELINE_SUITE_ID = "baseline-v1"
FIXED_SERVER_URL = "http://127.0.0.1:8089"
_REPOSITORY_COMMAND_ID = "coding-eval-validation-v1"
_SANDBOX_IMAGE_PATTERN = (
    "0123456789abcdef"
)
_MAX_ARTIFACT_BYTES = 1_048_576
_MAX_DRIVER_EVIDENCE_BYTES = 16_384
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MANIFEST_PATH = Path(__file__).with_name("cases.json")
_OUTPUT_ROOT = _REPO_ROOT / ".data" / "evals" / "system" / "ai_coding_behavior"
_WORK_PARENT = _OUTPUT_ROOT / "work"


class CodingBehaviorRunnerConfigurationError(RuntimeError):
    """A real evaluation did not satisfy its explicit operator gates."""


@dataclass(frozen=True, slots=True)
class CodingBehaviorRealRunOptions:
    suite_id: str
    server_url: str
    sandbox_image: str


@dataclass(frozen=True, slots=True)
class _PreparedCase:
    case: CodingBehaviorCase
    fixture: CodingBehaviorFixture
    repository_id: str
    repository: CodingRepositoryConfig


_HELD_OUT_PROGRAMS: Mapping[str, str] = {
    "single-file-logic-bug-v1": (
        "from src.range_check import contains\n"
        "assert contains(10, 0, 10) is True\n"
        "assert contains(-1, 0, 10) is False\n"
    ),
    "multi-file-interface-v1": (
        "from src.client import render_user\n"
        "assert render_user({'first_name': 'Ada', 'last_name': 'Lovelace'}) == 'Lovelace, Ada'\n"
    ),
    "regression-test-required-v1": (
        "from src.escaping import escape_html\n"
        "assert escape_html('&<>') == '&amp;&lt;&gt;'\n"
    ),
    "scope-discipline-v1": (
        "from src.total import calculate_total\n"
        "assert calculate_total([1, 2, 3]) == 6\n"
        "assert calculate_total([]) == 0\n"
    ),
}


def load_baseline_suite(path: Path = _MANIFEST_PATH) -> CodingBehaviorSuite:
    """Load only the tracked, exact baseline suite."""

    try:
        suite = CodingBehaviorSuite.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as exc:
        raise CodingBehaviorRunnerConfigurationError(
            "coding behavior suite is invalid"
        ) from exc
    if suite.suite_id != BASELINE_SUITE_ID:
        raise CodingBehaviorRunnerConfigurationError(
            f"system eval requires exact suite {BASELINE_SUITE_ID}"
        )
    return suite


def build_real_run_options(
    *,
    suite_id: str,
    server_url: str,
    allow_real_provider: bool,
    allow_local_git_mutation: bool,
    sandbox_image: str | None = None,
) -> CodingBehaviorRealRunOptions:
    if not allow_real_provider:
        raise CodingBehaviorRunnerConfigurationError(
            "real mode requires --allow-real-provider"
        )
    if not allow_local_git_mutation:
        raise CodingBehaviorRunnerConfigurationError(
            "real mode requires --allow-local-git-mutation"
        )
    if server_url != FIXED_SERVER_URL:
        raise CodingBehaviorRunnerConfigurationError(
            "real mode requires http://127.0.0.1:8089"
        )
    if suite_id != BASELINE_SUITE_ID:
        raise CodingBehaviorRunnerConfigurationError(
            f"real mode requires exact suite {BASELINE_SUITE_ID}"
        )
    image = (sandbox_image or "").strip()
    prefix, separator, digest = image.rpartition("@sha256:")
    if (
        not separator
        or not prefix
        or len(digest) != 64
        or any(character not in _SANDBOX_IMAGE_PATTERN for character in digest)
    ):
        raise CodingBehaviorRunnerConfigurationError(
            "real mode requires a digest-pinned --sandbox-image"
        )
    return CodingBehaviorRealRunOptions(
        suite_id=suite_id,
        server_url=server_url,
        sandbox_image=image,
    )


class IsolatedHeldOutValidationExecutor:
    """Run a fixed held-out command through the existing network-none sandbox."""

    def __init__(self, *, work_root: Path, sandbox_image: str) -> None:
        self._work_root = work_root.resolve()
        self._sandbox_image = sandbox_image
        self._sandbox = DockerCodingSandboxBackend(
            owner_id=f"coding-eval-{secrets.token_hex(8)}"
        )

    def execute(self, request: HeldOutValidationRequest) -> HeldOutValidationResult:
        program = _HELD_OUT_PROGRAMS.get(request.command_id)
        if program is None:
            raise ValueError("held-out command is not in the trusted catalog")
        self._require_repository_binding(request)
        repo_id = f"held-out-{sha256(request.command_id.encode()).hexdigest()[:16]}"
        command = CodingCommandConfig(
            command_id=request.command_id,
            kind="test",
            argv=("python", "-c", program),
            timeout_seconds=request.timeout_seconds,
            cpu_seconds=min(request.timeout_seconds, 120),
            max_output_bytes=65_536,
            max_disk_bytes=67_108_864,
            max_files=4_096,
        )
        repository = CodingRepositoryConfig(
            repo_id=repo_id,
            path=request.repository.resolve(strict=True),
            target_branch="main",
            commands={request.command_id: command},
            verification_sequence=(request.command_id,),
            sandbox_enabled=True,
            sandbox_image=self._sandbox_image,
        )
        validation_root = self._work_root / "held-out-validation"
        config = CodingConfig(
            enabled=True,
            workspace_root=validation_root,
            repositories={repo_id: repository},
            max_changed_files=16,
            max_patch_bytes=65_536,
            max_file_bytes=1_048_576,
        )
        service = CodingValidationService(
            CodingWorkspaceService(config),
            sandbox_backend=self._sandbox,
        )
        workspace = CodingWorkspace(
            workspace_ref=f"held-out-{sha256((request.command_id + request.expected_commit).encode()).hexdigest()}",
            root=request.repository,
            repo_id=repo_id,
            base_commit=request.expected_commit,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        result = service.run(workspace, repository, format_round=0)
        self._require_repository_binding(request)
        if len(result.evidence) != 1:
            raise ValueError("held-out validation returned an invalid evidence inventory")
        evidence = result.evidence[0]
        status = evidence.status
        error_category = {
            "passed": "none",
            "timed_out": "timed_out",
            "resource_exceeded": "resource_exceeded",
        }.get(status, "output_limit" if evidence.truncated else "failed")
        return HeldOutValidationResult(
            status=status,
            returncode=evidence.exit_code,
            stdout_digest=sha256(evidence.stdout.encode("utf-8")).hexdigest(),
            stderr_digest=sha256(evidence.stderr.encode("utf-8")).hexdigest(),
            error_category=error_category,
        )

    def close(self) -> None:
        asyncio.run(self._sandbox.aclose())

    @staticmethod
    def _require_repository_binding(request: HeldOutValidationRequest) -> None:
        metadata = os.fstat(request.repository_fd)
        if metadata.st_ino != request.repository_inode:
            raise ValueError("held-out repository descriptor identity changed")
        completed = subprocess.run(
            ("git", "rev-parse", "HEAD", "HEAD^{tree}"),
            cwd=request.repository,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            env=governed_git_environment(),
            pass_fds=request.pass_fds,
            check=False,
        )
        lines = completed.stdout.decode("ascii", errors="strict").splitlines()
        if (
            completed.returncode != 0
            or lines != [request.expected_commit, request.expected_tree_digest]
            or len(completed.stderr) > 65_536
        ):
            raise ValueError("held-out repository binding changed")


def run_coding_behavior_eval(
    *,
    real: bool = False,
    suite_id: str | None = None,
    server_url: str = FIXED_SERVER_URL,
    allow_real_provider: bool = False,
    allow_local_git_mutation: bool = False,
    sandbox_image: str | None = None,
    binding_acknowledger: Callable[[dict[str, object]], bool] | None = None,
) -> CodingBehaviorDryRunReport | CodingBehaviorSuiteResult:
    suite = load_baseline_suite()
    if not real:
        if suite_id not in {None, suite.suite_id}:
            raise CodingBehaviorRunnerConfigurationError(
                f"dry-run requires exact suite {BASELINE_SUITE_ID}"
            )
        return build_coding_behavior_dry_run(suite)

    options = build_real_run_options(
        suite_id=suite_id or "",
        server_url=server_url,
        allow_real_provider=allow_real_provider,
        allow_local_git_mutation=allow_local_git_mutation,
        sandbox_image=sandbox_image,
    )
    try:
        provider = ProviderConfig.from_env()
        validate_real_chat_config(provider)
    except (ValueError, SystemEvalConfigurationError) as exc:
        raise CodingBehaviorRunnerConfigurationError(str(exc)) from exc
    return _run_real_suite(
        suite,
        options=options,
        provider=provider,
        binding_acknowledger=binding_acknowledger or _terminal_binding_acknowledger,
    )


def _run_real_suite(
    suite: CodingBehaviorSuite,
    *,
    options: CodingBehaviorRealRunOptions,
    provider: ProviderConfig,
    binding_acknowledger: Callable[[dict[str, object]], bool],
) -> CodingBehaviorSuiteResult:
    started = monotonic()
    _prepare_owned_directory(_OUTPUT_ROOT)
    _prepare_owned_directory(_WORK_PARENT)
    store = CodingBehaviorFixtureStore(_WORK_PARENT)
    prepared: list[_PreparedCase] = []
    case_results: dict[str, CodingBehaviorCaseResult] = {}
    released_fixture_tokens: set[str] = set()
    thread_cleanup_debts: list[tuple[str, object]] = []
    outer_cleanup_pending: set[str] = set()
    identity = f"coding-eval-{secrets.token_hex(12)}"
    executor = IsolatedHeldOutValidationExecutor(
        work_root=_WORK_PARENT,
        sandbox_image=options.sandbox_image,
    )
    try:
        for case in suite.cases:
            fixture = store.create(case)
            repository_id = f"eval-{sha256((identity + case.case_id).encode()).hexdigest()[:24]}"
            repository = _server_repository(
                fixture,
                repository_id=repository_id,
                sandbox_image=options.sandbox_image,
            )
            prepared.append(_PreparedCase(case, fixture, repository_id, repository))
        binding = _binding_projection(prepared, identity=identity)
        binding_ready = binding_acknowledger(binding)
        run_items = prepared if binding_ready else []
        if not binding_ready:
            for item in prepared:
                case_results[item.case.case_id] = _failed_case(
                    item.case,
                    "coding_eval_configuration_error",
                    "Operator did not confirm the reloaded 8089 repository binding.",
                )
        for item in run_items:
            outcome: DriverOutcome | None = None
            cleanup_pending = False
            try:
                policy = FixtureApprovalPolicy(
                    store=store,
                    case=item.case,
                    fixture=item.fixture,
                    repository_id=item.repository_id,
                    identity=identity,
                    target_branch="main",
                )
                outcome = CodingBehaviorAgentServerDriver(
                    server_url=options.server_url,
                    identity=identity,
                    max_interrupts=len(item.case.required_interrupts),
                ).run(case=item.case, policy=policy)
                case_results[item.case.case_id] = _case_result(
                    item.case,
                    item.fixture,
                    outcome.result,
                    store=store,
                    executor=executor,
                )
            except Exception:
                case_results[item.case.case_id] = _failed_case(
                    item.case,
                    "coding_eval_grader_failed",
                    "Evaluation orchestration failed.",
                )
            finally:
                if outcome is not None and outcome.cleanup_debt is not None:
                    cleanup_pending = not outcome.cleanup_debt.retry()
                    if cleanup_pending:
                        thread_cleanup_debts.append(
                            (item.case.case_id, outcome.cleanup_debt)
                        )
                try:
                    store.cleanup(item.fixture, item.case)
                except Exception:
                    cleanup_pending = True
                else:
                    released_fixture_tokens.add(item.fixture.capability_token)
                if cleanup_pending:
                    case_results[item.case.case_id] = _failed_case(
                        item.case,
                        "coding_eval_cleanup_pending",
                        "Evaluation cleanup remains pending.",
                        cleanup_pending=True,
                        prior=case_results.get(item.case.case_id),
                    )
    except FixtureCreationError as exc:
        matching = next(
            (case for case in suite.cases if case.case_id == exc.fixture.case_id),
            None,
        )
        if matching is not None:
            try:
                store.cleanup(exc.fixture, matching)
            except Exception:
                pass
        raise CodingBehaviorRunnerConfigurationError(
            "fixture creation failed with cleanup pending"
        ) from exc
    finally:
        outer_cleanup_pending.update(
            _cleanup_owned_fixtures(
                store,
                prepared,
                released_fixture_tokens,
            )
        )
        for case_id, debt in thread_cleanup_debts:
            try:
                released = debt.retry()
            except Exception:
                released = False
            if not released:
                outer_cleanup_pending.add(case_id)
        executor.close()

    for case in suite.cases:
        if case.case_id in outer_cleanup_pending:
            case_results[case.case_id] = _failed_case(
                case,
                "coding_eval_cleanup_pending",
                "Evaluation cleanup remains pending.",
                cleanup_pending=True,
                prior=case_results.get(case.case_id),
            )

    for case in suite.cases:
        case_results.setdefault(
            case.case_id,
            _failed_case(
                case,
                "coding_eval_configuration_error",
                "Evaluation did not execute the complete suite.",
            ),
        )
    ordered = tuple(case_results[case.case_id] for case in suite.cases)
    result = CodingBehaviorSuiteResult(
        schema_version=SCHEMA_VERSION,
        suite_id=suite.suite_id,
        execution_profile=suite.execution_profile,
        suite_binding=CodingBehaviorSuiteBinding.from_suite(suite),
        status="passed" if all(item.status == "passed" for item in ordered) else "failed",
        cases=ordered,
        elapsed_ms=min(115_200_000, max(0, int((monotonic() - started) * 1000))),
    )
    validated = validate_coding_behavior_suite_result(suite, result)
    write_result_artifact(
        root=_OUTPUT_ROOT,
        suite=suite,
        result=validated,
        provider_id=provider.chat_provider,
        model_id=provider.chat_model or provider.resolved_chat_provider().model,
    )
    return validated


def _cleanup_owned_fixtures(
    store: object,
    prepared: list[object],
    released_fixture_tokens: set[str],
) -> set[str]:
    """Consume every capability created by this run, including active fixtures."""

    pending: set[str] = set()
    for item in prepared:
        token = item.fixture.capability_token
        if token in released_fixture_tokens:
            continue
        try:
            store.cleanup(item.fixture, item.case)
        except Exception:
            pending.add(item.case.case_id)
        else:
            released_fixture_tokens.add(token)
    return pending


def _case_result(
    case: CodingBehaviorCase,
    fixture: CodingBehaviorFixture,
    driver: CodingBehaviorDriverResult,
    *,
    store: CodingBehaviorFixtureStore,
    executor: IsolatedHeldOutValidationExecutor,
) -> CodingBehaviorCaseResult:
    if driver.status != "completed":
        return _failed_case(
            case,
            driver.error_code or "coding_eval_terminal_mismatch",
            "Native coding run failed.",
            elapsed_ms=driver.elapsed_ms,
            cleanup_pending=driver.cleanup_pending,
        )
    required = (
        driver.final_commit,
        driver.validation_tree_digest,
        driver.review_tree_digest,
        driver.integration_tree_digest,
    )
    if any(value is None for value in required):
        return _failed_case(
            case,
            "coding_eval_terminal_mismatch",
            "Native terminal evidence is incomplete.",
            elapsed_ms=driver.elapsed_ms,
        )
    evidence_size = len(
        json.dumps(
            [
                {
                    "sequence": item.sequence,
                    "kind": item.kind,
                    "checkpoint_digest": item.checkpoint_digest,
                }
                for item in driver.transitions
            ],
            separators=(",", ":"),
        ).encode("utf-8")
    )
    grade = grade_coding_behavior_case(
        case,
        CodingBehaviorGradeInput(
            fixture=fixture,
            terminal_status=driver.terminal_status or "",
            interrupt_kinds=driver.interrupt_kinds,
            elapsed_ms=driver.elapsed_ms,
            interrupt_count=driver.interrupt_count,
            max_interrupts=len(case.required_interrupts),
            evidence_size_bytes=evidence_size,
            max_evidence_size_bytes=_MAX_DRIVER_EVIDENCE_BYTES,
            validation_tree_digest=driver.validation_tree_digest or "",
            review_tree_digest=driver.review_tree_digest or "",
            integration_tree_digest=driver.integration_tree_digest or "",
            final_commit=driver.final_commit or "",
        ),
        store=store,
        validation_executor=executor,
    )
    return CodingBehaviorCaseResult(
        schema_version=SCHEMA_VERSION,
        case_id=case.case_id,
        fixture_id=case.fixture_id,
        case_binding=CodingBehaviorCaseBinding.from_case(case),
        status=grade.status,
        checks=grade.checks,
        error=(
            None
            if grade.status == "passed"
            else _error("coding_eval_grader_failed", "A deterministic grader failed.")
        ),
        terminal_status=driver.terminal_status,
        changed_paths=grade.changed_paths,
        elapsed_ms=driver.elapsed_ms,
        cleanup_pending=False,
    )


def _failed_case(
    case: CodingBehaviorCase,
    code: str,
    message: str,
    *,
    elapsed_ms: int = 0,
    cleanup_pending: bool = False,
    prior: CodingBehaviorCaseResult | None = None,
) -> CodingBehaviorCaseResult:
    return CodingBehaviorCaseResult(
        schema_version=SCHEMA_VERSION,
        case_id=case.case_id,
        fixture_id=case.fixture_id,
        case_binding=CodingBehaviorCaseBinding.from_case(case),
        status="failed",
        checks=prior.checks if prior is not None else (),
        error=_error(code, message),
        terminal_status=prior.terminal_status if prior is not None else None,
        changed_paths=prior.changed_paths if prior is not None else (),
        elapsed_ms=prior.elapsed_ms if prior is not None else elapsed_ms,
        cleanup_pending=cleanup_pending,
    )


def _error(code: str, message: str) -> CodingBehaviorError:
    return CodingBehaviorError(
        schema_version=SCHEMA_VERSION,
        code=code,  # type: ignore[arg-type]
        message=message,
    )


def _server_repository(
    fixture: CodingBehaviorFixture,
    *,
    repository_id: str,
    sandbox_image: str,
) -> CodingRepositoryConfig:
    command = CodingCommandConfig(
        command_id=_REPOSITORY_COMMAND_ID,
        kind="test",
        argv=("python", "-m", "compileall", "-q", "src", "tests"),
        timeout_seconds=60,
        cpu_seconds=60,
        max_output_bytes=65_536,
        max_disk_bytes=67_108_864,
        max_files=4_096,
    )
    return CodingRepositoryConfig(
        repo_id=repository_id,
        path=fixture.repository.resolve(strict=True),
        target_branch="main",
        parallel_analysis_enabled=False,
        code_review_enabled=True,
        commands={_REPOSITORY_COMMAND_ID: command},
        verification_sequence=(_REPOSITORY_COMMAND_ID,),
        integration_enabled=True,
        sandbox_enabled=True,
        sandbox_image=sandbox_image,
        dependency_profile=None,
        artifact_profile=None,
        commit_author_name="Assistant Agent Eval",
        commit_author_email="eval@invalid.local",
    )


def _binding_projection(
    prepared: list[_PreparedCase], *, identity: str
) -> dict[str, object]:
    repositories: dict[str, object] = {}
    for item in prepared:
        payload = item.repository.model_dump(mode="json")
        payload.pop("repo_id", None)
        repositories[item.repository_id] = payload
    workspace_root = str((_WORK_PARENT / "server-workspaces").resolve())
    return {
        "schema_version": SCHEMA_VERSION,
        "server": FIXED_SERVER_URL,
        "identity": identity,
        "operator_action": "restart the existing 8089 server with exactly this temporary coding binding",
        "environment": {
            "MULTIMODAL_AGENT_CODING_ENABLED": "true",
            "MULTIMODAL_AGENT_CODING_WORKSPACE_ROOT": workspace_root,
            "MULTIMODAL_AGENT_CODING_REPOSITORIES_JSON": json.dumps(
                repositories, sort_keys=True, separators=(",", ":")
            ),
        },
    }


def _terminal_binding_acknowledger(binding: dict[str, object]) -> bool:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise CodingBehaviorRunnerConfigurationError(
            "real mode requires an interactive operator for the 8089 binding reload"
        )
    print(
        json.dumps(binding, ensure_ascii=False, indent=2, sort_keys=True),
        file=sys.stderr,
    )
    answer = input(
        "Restart the existing 8089 server with this binding, then type BINDING READY: "
    )
    return answer == "BINDING READY"


def write_result_artifact(
    *,
    root: Path,
    suite: CodingBehaviorSuite,
    result: CodingBehaviorSuiteResult,
    provider_id: str,
    model_id: str,
) -> Path:
    validated = validate_coding_behavior_suite_result(suite, result)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "provider_id": provider_id,
        "model_id": model_id,
        "result": validated.model_dump(mode="json"),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_ARTIFACT_BYTES:
        raise CodingBehaviorRunnerConfigurationError(
            "coding behavior artifact exceeds its bound"
        )
    _prepare_owned_directory(root)
    run_dir = create_run_dir(root, domain="run", case_id=suite.suite_id)
    temporary = run_dir / ".result.json.tmp"
    destination = run_dir / "result.json"
    write_json(temporary, payload)
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    directory_fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return destination


def _prepare_owned_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise CodingBehaviorRunnerConfigurationError(
            "coding behavior output root is unsafe"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the native AI coding behavior system evaluation."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="real", action="store_false")
    mode.add_argument("--real", dest="real", action="store_true")
    parser.set_defaults(real=False)
    parser.add_argument("--suite-id")
    parser.add_argument("--server", default=FIXED_SERVER_URL)
    parser.add_argument("--sandbox-image")
    parser.add_argument("--allow-real-provider", action="store_true")
    parser.add_argument("--allow-local-git-mutation", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        report = run_coding_behavior_eval(
            real=arguments.real,
            suite_id=arguments.suite_id,
            server_url=arguments.server,
            sandbox_image=arguments.sandbox_image,
            allow_real_provider=arguments.allow_real_provider,
            allow_local_git_mutation=arguments.allow_local_git_mutation,
        )
    except CodingBehaviorRunnerConfigurationError as exc:
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "failed",
                    "error_code": "coding_eval_configuration_error",
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(report.model_dump_json(indent=2))
    return 0 if isinstance(report, CodingBehaviorDryRunReport) or report.status == "passed" else 1


__all__ = [
    "BASELINE_SUITE_ID",
    "FIXED_SERVER_URL",
    "CodingBehaviorRealRunOptions",
    "CodingBehaviorRunnerConfigurationError",
    "IsolatedHeldOutValidationExecutor",
    "build_real_run_options",
    "load_baseline_suite",
    "main",
    "run_coding_behavior_eval",
    "write_result_artifact",
]


if __name__ == "__main__":
    raise SystemExit(main())
