"""Native nodes and subgraphs for DurableWorkflowGraph DAG execution."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Mapping, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.errors import NodeError, NodeTimeoutError
from langgraph.types import Command, RetryPolicy, Send, TimeoutPolicy
from pydantic import ValidationError

from assistant_agent.identity import RequestIdentity
from assistant_agent.providers.provider_errors import ProviderAdapterError
from assistant_agent.providers.provider_policy import DEFAULT_RETRYABLE_PROVIDER_ERRORS
from assistant_agent.runtime.assistant_graph_profiles import (
    ProfileInvocationInput,
    profile_input_adapter,
    profile_output_adapter,
)
from assistant_agent.runtime.assistant_graph_state import AssistantTurnState
from assistant_agent.runtime.graph_runtime import GraphRuntimeContext
from assistant_agent.workflows.graph_context import (
    WorkflowGraphRuntimeContext,
)
from assistant_agent.workflows.graph_state import (
    DurableWorkflowState,
    PersistedAdmittedWorkflowNode,
    PersistedAdmittedWorkflowPlan,
    PersistedWorkflowBudget,
    PersistedWorkflowBudgetSlice,
    PersistedWorkflowInputRequest,
    WorkflowBranchResult,
    WorkflowGraphError,
    WorkflowGraphStateConflict,
    WorkflowProfileAssignment,
    WorkflowResultSlot,
    WorkflowWorkerControl,
    WorkflowVerifierControl,
    latest_results,
    ledger_update,
    merge_result_ledger,
)
from assistant_agent.workflows.constraints import minimal_repair_closure
from assistant_agent.workflows.transitions import WorkflowTransitionRejected


MAX_WORKFLOW_REPAIR_ROUNDS = 3
WORKFLOW_TRANSIENT_RETRY_POLICY = RetryPolicy(
    initial_interval=0.1,
    backoff_factor=2.0,
    max_interval=1.0,
    max_attempts=3,
    jitter=False,
    retry_on=lambda error: is_transient_workflow_node_error(error),
)
WORKFLOW_NODE_TIMEOUT = TimeoutPolicy(
    run_timeout=30.0,
    idle_timeout=10.0,
    refresh_on="auto",
)


class WorkflowProfileBranchState(TypedDict, total=False):
    assignment: WorkflowProfileAssignment
    worker_child_state: AssistantTurnState
    worker_control: WorkflowWorkerControl
    verifier_child_state: AssistantTurnState
    result_ledger: Annotated[dict[str, WorkflowResultSlot], merge_result_ledger]


class WorkflowBranchOutput(TypedDict):
    result_ledger: dict[str, WorkflowResultSlot]


def _runtime_context(value: object) -> WorkflowGraphRuntimeContext:
    if not isinstance(value, WorkflowGraphRuntimeContext):
        raise TypeError("workflow graph runtime context is required")
    return value


def _assignment(state: Mapping[str, object]) -> WorkflowProfileAssignment:
    value = state.get("assignment")
    if isinstance(value, WorkflowProfileAssignment):
        return value
    return WorkflowProfileAssignment.model_validate_json(json.dumps(value))


def prepare_worker_child_node(
    state: WorkflowProfileBranchState,
    runtime: Runtime[WorkflowGraphRuntimeContext],
) -> dict[str, object]:
    assignment = _assignment(state)
    if assignment.profile != "worker":
        raise ValueError("worker branch requires worker assignment")
    context = _runtime_context(runtime.context)
    registry = context.services.tool_registry
    child = profile_input_adapter(
        {
            "user_id": assignment.user_id,
            "session_id": assignment.session_id,
            "agent_id": assignment.agent_id,
            "run_id": assignment.run_id,
            "trace_id": assignment.trace_id,
            "registered_tool_specs": registry.list_specs(),
            "available_tool_names": assignment.available_tool_names,
        },
        ProfileInvocationInput(
            profile="worker",
            assignment_ref=assignment.assignment_ref,
            objective=assignment.objective,
            constraints=assignment.constraints,
            capability_refs=assignment.capability_refs,
            explicit_tool_allowlist=assignment.explicit_tool_allowlist,
        ),
        model_call_limit=assignment.budget_slice.model_calls,
        tool_call_limit=assignment.budget_slice.tool_calls,
    )
    # Validate the complete owner and Tool scope before the first child node.
    context.branch_context_factory.context_for_assignment(
        assignment,
        child,
        context.services,
    )
    return {
        "assignment": assignment.model_dump(mode="json"),
        "worker_child_state": child,
    }


def worker_child_runtime_context(
    state: Mapping[str, object],
    child_state: AssistantTurnState,
    runtime_context: object,
) -> GraphRuntimeContext:
    assignment = _assignment(state)
    context = _runtime_context(runtime_context)
    return context.branch_context_factory.context_for_assignment(
        assignment,
        child_state,
        context.services,
    )


def _control_from_child(
    child: AssistantTurnState,
) -> tuple[WorkflowWorkerControl, str]:
    projected = profile_output_adapter(child)
    response = projected.response
    message = response.message if response is not None else ""
    if projected.status != "completed":
        return (
            WorkflowWorkerControl(
                outcome="failed",
                summary="Worker child did not complete.",
                error_code=f"worker_child_{projected.status}",
            ),
            message,
        )
    try:
        payload = json.loads(message)
    except (TypeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, Mapping) and "workflow_control" in payload:
        envelope = dict(payload)
        if set(envelope) != {"workflow_control"}:
            raise ValueError("worker control envelope contains extra fields")
        raw = envelope["workflow_control"]
        if not isinstance(raw, Mapping):
            raise ValueError("worker control must be an object")
        control = WorkflowWorkerControl.model_validate_json(json.dumps(raw))
        return control, ""
    raise ValueError("worker response must contain strict workflow_control")


def parse_branch_control_node(
    state: WorkflowProfileBranchState,
    runtime: Runtime[WorkflowGraphRuntimeContext],
) -> WorkflowBranchOutput:
    assignment = _assignment(state)
    child = cast(AssistantTurnState, state["worker_child_state"])
    try:
        control, content = _control_from_child(child)
    except (TypeError, ValueError, ValidationError):
        control = WorkflowWorkerControl(
            outcome="failed",
            summary="Worker returned invalid structured control.",
            error_code="workflow_worker_control_invalid",
        )
        content = ""
    projected = profile_output_adapter(child)
    artifact_refs = list(projected.artifact_refs)
    input_request = None
    status: Literal["succeeded", "blocked", "failed"]
    error_code = control.error_code
    if control.outcome == "completed":
        status = "succeeded"
        if content:
            context = _runtime_context(runtime.context)
            ref = context.artifact_store.write_text(
                identity=RequestIdentity.for_user(
                    user_id=assignment.user_id,
                    agent_id=assignment.agent_id,
                    session_id=assignment.session_id,
                ),
                workflow_id=assignment.workflow_id,
                kind=assignment.acceptance_contract.output.artifact_type,
                text=content,
                producer_work_item_id=assignment.node_id,
            )
            artifact_refs.append(ref.uri)
    elif control.outcome == "blocked":
        status = "blocked"
        input_request = PersistedWorkflowInputRequest(
            required_fields=control.required_fields,
            prompt_code=cast(str, control.prompt_code),
            safe_prompt=cast(str, control.safe_prompt),
        )
    else:
        status = "failed"
    result = WorkflowBranchResult(
        node_id=assignment.node_id,
        execution_generation=assignment.execution_generation,
        profile="worker",
        status=status,
        summary=control.summary,
        artifact_refs=tuple(dict.fromkeys(artifact_refs)),
        error_code=error_code,
        input_request=input_request,
        model_calls_used=int(child.get("assistant_iterations", 0)),
        tool_calls_used=int(child.get("tool_calls_used", 0)),
    )
    return {"result_ledger": ledger_update(result)}


def build_worker_branch_subgraph(*, worker_graph: Any) -> Any:
    if getattr(worker_graph, "name", None) != "AssistantTurnGraph.worker":
        raise ValueError("worker branch requires compiled AssistantTurnGraph.worker")
    builder = StateGraph(
        WorkflowProfileBranchState,
        context_schema=WorkflowGraphRuntimeContext,
        output_schema=WorkflowBranchOutput,
    )
    builder.add_node("prepare_child", prepare_worker_child_node)
    builder.add_node("worker_profile", worker_graph)
    builder.add_node("parse_branch_control", parse_branch_control_node)
    builder.add_edge(START, "prepare_child")
    builder.add_edge("prepare_child", "worker_profile")
    builder.add_edge("worker_profile", "parse_branch_control")
    builder.add_edge("parse_branch_control", END)
    return builder.compile(checkpointer=None, name="WorkflowWorkerBranch")


def prepare_verifier_child_node(
    state: WorkflowProfileBranchState,
    runtime: Runtime[WorkflowGraphRuntimeContext],
) -> dict[str, object]:
    assignment = _assignment(state)
    if assignment.profile != "verifier":
        raise ValueError("verifier branch requires verifier assignment")
    context = _runtime_context(runtime.context)
    registry = context.services.tool_registry
    child = profile_input_adapter(
        {
            "user_id": assignment.user_id,
            "session_id": assignment.session_id,
            "agent_id": assignment.agent_id,
            "run_id": assignment.run_id,
            "trace_id": assignment.trace_id,
            "registered_tool_specs": registry.list_specs(),
            "available_tool_names": assignment.available_tool_names,
        },
        ProfileInvocationInput(
            profile="verifier",
            assignment_ref=assignment.assignment_ref,
            objective=assignment.objective,
            constraints=assignment.constraints,
            capability_refs=assignment.capability_refs,
            explicit_tool_allowlist=assignment.explicit_tool_allowlist,
        ),
        model_call_limit=assignment.budget_slice.model_calls,
        tool_call_limit=assignment.budget_slice.tool_calls,
    )
    context.branch_context_factory.context_for_assignment(assignment, child, context.services)
    return {"assignment": assignment.model_dump(mode="json"), "verifier_child_state": child}


def verifier_child_runtime_context(
    state: Mapping[str, object],
    child_state: AssistantTurnState,
    runtime_context: object,
) -> GraphRuntimeContext:
    assignment = _assignment(state)
    return _runtime_context(runtime_context).branch_context_factory.context_for_assignment(
        assignment, child_state, _runtime_context(runtime_context).services
    )


def parse_verifier_control_node(
    state: WorkflowProfileBranchState,
    runtime: Runtime[WorkflowGraphRuntimeContext],
) -> WorkflowBranchOutput:
    del runtime
    assignment = _assignment(state)
    child = cast(AssistantTurnState, state["verifier_child_state"])
    projected = profile_output_adapter(child)
    message = projected.response.message if projected.response is not None else ""
    try:
        envelope = json.loads(message)
        if not isinstance(envelope, Mapping) or set(envelope) != {"workflow_verification"}:
            raise ValueError
        control = WorkflowVerifierControl.model_validate_json(
            json.dumps(envelope["workflow_verification"])
        )
    except (TypeError, ValueError, json.JSONDecodeError, ValidationError):
        control = WorkflowVerifierControl(
            status="failed",
            summary="Verifier returned invalid structured control.",
            error_code="workflow_verifier_control_invalid",
        )
    status: Literal["succeeded", "blocked", "repair", "failed"]
    input_request = None
    if control.status == "verified":
        status = "succeeded"
    elif control.status == "blocked":
        status = "blocked"
        input_request = PersistedWorkflowInputRequest(
            required_fields=control.required_fields,
            prompt_code=cast(str, control.prompt_code),
            safe_prompt=cast(str, control.safe_prompt),
        )
    else:
        status = cast(Literal["repair", "failed"], control.status)
    result = WorkflowBranchResult(
        node_id=assignment.node_id,
        execution_generation=assignment.execution_generation,
        profile="verifier",
        status=status,
        summary=control.summary,
        artifact_refs=tuple(projected.artifact_refs),
        error_code=control.error_code,
        repair_node_ids=control.repair_node_ids,
        input_request=input_request,
        model_calls_used=int(child.get("assistant_iterations", 0)),
        tool_calls_used=int(child.get("tool_calls_used", 0)),
    )
    return {"result_ledger": ledger_update(result)}


def build_verifier_branch_subgraph(*, verifier_graph: Any) -> Any:
    if getattr(verifier_graph, "name", None) != "AssistantTurnGraph.verifier":
        raise ValueError("verifier branch requires compiled AssistantTurnGraph.verifier")
    builder = StateGraph(
        WorkflowProfileBranchState,
        context_schema=WorkflowGraphRuntimeContext,
        output_schema=WorkflowBranchOutput,
    )
    builder.add_node("prepare_child", prepare_verifier_child_node)
    builder.add_node("verifier_profile", verifier_graph)
    builder.add_node("parse_verifier_control", parse_verifier_control_node)
    builder.add_edge(START, "prepare_child")
    builder.add_edge("prepare_child", "verifier_profile")
    builder.add_edge("verifier_profile", "parse_verifier_control")
    builder.add_edge("parse_verifier_control", END)
    return builder.compile(checkpointer=None, name="WorkflowVerifierBranch")


def _plan(state: Mapping[str, object]) -> PersistedAdmittedWorkflowPlan:
    value = state.get("admitted_plan")
    if isinstance(value, PersistedAdmittedWorkflowPlan):
        return value
    return PersistedAdmittedWorkflowPlan.model_validate_json(json.dumps(value))


def _branch_assignment(
    state: Mapping[str, object],
    node: PersistedAdmittedWorkflowNode,
    *,
    input_artifact_refs: tuple[str, ...],
    context: WorkflowGraphRuntimeContext,
    wave_size: int,
) -> WorkflowProfileAssignment:
    identity = state["identity"]
    identity_map = (
        identity.model_dump(mode="python")
        if hasattr(identity, "model_dump")
        else identity
    )
    if not isinstance(identity_map, Mapping):
        raise ValueError("workflow identity is invalid")
    registry = context.services.tool_registry
    # Registry inventory is not a grant. Deep Research currently persists no
    # local Tool allowlist, so both Provider exposure and Executor scope remain
    # empty until a trusted definition explicitly admits one.
    eligible: tuple[str, ...] = ()
    budget_value = state["budget"]
    budget = (
        budget_value
        if isinstance(budget_value, PersistedWorkflowBudget)
        else PersistedWorkflowBudget.model_validate_json(json.dumps(budget_value))
    )
    model_calls = max(1, min(5, budget.model_calls_remaining // max(1, wave_size)))
    tool_calls = min(5, budget.tool_calls_remaining // max(1, wave_size))
    generation = int(
        cast(Mapping[str, int], state["execution_generation_by_node"])[node.node_id]
    )
    plan = _plan(state)
    is_verifier = any(
        binding.verifier_node_id == node.node_id
        for binding in plan.constraint_bindings
        if binding.severity == "required"
    )
    constraints = tuple(
        binding.statement
        for binding in plan.constraint_bindings
        if node.node_id in binding.owner_node_ids
        or node.node_id == binding.verifier_node_id
    )
    return WorkflowProfileAssignment.create(
        profile="verifier" if is_verifier else "worker",
        user_id=str(identity_map["user_id"]),
        session_id=str(identity_map["session_id"]),
        agent_id=str(identity_map["agent_id"]),
        workflow_id=str(state["workflow_id"]),
        workflow_thread_id=str(state["workflow_thread_id"]),
        node_id=node.node_id,
        execution_generation=generation,
        run_id=f"{state['invocation_run_id']}:{node.node_id}:g{generation}",
        trace_id=f"{state['invocation_trace_id']}:{node.node_id}:g{generation}",
        objective=node.objective,
        constraints=constraints,
        input_artifact_refs=input_artifact_refs,
        acceptance_contract=node.acceptance_contract,
        capability_refs=(),
        explicit_tool_allowlist=eligible,
        available_tool_names=eligible,
        tool_scope_ref=cast(str, registry.generation),
        budget_slice=PersistedWorkflowBudgetSlice(
            model_calls=model_calls,
            tool_calls=tool_calls,
            workflow_quanta=1,
        ),
    )


def prepare_next_wave_node(
    state: DurableWorkflowState,
    runtime: Runtime[WorkflowGraphRuntimeContext],
) -> dict[str, object]:
    if state["status"] == "failed":
        return {"active_wave": ()}
    plan = _plan(state)
    generations = cast(Mapping[str, int], state["execution_generation_by_node"])
    try:
        results = latest_results(state["result_ledger"], generations)
    except WorkflowGraphStateConflict as exc:
        return {
            "status": "failed",
            "phase": "failed",
            "active_wave": (),
            "errors": (
                WorkflowGraphError(code="workflow_result_conflict", message=str(exc)),
            ),
        }
    completed = {
        node_id for node_id, result in results.items() if result.status == "succeeded"
    }
    if any(result.status != "succeeded" for result in results.values()):
        return {"active_wave": ()}
    node_by_id = {node.node_id: node for node in plan.nodes}
    ready = tuple(
        node
        for node in sorted(plan.nodes, key=lambda item: item.node_id)
        if node.node_id not in results and set(node.depends_on).issubset(completed)
    )
    if not ready:
        if len(completed) == len(plan.nodes):
            return {"active_wave": (), "phase": "publishing"}
        return {
            "status": "failed",
            "phase": "failed",
            "active_wave": (),
            "errors": (
                WorkflowGraphError(
                    code="workflow_dag_stalled",
                    message="Admitted DAG has no ready node and is not complete.",
                ),
            ),
        }
    context = _runtime_context(runtime.context)
    assignments = []
    for node in ready:
        dependency_refs = tuple(
            ref
            for dependency in node.depends_on
            for ref in results[dependency].artifact_refs
        )
        refs = tuple(dict.fromkeys((*node.input_artifact_refs, *dependency_refs)))
        assignments.append(
            _branch_assignment(
                state,
                node_by_id[node.node_id],
                input_artifact_refs=refs,
                context=context,
                wave_size=len(ready),
            )
        )
    wave_ids = tuple(item.node_id for item in assignments)
    history = tuple(tuple(item) for item in state.get("wave_history", ()))
    if not history or history[-1] != wave_ids:
        history = (*history, wave_ids)
    return {
        "active_wave": tuple(assignments),
        "wave_history": history,
        "status": "running",
        "phase": "executing",
    }


def route_next_wave(
    state: DurableWorkflowState,
) -> list[Send] | Literal["publish", "fail"]:
    if state["status"] == "failed":
        return "fail"
    branches = tuple(state["active_wave"])
    if branches:
        return [
            Send(
                "run_verifier" if assignment.profile == "verifier" else "run_worker",
                {
                    "assignment": assignment.model_dump(mode="json")
                    if hasattr(assignment, "model_dump")
                    else assignment
                },
            )
            for assignment in branches
        ]
    try:
        results = latest_results(
            state["result_ledger"], state["execution_generation_by_node"]
        )
    except WorkflowGraphStateConflict:
        return "fail"
    if len(results) == len(state["execution_generation_by_node"]) and all(
        result.status == "succeeded" for result in results.values()
    ):
        return "publish"
    return "fail"


def join_wave_node(state: DurableWorkflowState) -> dict[str, object]:
    assignments = tuple(
        item
        if isinstance(item, WorkflowProfileAssignment)
        else WorkflowProfileAssignment.model_validate_json(json.dumps(item))
        for item in state["active_wave"]
    )
    try:
        results = latest_results(
            state["result_ledger"], state["execution_generation_by_node"]
        )
    except WorkflowGraphStateConflict as exc:
        return {
            "status": "failed",
            "phase": "failed",
            "active_wave": (),
            "errors": (
                WorkflowGraphError(code="workflow_result_conflict", message=str(exc)),
            ),
        }
    missing = [item.node_id for item in assignments if item.node_id not in results]
    if missing:
        return {
            "status": "failed",
            "phase": "failed",
            "active_wave": (),
            "errors": (
                WorkflowGraphError(
                    code="workflow_wave_incomplete",
                    message="Wave join is missing branch results: "
                    + ", ".join(sorted(missing)),
                ),
            ),
        }
    wave_results = [results[item.node_id] for item in assignments]
    failed = next(
        (result for result in wave_results if result.status == "failed"), None
    )
    blocked = next(
        (result for result in wave_results if result.status == "blocked"), None
    )
    budget_value = state["budget"]
    budget = (
        budget_value
        if isinstance(budget_value, PersistedWorkflowBudget)
        else PersistedWorkflowBudget.model_validate_json(json.dumps(budget_value))
    )
    used_model = sum(result.model_calls_used for result in wave_results)
    used_tool = sum(result.tool_calls_used for result in wave_results)
    used_quanta = len(wave_results)
    if (
        used_model > budget.model_calls_remaining
        or used_tool > budget.tool_calls_remaining
        or used_quanta > budget.workflow_quanta_remaining
    ):
        failed = WorkflowBranchResult(
            node_id=assignments[0].node_id,
            execution_generation=assignments[0].execution_generation,
            profile="worker",
            status="failed",
            summary="Workflow budget was exceeded.",
            error_code="workflow_budget_exceeded",
        )
    updated_budget = budget.model_copy(
        update={
            "model_calls_remaining": max(0, budget.model_calls_remaining - used_model),
            "tool_calls_remaining": max(0, budget.tool_calls_remaining - used_tool),
            "workflow_quanta_remaining": max(
                0, budget.workflow_quanta_remaining - used_quanta
            ),
        }
    )
    update: dict[str, object] = {
        "active_wave": (),
        "budget": updated_budget.model_dump(mode="json"),
        "result_artifact_refs": tuple(
            dict.fromkeys(
                (
                    *state.get("result_artifact_refs", ()),
                    *(ref for result in wave_results for ref in result.artifact_refs),
                )
            )
        ),
    }
    if any(item.profile == "verifier" for item in assignments):
        update["phase"] = "verifying"
    if failed is not None:
        update.update(
            status="failed",
            phase="failed",
            errors=(
                WorkflowGraphError(
                    code=failed.error_code or "workflow_worker_failed",
                    message=failed.summary or "Workflow worker failed.",
                    node_id=failed.node_id,
                    execution_generation=failed.execution_generation,
                ),
            ),
        )
    elif blocked is not None:
        update.update(status="blocked", phase="waiting_input")
    return update


def route_after_join(
    state: DurableWorkflowState,
) -> Literal["next_wave", "verification", "fail"]:
    if state["status"] in {"failed", "blocked"}:
        return "fail"
    return "verification" if state["phase"] == "verifying" else "next_wave"


def _verifier_ids(plan: PersistedAdmittedWorkflowPlan) -> frozenset[str]:
    return frozenset(
        binding.verifier_node_id
        for binding in plan.constraint_bindings
        if binding.severity == "required" and binding.verifier_node_id is not None
    )


def _failure_command(
    code: str,
    message: str,
    *,
    node_id: str | None = None,
    execution_generation: int | None = None,
) -> Command[Literal["fail"]]:
    return Command(
        update={
            "status": "failed",
            "phase": "failed",
            "active_wave": (),
            "errors": (
                WorkflowGraphError(
                    code=code,
                    message=message,
                    node_id=node_id,
                    execution_generation=execution_generation,
                ),
            ),
        },
        goto="fail",
    )


def decide_verification_node(
    state: DurableWorkflowState,
) -> Command[Literal["prepare_wave", "publish", "join_wave", "fail"]]:
    plan = _plan(state)
    try:
        results = latest_results(
            state["result_ledger"], state["execution_generation_by_node"]
        )
    except WorkflowGraphStateConflict as exc:
        return _failure_command("workflow_result_conflict", str(exc))
    verifier_ids = _verifier_ids(plan)
    decisions = [results[node_id] for node_id in sorted(verifier_ids) if node_id in results]
    if not decisions:
        return _failure_command(
            "workflow_verifier_missing", "Required verifier has no current result."
        )
    blocked = next((item for item in decisions if item.status == "blocked"), None)
    if blocked is not None:
        return Command(goto="join_wave")
    failed = next((item for item in decisions if item.status == "failed"), None)
    if failed is not None:
        return _failure_command(
            failed.error_code or "workflow_verifier_failed",
            failed.summary or "Workflow verifier failed.",
            node_id=failed.node_id,
            execution_generation=failed.execution_generation,
        )
    repair = next((item for item in decisions if item.status == "repair"), None)
    if repair is not None:
        try:
            closure = minimal_repair_closure(
                plan,
                repair.repair_node_ids,
                repair.node_id,
                current_result_ids=results,
            )
        except ValueError:
            return _failure_command(
                "invalid_repair_scope",
                "Verifier requested a repair outside its admitted ancestors.",
                node_id=repair.node_id,
                execution_generation=repair.execution_generation,
            )
        budget_value = state["budget"]
        budget = (
            budget_value
            if isinstance(budget_value, PersistedWorkflowBudget)
            else PersistedWorkflowBudget.model_validate_json(json.dumps(budget_value))
        )
        generations = dict(state["execution_generation_by_node"])
        if (
            int(state["repair_round"]) >= MAX_WORKFLOW_REPAIR_ROUNDS
            or len(closure) > budget.model_calls_remaining
            or len(closure) > budget.workflow_quanta_remaining
            or any(generations[node_id] >= 64 for node_id in closure)
        ):
            return _failure_command(
                "repair_budget_exhausted",
                "Workflow repair budget is exhausted.",
                node_id=repair.node_id,
                execution_generation=repair.execution_generation,
            )
        for node_id in closure:
            generations[node_id] += 1
        return Command(
            update={
                "execution_generation_by_node": generations,
                "repair_round": int(state["repair_round"]) + 1,
                "status": "running",
                "phase": "repairing",
                "active_wave": (),
            },
            goto="prepare_wave",
        )
    if any(item.status != "succeeded" for item in decisions):
        return _failure_command(
            "workflow_verification_rejected", "Workflow verification was rejected."
        )
    deliverable_refs: list[str] = []
    for binding in plan.deliverable_bindings:
        producer = results.get(binding.producer_node_id)
        if producer is None or producer.status != "succeeded":
            return _failure_command(
                "workflow_deliverable_unverified",
                "A required deliverable has no current successful producer result.",
                node_id=binding.producer_node_id,
            )
        deliverable_refs.extend(producer.artifact_refs)
    return Command(
        update={
            "phase": "publishing",
            "result_artifact_refs": tuple(dict.fromkeys(deliverable_refs)),
        },
        goto="publish",
    )


def is_transient_workflow_node_error(error: BaseException) -> bool:
    if isinstance(error, ProviderAdapterError):
        return error.code in DEFAULT_RETRYABLE_PROVIDER_ERRORS
    if isinstance(
        error,
        (
            PermissionError,
            WorkflowTransitionRejected,
            WorkflowGraphStateConflict,
            ValueError,
        ),
    ):
        return False
    return isinstance(error, (OSError, NodeTimeoutError))


def workflow_node_error_handler(
    state: Mapping[str, object],
    error: NodeError,
) -> Command[Literal["join_wave", "fail"]]:
    try:
        assignment = _assignment(state)
    except (TypeError, ValueError, ValidationError):
        return _failure_command(
            "workflow_node_failure_unattributed",
            "Workflow branch failed before its assignment could be recovered.",
        )
    code = (
        "workflow_node_timeout"
        if isinstance(error.error, NodeTimeoutError)
        else "workflow_node_execution_failed"
    )
    result = WorkflowBranchResult(
        node_id=assignment.node_id,
        execution_generation=assignment.execution_generation,
        profile=cast(Literal["worker", "verifier"], assignment.profile),
        status="failed",
        summary="Workflow branch execution failed.",
        error_code=code,
    )
    return Command(update={"result_ledger": ledger_update(result)}, goto="join_wave")


def publish_node(state: DurableWorkflowState) -> dict[str, object]:
    return {"status": "completed", "phase": "completed", "active_wave": ()}


def fail_node(state: DurableWorkflowState) -> dict[str, object]:
    if state["status"] == "blocked":
        return {"active_wave": ()}
    if state["status"] != "failed":
        return {
            "status": "failed",
            "phase": "failed",
            "active_wave": (),
            "errors": (
                WorkflowGraphError(
                    code="workflow_dag_stalled",
                    message="Workflow cannot advance from its current execution facts.",
                ),
            ),
        }
    return {"active_wave": ()}


__all__ = [
    "WorkflowBranchOutput",
    "WorkflowProfileBranchState",
    "build_worker_branch_subgraph",
    "build_verifier_branch_subgraph",
    "decide_verification_node",
    "fail_node",
    "join_wave_node",
    "parse_branch_control_node",
    "prepare_next_wave_node",
    "prepare_worker_child_node",
    "publish_node",
    "route_after_join",
    "route_next_wave",
    "minimal_repair_closure",
    "WORKFLOW_NODE_TIMEOUT",
    "WORKFLOW_TRANSIENT_RETRY_POLICY",
    "is_transient_workflow_node_error",
    "workflow_node_error_handler",
    "worker_child_runtime_context",
]
