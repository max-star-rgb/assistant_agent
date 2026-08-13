"""Native planner-profile subgraph and deterministic Workflow v2 admission."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.runtime.assistant_graph_app import AssistantTurnGraphApp
from assistant_agent.runtime.assistant_graph_profiles import (
    ProfileInvocationInput,
    profile_input_adapter,
    profile_output_adapter,
)
from assistant_agent.runtime.assistant_graph_state import AssistantTurnState
from assistant_agent.runtime.graph_runtime import GraphRuntimeContext
from assistant_agent.workflows.definitions import (
    WorkflowPlanMaterializationInput,
    materialize_runtime_plan,
)
from assistant_agent.workflows.graph_context import WorkflowGraphRuntimeContext
from assistant_agent.workflows.graph_state import (
    DurableWorkflowState,
    PersistedWorkflowBudgetSlice,
    PersistedWorkflowIdentity,
    PersistedWorkflowSubmission,
    PersistedWorkflowStepAcceptanceContract,
    WorkflowGraphError,
    WorkflowProfileAssignment,
    persist_admitted_workflow_plan,
    validate_durable_workflow_state,
)
from assistant_agent.workflows.models import WorkflowPlannerProposal, WorkflowPlanV2Proposal
from assistant_agent.workflows.agent_runtime import (
    parse_workflow_plan_response,
    render_workflow_planner_prompt,
)
from assistant_agent.workflows.transitions import (
    WorkflowTransitionRejected,
    validate_plan_dag,
)


class PlannerProfileResult(BaseModel):
    """Bounded semantic result projected out of AssistantTurnGraph.planner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["succeeded", "failed"]
    plan_proposal: WorkflowPlannerProposal | None = None
    model_calls_used: int = Field(ge=0)
    tool_calls_used: int = Field(ge=0)
    error_code: str | None = Field(default=None, max_length=160)


class PlanningSubgraphState(DurableWorkflowState, total=False):
    planner_assignment: WorkflowProfileAssignment
    planner_child_state: AssistantTurnState
    planner_result: PlannerProfileResult | None


def _require_context(value: object) -> WorkflowGraphRuntimeContext:
    if not isinstance(value, WorkflowGraphRuntimeContext):
        raise RuntimeError("workflow planning requires WorkflowGraphRuntimeContext")
    return value


def _validate_context_identity(
    state: Mapping[str, object],
    context: WorkflowGraphRuntimeContext,
) -> None:
    identity = state["identity"]
    if not isinstance(identity, Mapping):
        raise ValueError("workflow identity is missing")
    runtime_identity = context.services.workflow_identity
    if (
        identity.get("user_id") != runtime_identity.user_id
        or identity.get("session_id") != runtime_identity.session_id
        or identity.get("agent_id") != runtime_identity.agent_id
        or identity.get("workflow_thread_id") != runtime_identity.workflow_thread_id
    ):
        raise ValueError("workflow planning runtime identity mismatch")


def prepare_planner_profile_node(
    state: PlanningSubgraphState,
    runtime: Runtime[WorkflowGraphRuntimeContext],
) -> dict[str, object]:
    parent = validate_durable_workflow_state(state)
    context = _require_context(runtime.context)
    _validate_context_identity(parent, context)
    identity = PersistedWorkflowIdentity.model_validate_json(
        json.dumps(parent["identity"])
    )
    registry = context.services.tool_registry
    if not registry.sealed or registry.generation is None:
        raise ValueError("workflow planner requires a sealed Tool registry")
    submission = PersistedWorkflowSubmission.model_validate_json(
        json.dumps(parent["submission"])
    )
    assignment = WorkflowProfileAssignment.create(
        profile="planner",
        user_id=identity.user_id,
        session_id=identity.session_id,
        agent_id=identity.agent_id,
        workflow_id=parent["workflow_id"],
        workflow_thread_id=parent["workflow_thread_id"],
        node_id="workflow_planner",
        execution_generation=0,
        run_id=f"{parent['invocation_run_id']}:planner",
        trace_id=parent["invocation_trace_id"],
        objective=submission.objective,
        constraints=submission.constraints,
        constraint_ids=tuple(
            f"trusted_constraint_{index}"
            for index, _ in enumerate(submission.constraints)
        ),
        input_artifact_refs=submission.seed_artifact_refs,
        acceptance_contract=PersistedWorkflowStepAcceptanceContract(
            schema_version="workflow_step_acceptance_v2",
            output={
                "artifact_type": "workflow_plan",
                "description": "Admissible Workflow v2 DAG proposal",
            },
            criteria=(
                {
                    "criterion_id": "valid_plan",
                    "statement": "The proposal satisfies deterministic admission.",
                },
            ),
        ),
        capability_refs=(),
        explicit_tool_allowlist=(),
        available_tool_names=(),
        tool_scope_ref=registry.generation,
        budget_slice=PersistedWorkflowBudgetSlice(
            model_calls=1,
            tool_calls=0,
            workflow_quanta=1,
        ),
    )
    child = profile_input_adapter(
        {
            "user_id": assignment.user_id,
            "session_id": assignment.session_id,
            "agent_id": assignment.agent_id,
            "run_id": assignment.run_id,
            "trace_id": assignment.trace_id,
            "registered_tool_specs": registry.list_specs(),
            "available_tool_names": [],
        },
        ProfileInvocationInput(
            profile="planner",
            assignment_ref=assignment.assignment_ref,
            objective=assignment.objective,
            request_text=render_workflow_planner_prompt(
                workflow_objective=submission.objective,
                workflow_deliverables=submission.deliverables,
                workflow_constraints=submission.constraints,
                workflow_inputs=submission.inputs.model_dump(mode="json"),
                planning_objective=assignment.objective,
            ),
            constraints=assignment.constraints,
            explicit_tool_allowlist=(),
        ),
        model_call_limit=assignment.budget_slice.model_calls,
        tool_call_limit=0,
    )
    return {
        "planner_assignment": assignment.model_dump(mode="json"),
        "planner_child_state": child,
        "planner_result": None,
    }


def planner_child_runtime_context(
    state: Mapping[str, object],
    child_state: AssistantTurnState,
    runtime_context: object,
) -> GraphRuntimeContext:
    context = _require_context(runtime_context)
    assignment = WorkflowProfileAssignment.model_validate_json(
        json.dumps(state.get("planner_assignment"))
    )
    _validate_context_identity(state, context)
    return context.branch_context_factory.context_for_assignment(
        assignment,
        child_state,
        context.services,
        parent_invocation_token=context.invocation_token,
    )


def project_planner_profile_node(
    state: PlanningSubgraphState,
) -> dict[str, object]:
    child = state["planner_child_state"]
    assignment = WorkflowProfileAssignment.model_validate_json(
        json.dumps(state["planner_assignment"])
    )
    projected = profile_output_adapter(child)
    response = projected.response
    parsed = parse_workflow_plan_response(
        response.message if response is not None else "",
        run_id=assignment.run_id,
        trace_id=assignment.trace_id,
        model_calls_used=int(child.get("assistant_iterations", 0)),
        tool_calls_used=int(child.get("tool_calls_used", 0)),
    )
    result = PlannerProfileResult(
            status="succeeded" if parsed.status == "succeeded" else "failed",
            plan_proposal=parsed.plan_proposal,
            model_calls_used=parsed.model_calls_used,
            tool_calls_used=parsed.tool_calls_used,
            error_code=parsed.error_code,
        )
    return {"planner_result": result.model_dump(mode="json")}


def _rejected(message: str) -> dict[str, object]:
    return {
        "status": "failed",
        "phase": "failed",
        "admitted_plan": None,
        "execution_generation_by_node": {},
        "errors": (
            WorkflowGraphError(
                code="workflow_plan_rejected",
                message=message[:2_000],
            ),
        ),
    }


def admit_planner_result_node(state: PlanningSubgraphState) -> dict[str, object]:
    result = PlannerProfileResult.model_validate(state.get("planner_result"))
    proposal = result.plan_proposal
    if result.status != "succeeded" or not isinstance(proposal, WorkflowPlanV2Proposal):
        return _rejected(result.error_code or "planner proposal is not Workflow v2")
    if any(node.node_id == "plan" for node in proposal.nodes):
        return _rejected("planner proposal cannot create a plan node")
    submission = PersistedWorkflowSubmission.model_validate_json(
        json.dumps(state["submission"])
    )
    try:
        plan = materialize_runtime_plan(
            workflow=WorkflowPlanMaterializationInput(
                workflow_id=state["workflow_id"],
                current_plan_version=state["current_plan_version"],
                deliverables=submission.deliverables,
                constraints=submission.constraints,
            ),
            proposal=proposal,
            definition_version=state["definition_version"],
        )
        validate_plan_dag(plan, max_work_items=128)
    except (TypeError, ValueError, WorkflowTransitionRejected) as exc:
        return _rejected(str(exc) or "workflow plan rejected")
    persisted = persist_admitted_workflow_plan(plan)
    return {
        "admitted_plan": persisted.model_dump(mode="json"),
        "execution_generation_by_node": {node.node_id: 0 for node in persisted.nodes},
        "status": "running",
        "phase": "admitted",
    }


def build_workflow_planner_profile_graph(
    *,
    assistant_graph_app: AssistantTurnGraphApp,
) -> Any:
    return assistant_graph_app.namespaced_graph_for_profile(
        "planner",
        state_schema=PlanningSubgraphState,
        context_schema=WorkflowGraphRuntimeContext,
        child_state_key="planner_child_state",
        runtime_context_resolver=planner_child_runtime_context,
    )


def build_workflow_planning_subgraph(*, planner_graph: Any) -> Any:
    if getattr(planner_graph, "name", None) != "AssistantTurnGraph.planner":
        raise ValueError("planning requires compiled AssistantTurnGraph.planner")
    graph = StateGraph(
        PlanningSubgraphState,
        context_schema=WorkflowGraphRuntimeContext,
    )
    graph.add_node("prepare_planner", prepare_planner_profile_node)
    graph.add_node("planner_profile", planner_graph)
    graph.add_node("project_planner", project_planner_profile_node)
    graph.add_node("admit_plan", admit_planner_result_node)
    graph.add_edge(START, "prepare_planner")
    graph.add_edge("prepare_planner", "planner_profile")
    graph.add_edge("planner_profile", "project_planner")
    graph.add_edge("project_planner", "admit_plan")
    graph.add_edge("admit_plan", END)
    return graph.compile(checkpointer=None, name="WorkflowPlanningSubgraph")


__all__ = [
    "PlannerProfileResult",
    "PlanningSubgraphState",
    "admit_planner_result_node",
    "build_workflow_planner_profile_graph",
    "build_workflow_planning_subgraph",
    "planner_child_runtime_context",
    "prepare_planner_profile_node",
    "project_planner_profile_node",
]
