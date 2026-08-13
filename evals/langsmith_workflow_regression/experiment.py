"""Run and audit native DurableWorkflowGraph LangSmith experiments."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import math
import time
from typing import Any, Protocol
from uuid import uuid4

from langsmith.utils import LangSmithRateLimitError

from assistant_agent.workflows.durable_graph_app import WorkflowGraphStreamResult
from assistant_agent.workflows.graph_state import (
    PersistedAdmittedWorkflowPlan,
    WorkflowResultSlot,
    validate_durable_workflow_state,
)

from .contracts import (
    WORKFLOW_REGRESSION_DATASET,
    WorkflowDatasetExample,
    WorkflowReferenceOutput,
    validate_active_examples,
)
from .evaluators import (
    REQUIRED_WORKFLOW_FEEDBACK_KEYS,
    langsmith_evaluators,
)
from evals.release_review.report import LangSmithTargetEvidence


class WorkflowInvocationFactory(Protocol):
    def __call__(
        self,
        inputs: dict[str, Any],
        *,
        example_id: str,
        reference_output: WorkflowReferenceOutput,
    ) -> "DirectWorkflowInvocation": ...


@dataclass(frozen=True)
class DirectWorkflowInvocation:
    """All production-owned objects needed to call the already compiled graph."""

    invoke: Callable[
        [], Awaitable[tuple[WorkflowGraphStreamResult, bool | None]]
    ]


@dataclass(frozen=True)
class WorkflowExperimentSettings:
    invocation_factory: WorkflowInvocationFactory
    run_name: str
    model: str
    git_commit: str
    max_concurrency: int = 1


@dataclass(frozen=True)
class WorkflowExperimentResult:
    native_result: Any
    experiment_id: str
    experiment_name: str
    experiment_url: str | None
    dataset_id: str
    example_ids: tuple[str, ...]
    run_ids: tuple[str, ...]
    tree_requirements: dict[str, "WorkflowTreeRequirement"]


@dataclass(frozen=True)
class WorkflowCompletenessResult:
    run_ids: tuple[str, ...]
    feedback: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class NativeWorkflowTreeAudit:
    complete: bool
    run_ids: tuple[str, ...]
    problems: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class WorkflowTreeRequirement:
    require_verifier: bool = True
    worker_generations: tuple[tuple[str, int], ...] = ()
    repair_generations: tuple[tuple[str, int], ...] = ()


def workflow_regression_equivalence_evidence(
    result: WorkflowExperimentResult,
    completeness: WorkflowCompletenessResult,
) -> LangSmithTargetEvidence:
    """Project only persisted Workflow Regression facts into Gate P3."""

    return LangSmithTargetEvidence(
        target="workflow_regression",
        dataset_id=result.dataset_id,
        project_id=result.experiment_id,
        experiment_id=result.experiment_id,
        active_example_ids=result.example_ids,
        root_run_ids=completeness.run_ids,
        required_feedback=REQUIRED_WORKFLOW_FEEDBACK_KEYS,
        feedback=completeness.feedback,
        native_tree_complete=True,
    )


def inspect_workflow_dataset(
    client: Any,
) -> tuple[Any, tuple[WorkflowDatasetExample, ...], dict[str, Any]]:
    dataset = client.read_dataset(dataset_name=WORKFLOW_REGRESSION_DATASET)
    raw_examples = []
    originals: dict[str, Any] = {}
    for example in client.list_examples(dataset_id=dataset.id):
        payload = {
            "id": str(_field(example, "id") or ""),
            "inputs": _field(example, "inputs"),
            "outputs": _field(example, "outputs"),
            "metadata": _field(example, "metadata"),
        }
        raw_examples.append(payload)
        originals[payload["id"]] = example
    active = validate_active_examples(raw_examples)
    return dataset, active, originals


async def run_workflow_example(
    invocation: DirectWorkflowInvocation,
    *,
    require_resume_equivalence: bool = False,
) -> dict[str, Any]:
    """Execute the actual compiled graph app; no scheduler/eval runtime exists here."""

    result, resume_equivalent = await invocation.invoke()
    return project_workflow_result(
        result,
        resume_equivalent=resume_equivalent,
        require_resume_equivalence=require_resume_equivalence,
    )


def project_workflow_result(
    result: WorkflowGraphStreamResult | Any,
    *,
    resume_equivalent: bool | None,
    require_resume_equivalence: bool,
) -> dict[str, Any]:
    if require_resume_equivalence and resume_equivalent is None:
        raise RuntimeError("resume equivalence requires real comparison evidence")
    state = validate_durable_workflow_state(result.final_state)
    plan = PersistedAdmittedWorkflowPlan.model_validate_json(
        json.dumps(state.get("admitted_plan"))
    )
    dependencies = {node.node_id: tuple(node.depends_on) for node in plan.nodes}
    order = _topological_order(dependencies)
    profile_by_node = {
        node.node_id: (
            "verifier"
            if any(
                binding.severity == "required"
                and binding.verifier_node_id == node.node_id
                for binding in plan.constraint_bindings
            )
            else "worker"
        )
        for node in plan.nodes
    }
    ledger_results: list[tuple[str, int, tuple[str, ...]]] = []
    for raw_slot in state["result_ledger"].values():
        slot = WorkflowResultSlot.model_validate_json(json.dumps(raw_slot))
        if slot.conflict is not None:
            raise RuntimeError("workflow evaluation refuses conflicting branch results")
        branch = next(iter(slot.variants_by_digest.values()))
        ledger_results.append(
            (branch.node_id, branch.execution_generation, tuple(branch.artifact_refs))
        )
    ledger_results.sort(key=lambda value: (value[1], order[value[0]], value[0]))
    trajectory = [
        {
            "node_id": node_id,
            "generation": generation,
            "profile": profile_by_node[node_id],
        }
        for node_id, generation, _refs in ledger_results
    ][:16_640]
    latest_generation = state["execution_generation_by_node"]
    refs_by_current_node = {
        node_id: list(refs)
        for node_id, generation, refs in ledger_results
        if latest_generation.get(node_id) == generation
    }
    deliverable_refs = {
        binding.deliverable: refs_by_current_node.get(binding.producer_node_id, [])
        for binding in plan.deliverable_bindings
    }
    repair_scope = sorted(
        {node_id for node_id, generation, _refs in ledger_results if generation > 0}
    )
    terminal_status = str(result.status)
    if terminal_status == "infrastructure_error":
        raise RuntimeError("workflow graph ended with infrastructure_error")
    return {
        "workflow_id": _stable_digest(state["workflow_id"]),
        "terminal_status": terminal_status,
        "plan": {
            "node_ids": [node.node_id for node in plan.nodes],
            "dependencies": {key: list(value) for key, value in dependencies.items()},
        },
        "trajectory": trajectory,
        "result_artifact_refs": list(state["result_artifact_refs"][:128]),
        "evaluation_evidence": {
            "constraint_ids": [
                binding.constraint_id for binding in plan.constraint_bindings
            ],
            "deliverable_artifact_refs": deliverable_refs,
            "repair_scope": repair_scope,
            "resume_equivalent": resume_equivalent,
        },
    }


async def run_workflow_experiment(
    client: Any,
    settings: WorkflowExperimentSettings,
) -> WorkflowExperimentResult:
    dataset, examples, originals = inspect_workflow_dataset(client)
    example_ids = tuple(example.id for example in examples)
    metadata = {
        "evaluation_mode": "durable_workflow_regression",
        "model": settings.model,
        "git_commit": settings.git_commit,
    }
    project = client.create_project(
        f"{settings.run_name}-{uuid4().hex[:8]}",
        reference_dataset_id=dataset.id,
        metadata=metadata,
        num_examples=len(examples),
        evaluator_keys=list(REQUIRED_WORKFLOW_FEEDBACK_KEYS),
    )

    async def target(inputs: dict[str, Any]) -> dict[str, Any]:
        current = _current_run_tree()
        example_id = str(_field(current, "reference_example_id") or "")
        if (
            current is None
            or not _field(current, "id")
            or not _field(current, "trace_id")
            or example_id not in example_ids
        ):
            raise RuntimeError("workflow target has no matching LangSmith RunTree")
        example = next(item for item in examples if item.id == example_id)
        invocation = settings.invocation_factory(
            inputs,
            example_id=example_id,
            reference_output=example.outputs,
        )
        return await run_workflow_example(
            invocation,
            require_resume_equivalence=(
                example.inputs.case_type == "interrupt_resume_equivalence"
            ),
        )

    native = await client.aevaluate(
        target,
        data=[originals[example.id] for example in examples],
        evaluators=list(langsmith_evaluators()),
        experiment=project,
        blocking=True,
        error_handling="log",
        max_concurrency=settings.max_concurrency,
        metadata=metadata,
    )
    rows = [row async for row in native]
    rows_by_example: dict[str, Any] = {}
    for row in rows:
        example_id = str(_field(_field(row, "example"), "id") or "")
        if example_id in rows_by_example:
            raise RuntimeError("LangSmith workflow Experiment returned duplicate row")
        rows_by_example[example_id] = row
    missing = sorted(set(example_ids) - set(rows_by_example))
    if missing:
        raise RuntimeError(f"LangSmith workflow Experiment missing rows {missing!r}")
    run_ids = tuple(
        _require_id(_field(rows_by_example[example_id], "run"), "run")
        for example_id in example_ids
    )
    native_dataset_id = await native.get_dataset_id()
    return WorkflowExperimentResult(
        native_result=native,
        experiment_id=str(native.experiment_id),
        experiment_name=str(native.experiment_name),
        experiment_url=str(native.url) if getattr(native, "url", None) else None,
        dataset_id=str(native_dataset_id or dataset.id),
        example_ids=example_ids,
        run_ids=run_ids,
        tree_requirements={
            example.id: WorkflowTreeRequirement(
                require_verifier=(example.inputs.case_type != "parallel_join"),
                worker_generations=tuple(
                    (node_id, 0)
                    for node_id in example.outputs.plan.node_ids
                    if (
                        example.inputs.case_type == "parallel_join"
                        or node_id
                        not in _terminal_nodes(example.outputs.plan.dependencies)
                    )
                ),
                repair_generations=(
                    tuple(
                        (node_id, 1)
                        for node_id in example.outputs.evaluation_contract.repair_scope
                    )
                    if example.inputs.case_type == "minimal_repair"
                    else ()
                ),
            )
            for example in examples
        },
    )


def audit_native_workflow_tree(
    runs: Sequence[Any],
    *,
    example_ids: tuple[str, ...],
    requirements: Mapping[str, WorkflowTreeRequirement] | None = None,
) -> NativeWorkflowTreeAudit:
    runs_by_id = {str(_field(run, "id")): run for run in runs}
    duplicate_ids = len(runs_by_id) != len(runs)
    roots_by_example = {
        example_id: [
            run
            for run in runs
            if str(_field(run, "reference_example_id") or "") == example_id
            and _field(run, "parent_run_id") is None
        ]
        for example_id in example_ids
    }
    claimed_run_ids: set[str] = set()
    for roots in roots_by_example.values():
        if len(roots) != 1:
            continue
        root_id = str(_field(roots[0], "id"))
        claimed_run_ids.update(
            str(_field(run, "id"))
            for run in runs
            if str(_field(run, "id")) == root_id
            or _is_descendant(run, root_id, runs_by_id)
        )
    workflow_names = {
        "DurableWorkflowGraph",
        "WorkflowPlanningSubgraph",
        "AssistantTurnGraph.planner",
        "WorkflowWorkerBranch",
        "AssistantTurnGraph.worker",
        "WorkflowVerifierBranch",
        "AssistantTurnGraph.verifier",
        "join_wave",
    }
    detached_workflow_runs = [
        run
        for run in runs
        if _field(run, "name") in workflow_names
        and str(_field(run, "id")) not in claimed_run_ids
    ]
    problems: dict[str, tuple[str, ...]] = {}
    root_ids: list[str] = []
    for example_id in example_ids:
        requirement = (requirements or {}).get(example_id, WorkflowTreeRequirement())
        item: list[str] = []
        roots = roots_by_example[example_id]
        if duplicate_ids:
            item.append("duplicate run id")
        if len(roots) != 1:
            item.append(f"root_run_count={len(roots)}")
            problems[example_id] = tuple(item)
            continue
        root = roots[0]
        root_id = str(_field(root, "id"))
        root_ids.append(root_id)
        trace_id = str(_field(root, "trace_id") or "")
        if not trace_id or _field(root, "run_type") != "chain":
            item.append("invalid experiment root")
        subtree = [
            run
            for run in runs
            if str(_field(run, "id")) == root_id
            or _is_descendant(run, root_id, runs_by_id)
        ]
        if any(str(_field(run, "trace_id") or "") != trace_id for run in subtree):
            item.append("trace mismatch")
        if any(
            str(_field(run, "reference_example_id") or "") not in ("", example_id)
            for run in subtree
        ):
            item.append("reference example mismatch")
        if detached_workflow_runs:
            item.append("detached workflow run detected")
        graphs = _children_named(subtree, root_id, "DurableWorkflowGraph")
        if len(graphs) != 1:
            item.append(f"DurableWorkflowGraph child count={len(graphs)}")
        else:
            graph = graphs[0]
            graph_id = str(_field(graph, "id"))
            metadata = _metadata(graph)
            if (
                _field(graph, "run_type") != "chain"
                or metadata.get("execution_engine") != "durable_workflow_graph"
                or not _digest(metadata.get("workflow_id"))
                or not _digest(metadata.get("thread_id"))
                or not _digest(metadata.get("run_id"))
            ):
                item.append("unsafe or missing DurableWorkflowGraph metadata")
            graph_subtree = [
                run for run in subtree if _is_descendant(run, graph_id, runs_by_id)
            ]
            _require_native_path(
                graph_subtree,
                runs_by_id,
                ("WorkflowPlanningSubgraph", "AssistantTurnGraph.planner"),
                item,
            )
            workers = [
                run
                for run in graph_subtree
                if _field(run, "name") == "WorkflowWorkerBranch"
            ]
            if not workers:
                item.append("missing WorkflowWorkerBranch")
            elif any(
                not any(
                    _field(child, "name") == "AssistantTurnGraph.worker"
                    and _is_descendant(child, str(_field(worker, "id")), runs_by_id)
                    for child in graph_subtree
                )
                for worker in workers
            ):
                item.append("worker branch missing AssistantTurnGraph.worker")
            if not any(_field(run, "name") == "join_wave" for run in graph_subtree):
                item.append("missing native join_wave")
            if requirement.require_verifier:
                _require_native_path(
                    graph_subtree,
                    runs_by_id,
                    ("WorkflowVerifierBranch", "AssistantTurnGraph.verifier"),
                    item,
                )
            for node_id, generation in requirement.repair_generations:
                matching = [
                    run
                    for run in graph_subtree
                    if _field(run, "name")
                    in {"WorkflowWorkerBranch", "WorkflowVerifierBranch"}
                    and _metadata(run).get("workflow_node_id") == node_id
                    and _metadata(run).get("workflow_generation") == generation
                    and _digest(_metadata(run).get("workflow_branch_run_id"))
                ]
                if not matching:
                    item.append(f"missing repair generation {node_id}:g{generation}")
            for node_id, generation in requirement.worker_generations:
                matching = [
                    run
                    for run in graph_subtree
                    if _field(run, "name") == "WorkflowWorkerBranch"
                    and _metadata(run).get("workflow_node_id") == node_id
                    and _metadata(run).get("workflow_generation") == generation
                    and _metadata(run).get("workflow_profile") == "worker"
                    and _digest(_metadata(run).get("workflow_branch_run_id"))
                ]
                if len(matching) != 1:
                    item.append(
                        f"worker generation count {node_id}:g{generation}={len(matching)}"
                    )
            if any(
                _field(run, "name") in workflow_names
                and _field(run, "run_type") != "chain"
                for run in graph_subtree
            ):
                item.append("workflow graph node/subgraph run_type must be chain")
            llm_runs = [
                run for run in graph_subtree if _field(run, "name") == "llm.chat"
            ]
            if any(_field(run, "run_type") != "llm" for run in llm_runs):
                item.append("llm.chat run_type must be llm")
            execute_tool_ids = {
                str(_field(run, "id"))
                for run in graph_subtree
                if _field(run, "name") == "execute_tool"
                and _field(run, "run_type") == "chain"
            }
            tool_runs = [
                run for run in graph_subtree if _field(run, "run_type") == "tool"
            ]
            if any(
                not any(
                    _is_descendant(run, execute_tool_id, runs_by_id)
                    for execute_tool_id in execute_tool_ids
                )
                for run in tool_runs
            ):
                item.append("governed tool outside execute_tool subtree")
            shadow_names = {
                "deep_research.workflow",
                "agent.runtime",
                "workflow.worker",
            }
            if any(_field(run, "name") in shadow_names for run in graph_subtree):
                item.append("canonical OTel shadow graph detected")
        if item:
            problems[example_id] = tuple(dict.fromkeys(item))
    return NativeWorkflowTreeAudit(
        complete=not problems,
        run_ids=tuple(root_ids),
        problems=problems,
    )


def wait_for_workflow_experiment_completeness(
    client: Any,
    *,
    experiment_id: str,
    example_ids: tuple[str, ...],
    requirements: Mapping[str, WorkflowTreeRequirement] | None = None,
    timeout_seconds: float = 180.0,
    poll_interval_seconds: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> WorkflowCompletenessResult:
    if timeout_seconds <= 0 or poll_interval_seconds <= 0:
        raise ValueError("completeness timeout and interval must be positive")
    deadline = clock() + timeout_seconds
    attempts = math.ceil(timeout_seconds / poll_interval_seconds) + 1
    latest: dict[str, list[str]] = {}
    for attempt in range(attempts):
        if attempt and clock() >= deadline:
            break
        try:
            result, latest = _audit_remote(
                client,
                experiment_id=experiment_id,
                example_ids=example_ids,
                requirements=requirements,
            )
        except LangSmithRateLimitError:
            result = None
            latest = {key: ["LangSmith query rate limited"] for key in example_ids}
        if result is not None:
            return result
        if attempt + 1 < attempts:
            remaining = max(0.0, deadline - clock())
            if remaining <= 0:
                break
            sleep(min(poll_interval_seconds, remaining))
    raise RuntimeError("LangSmith workflow Experiment incomplete: " + repr(latest))


def _audit_remote(
    client: Any,
    *,
    experiment_id: str,
    example_ids: tuple[str, ...],
    requirements: Mapping[str, WorkflowTreeRequirement] | None,
) -> tuple[WorkflowCompletenessResult | None, dict[str, list[str]]]:
    runs = list(
        client.list_runs(
            project_id=experiment_id,
            select=[
                "id",
                "parent_run_id",
                "name",
                "run_type",
                "reference_example_id",
                "trace_id",
                "inputs",
                "outputs",
                "extra",
            ],
        )
    )
    audit = audit_native_workflow_tree(
        runs, example_ids=example_ids, requirements=requirements
    )
    problems = {key: list(value) for key, value in audit.problems.items()}
    roots = {
        str(_field(run, "id")): str(_field(run, "reference_example_id") or "")
        for run in runs
        if _field(run, "parent_run_id") is None
    }
    feedback: dict[str, dict[str, Any]] = {key: {} for key in example_ids}
    if audit.run_ids:
        for item in client.list_feedback(run_ids=list(audit.run_ids)):
            example_id = roots.get(str(_field(item, "run_id")))
            key = str(_field(item, "key") or "")
            if example_id in feedback and key in REQUIRED_WORKFLOW_FEEDBACK_KEYS:
                if key in feedback[example_id]:
                    problems.setdefault(example_id, []).append(
                        f"duplicate feedback {key}"
                    )
                feedback[example_id][key] = _field(item, "score")
    for example_id in example_ids:
        missing = [
            key
            for key in REQUIRED_WORKFLOW_FEEDBACK_KEYS
            if feedback[example_id].get(key) is None
        ]
        if missing:
            problems.setdefault(example_id, []).append(
                "missing feedback " + repr(missing)
            )
    if problems:
        return None, problems
    return WorkflowCompletenessResult(audit.run_ids, feedback), {}


def _topological_order(dependencies: Mapping[str, tuple[str, ...]]) -> dict[str, int]:
    remaining = {key: set(value) for key, value in dependencies.items()}
    ordered: list[str] = []
    while remaining:
        ready = sorted(key for key, value in remaining.items() if not value)
        if not ready:
            raise RuntimeError("admitted plan is not a DAG")
        ordered.extend(ready)
        for key in ready:
            remaining.pop(key)
        for value in remaining.values():
            value.difference_update(ready)
    return {node_id: index for index, node_id in enumerate(ordered)}


def _terminal_nodes(dependencies: Mapping[str, tuple[str, ...]]) -> set[str]:
    depended_on = {parent for parents in dependencies.values() for parent in parents}
    return set(dependencies) - depended_on


def _children_named(runs: Sequence[Any], parent_id: str, name: str) -> list[Any]:
    return [
        run
        for run in runs
        if str(_field(run, "parent_run_id") or "") == parent_id
        and _field(run, "name") == name
    ]


def _require_native_path(
    runs: Sequence[Any],
    runs_by_id: dict[str, Any],
    names: tuple[str, str],
    problems: list[str],
) -> None:
    parents = [run for run in runs if _field(run, "name") == names[0]]
    if not parents:
        problems.append(f"missing {names[0]}")
        return
    if not any(
        _field(child, "name") == names[1]
        and _is_descendant(child, str(_field(parent, "id")), runs_by_id)
        for parent in parents
        for child in runs
    ):
        problems.append(f"{names[0]} missing {names[1]}")


def _is_descendant(run: Any, ancestor_id: str, runs_by_id: dict[str, Any]) -> bool:
    parent_id = str(_field(run, "parent_run_id") or "")
    visited: set[str] = set()
    while parent_id and parent_id not in visited:
        if parent_id == ancestor_id:
            return True
        visited.add(parent_id)
        parent = runs_by_id.get(parent_id)
        if parent is None:
            return False
        parent_id = str(_field(parent, "parent_run_id") or "")
    return False


def _metadata(run: Any) -> dict[str, Any]:
    extra = _field(run, "extra")
    metadata = extra.get("metadata") if isinstance(extra, dict) else None
    return dict(metadata) if isinstance(metadata, dict) else {}


def _digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _stable_digest(value: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_id(value: Any, label: str) -> str:
    result = str(_field(value, "id") or "")
    if not result:
        raise RuntimeError(f"LangSmith workflow Experiment {label} has no id")
    return result


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _current_run_tree() -> Any | None:
    from langsmith.run_helpers import get_current_run_tree

    return get_current_run_tree()


__all__ = [
    "DirectWorkflowInvocation",
    "NativeWorkflowTreeAudit",
    "REQUIRED_WORKFLOW_FEEDBACK_KEYS",
    "WorkflowCompletenessResult",
    "WorkflowExperimentResult",
    "WorkflowExperimentSettings",
    "WorkflowTreeRequirement",
    "audit_native_workflow_tree",
    "inspect_workflow_dataset",
    "project_workflow_result",
    "run_workflow_example",
    "run_workflow_experiment",
    "wait_for_workflow_experiment_completeness",
    "workflow_regression_equivalence_evidence",
]
