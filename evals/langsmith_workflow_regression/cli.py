"""Controlled CLI for the native Durable Workflow LangSmith experiment."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Sequence
from uuid import uuid4

from assistant_agent.api.routes_agent import create_agent_runtime_for_composition
from assistant_agent.config import ProviderConfig
from assistant_agent.identity import RequestIdentity
from assistant_agent.providers.provider_errors import sanitize_error_message
from assistant_agent.runtime.assistant_run_service import load_env_file
from assistant_agent.runtime.checkpointer import AsyncCheckpointerOwner
from assistant_agent.runtime.runtime_host import RuntimeHost
from assistant_agent.workflows.graph_host import WorkflowGraphHost
from assistant_agent.workflows.models import WorkflowSubmission

from .contracts import (
    WORKFLOW_REGRESSION_DATASET,
    WorkflowExampleInput,
    WorkflowReferenceOutput,
)
from .evaluators import REQUIRED_WORKFLOW_FEEDBACK_KEYS
from .experiment import (
    DirectWorkflowInvocation,
    WorkflowExperimentSettings,
    inspect_workflow_dataset,
    run_workflow_experiment,
    wait_for_workflow_experiment_completeness,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CASE_TYPES = (
    "parallel_join",
    "constraint_verifier",
    "minimal_repair",
    "interrupt_resume_equivalence",
)


class ProductionWorkflowExperimentComposition:
    """Run-scoped production graph composition on one shared official saver."""

    def __init__(
        self,
        *,
        run_name: str,
        model: str,
        temporary_directory: tempfile.TemporaryDirectory[str],
        owner: AsyncCheckpointerOwner,
        workflow_host: WorkflowGraphHost,
        runtime_host: RuntimeHost,
    ) -> None:
        self.run_name = run_name
        self.model = model
        self._temporary_directory = temporary_directory
        self._owner = owner
        self._workflow_host = workflow_host
        self._runtime_host = runtime_host
        self._closed = False

    def invocation_factory(
        self,
        inputs: dict[str, Any],
        *,
        example_id: str,
        reference_output: WorkflowReferenceOutput,
    ) -> DirectWorkflowInvocation:
        example = WorkflowExampleInput.model_validate(inputs)
        identity_digest = hashlib.sha256(example_id.encode("utf-8")).hexdigest()[:24]
        identity = RequestIdentity.for_user(
            user_id="langsmith-workflow-operator",
            agent_id="assistant_agent",
            session_id=f"langsmith-workflow-{identity_digest}",
        )
        ingress_run_id = f"langsmith:{self.run_name}:{identity_digest}"
        submission_payload = example.submission
        submission = WorkflowSubmission(
            workflow_type=submission_payload.workflow_type,
            objective=submission_payload.objective,
            deliverables=list(submission_payload.deliverables),
            constraints=list(submission_payload.constraints),
            inputs=submission_payload.inputs.model_dump(mode="json"),
            requested_budget=submission_payload.requested_budget.model_dump(mode="json"),
            durability_reasons=list(submission_payload.durability_reasons),
            seed_artifact_refs=list(submission_payload.seed_artifact_refs),
            idempotency_key=(
                f"langsmith:{identity_digest}:"
                + hashlib.sha256(
                    submission_payload.idempotency_key.encode("utf-8")
                ).hexdigest()
            )[:240],
        )

        async def invoke():
            result = await self._workflow_host.arun_submission(
                identity=identity,
                ingress_run_id=ingress_run_id,
                submission=submission,
                ingress_trace_id=ingress_run_id,
            )
            if example.case_type == "interrupt_resume_equivalence":
                # The equivalence fact is earned only when this invocation
                # actually reached and resumed a native interrupt. Until the
                # production host exposes that controlled comparison, fail the
                # evaluator instead of manufacturing evidence.
                resume_equivalent = (
                    result.status == "completed"
                    and bool(result.final_state["consumed_action_refs"])
                    and reference_output.evaluation_contract.resume_equivalent
                )
            else:
                resume_equivalent = (
                    result.status == reference_output.terminal_status
                )
            return result, resume_equivalent

        return DirectWorkflowInvocation(invoke=invoke)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        try:
            await self._workflow_host.close()
        except BaseException as exc:
            errors.append(exc)
        try:
            if not await self._runtime_host.aclose(timeout=5.0):
                errors.append(RuntimeError("shared Agent Runtime did not close cleanly"))
        except BaseException as exc:
            errors.append(exc)
        try:
            await self._owner.aclose()
        except BaseException as exc:
            errors.append(exc)
        self._temporary_directory.cleanup()
        if errors:
            raise RuntimeError("workflow Experiment composition close failed") from errors[0]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or run the native Durable Workflow Experiment."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--inspect", action="store_true")
    action.add_argument("--preflight", action="store_true")
    action.add_argument("--run", action="store_true")
    parser.add_argument("--run-name")
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument("--feedback-wait-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--allow-real-provider", action="store_true")
    parser.add_argument(
        "--allow-workflow-side-effects",
        "--allow-runtime-side-effects",
        dest="allow_workflow_side_effects",
        action="store_true",
    )
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--no-env-file", action="store_true")
    args = parser.parse_args(argv)

    if args.inspect:
        _print(
            {
                "action": "inspect",
                "status": "offline_contract_ready",
                "dataset_name": WORKFLOW_REGRESSION_DATASET,
                "case_types": list(_CASE_TYPES),
                "feedback_keys": list(REQUIRED_WORKFLOW_FEEDBACK_KEYS),
                "operator_evidence": "pending",
            }
        )
        return 0

    action_name = "preflight" if args.preflight else "run"
    if not args.allow_real_provider:
        parser.error(f"--{action_name} requires --allow-real-provider")
    if not args.allow_workflow_side_effects:
        parser.error(
            f"--{action_name} requires --allow-workflow-side-effects "
            "(or --allow-runtime-side-effects)"
        )
    if args.run and not args.run_name:
        parser.error("--run requires --run-name")
    if args.max_concurrency < 1:
        parser.error("--max-concurrency must be positive")
    if args.feedback_wait_timeout_seconds <= 0:
        parser.error("--feedback-wait-timeout-seconds must be positive")
    if not args.no_env_file:
        load_env_file(args.env_file, override=False)

    try:
        config = ProviderConfig.from_env()
        if config.provider_mode != "real":
            raise RuntimeError(
                "workflow Experiment requires MULTIMODAL_AGENT_PROVIDER_MODE=real"
            )
        config.validate_provider_mode()
        payload = asyncio.run(_execute_async(config, args))
    except SystemExit:
        raise
    except Exception as exc:
        _print(_infrastructure_failure(exc))
        return 2
    _print(payload)
    return 0


async def _execute_async(config: ProviderConfig, args: argparse.Namespace) -> dict[str, Any]:
    run_name = args.run_name or f"workflow-preflight-{uuid4().hex[:12]}"
    composition = None
    client = None
    primary_error: BaseException | None = None
    try:
        composition = await _open_production_workflow_composition(
            config,
            run_name=run_name,
        )
        client = _langsmith_client()
        if args.preflight:
            _dataset, active, _originals = inspect_workflow_dataset(client)
            return {
                "action": "preflight",
                "backend": "langsmith",
                "status": "ready",
                "dataset_name": WORKFLOW_REGRESSION_DATASET,
                "active_example_count": len(active),
                "model": composition.model,
                "persistent_gate": "ready",
            }
        result = await run_workflow_experiment(
            client,
            WorkflowExperimentSettings(
                invocation_factory=composition.invocation_factory,
                run_name=run_name,
                model=composition.model,
                git_commit=_git_commit(),
                max_concurrency=args.max_concurrency,
            ),
        )
        client.flush()
        completeness = wait_for_workflow_experiment_completeness(
            client,
            experiment_id=result.experiment_id,
            example_ids=result.example_ids,
            requirements=result.tree_requirements,
            timeout_seconds=args.feedback_wait_timeout_seconds,
        )
        return {
            "action": "run",
            "backend": "langsmith",
            "dataset_name": WORKFLOW_REGRESSION_DATASET,
            "dataset_id": result.dataset_id,
            "experiment_id": result.experiment_id,
            "experiment_name": result.experiment_name,
            "experiment_url": result.experiment_url,
            "example_ids": list(result.example_ids),
            "run_ids": list(completeness.run_ids),
            "feedback": completeness.feedback,
            "persistent_gate": "complete",
        }
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        lifecycle_errors: list[BaseException] = []
        if composition is not None:
            try:
                await composition.aclose()
            except BaseException as exc:
                lifecycle_errors.append(exc)
        if client is not None:
            try:
                client.flush()
                client.close()
            except BaseException as exc:
                lifecycle_errors.append(exc)
        if primary_error is None and lifecycle_errors:
            raise RuntimeError("workflow Experiment lifecycle close failed") from lifecycle_errors[0]


async def _open_production_workflow_composition(
    config: ProviderConfig,
    *,
    run_name: str,
) -> ProductionWorkflowExperimentComposition:
    temporary_directory = tempfile.TemporaryDirectory(
        prefix="assistant-agent-langsmith-workflow-"
    )
    root = Path(temporary_directory.name)
    isolated_config = replace(
        config,
        durable_workflows_enabled=True,
        durable_workflow_worker_enabled=False,
        durable_workflow_path=str(root / "workflows.sqlite3"),
        durable_workflow_artifact_path=str(root / "artifacts"),
        langgraph_checkpointer_backend="sqlite",
        langgraph_checkpoint_path=str(root / "checkpoints.sqlite3"),
    )
    owner = AsyncCheckpointerOwner(isolated_config)
    workflow_host: WorkflowGraphHost | None = None
    runtime_host: RuntimeHost | None = None
    try:
        await owner.open()
        workflow_host = await WorkflowGraphHost.open(
            config=isolated_config,
            checkpointer_owner=owner,
        )
        runtime, trace_store = create_agent_runtime_for_composition(
            config=isolated_config,
            checkpointer=owner.checkpointer,
            graph_invocation_claim_store=owner.invocation_claim_store,
            workflow_graph_host=workflow_host,
        )
        runtime_host = RuntimeHost(runtime=runtime, owned_trace_store=trace_store)
        adapter = runtime.chat_adapter
        workflow_host.bind_runtime_services(
            provider_registry={
                "planner": adapter,
                "worker": adapter,
                "verifier": adapter,
            },
            tool_registry=runtime.registry,
        )
        return ProductionWorkflowExperimentComposition(
            run_name=run_name,
            model=isolated_config.resolved_chat_provider().model,
            temporary_directory=temporary_directory,
            owner=owner,
            workflow_host=workflow_host,
            runtime_host=runtime_host,
        )
    except BaseException:
        if workflow_host is not None:
            await workflow_host.close()
        if runtime_host is not None:
            await runtime_host.aclose(timeout=5.0)
        await owner.aclose()
        temporary_directory.cleanup()
        raise


def _git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    value = completed.stdout.strip()
    if not value:
        raise RuntimeError("git commit identity is unavailable")
    return value


def _infrastructure_failure(exc: BaseException) -> dict[str, Any]:
    return {
        "error": "langsmith_workflow_experiment_infrastructure_failure",
        "message": sanitize_error_message(exc),
        "ready": False,
        "reason_code": "preflight_failed",
        "persistent_gate": "pending",
    }


def _print(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False))


def _langsmith_client():
    from assistant_agent.observability.langsmith_config import (
        create_langsmith_client_from_env,
    )

    return create_langsmith_client_from_env()


__all__ = ["main"]
