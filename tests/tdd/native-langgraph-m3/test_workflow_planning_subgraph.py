from __future__ import annotations

import json
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from assistant_agent.context.service import ContextService
from assistant_agent.runtime.assistant_graph_app import AssistantTurnGraphApp
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.graph_invocation_claims import (
    InMemoryGraphInvocationClaimStore,
)
from assistant_agent.runtime.tool_operation_barrier import SQLiteToolOperationStore
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.workflows.artifacts import LocalWorkflowArtifactStore
from assistant_agent.workflows.context import WorkflowContextCompiler
from assistant_agent.workflows.graph_context import (
    BranchProfileContextFactory,
    WorkflowGraphRuntimeContext,
    WorkflowGraphRuntimeServices,
)
from assistant_agent.workflows.graph_state import (
    PersistedWorkflowIdentity,
    initial_workflow_graph_state,
)
from assistant_agent.workflows.models import (
    WorkflowBudget,
    WorkflowRecord,
    WorkflowSubmission,
)
from assistant_agent.workflows.planning_graph import (
    build_workflow_planner_profile_graph,
    build_workflow_planning_subgraph,
    PlanningSubgraphState,
)
from tests.core.support import ProbeTool


class _PlannerAdapter:
    provider = "scripted"
    model = "planner-probe"

    def __init__(
        self,
        proposal: dict[str, object],
        *,
        response_text: str | None = None,
    ) -> None:
        self._proposal = proposal
        self._response_text = response_text
        self.requests = []

    def chat(self, request):
        self.requests.append(request)
        return ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            response_text=self._response_text
            or json.dumps(
                {"workflow_plan": self._proposal},
                ensure_ascii=False,
                sort_keys=True,
            ),
            usage={"input_tokens": 7, "output_tokens": 11},
        )


class _RecordingContextFactory(BranchProfileContextFactory):
    def __init__(self) -> None:
        self.contexts = []

    def context_for_assignment(
        self,
        outer_assignment,
        child_state,
        services,
        *,
        parent_invocation_token,
    ):
        context = super().context_for_assignment(
            outer_assignment,
            child_state,
            services,
            parent_invocation_token=parent_invocation_token,
        )
        self.contexts.append(context)
        return context


def _submission(constraints: list[str] | None = None) -> WorkflowSubmission:
    return WorkflowSubmission(
        workflow_type="deep_research",
        objective="Compare native graph execution",
        deliverables=["research report"],
        constraints=constraints or ["cite evidence"],
        inputs={"research_questions": ["How does recovery work?"]},
        requested_budget={
            "model_calls": 12,
            "tool_calls": 8,
            "workflow_quanta": 32,
            "deadline_seconds": 3600,
        },
        durability_reasons=["multi_stage"],
        seed_artifact_refs=["artifact://seed/evidence"],
        idempotency_key="workflow-submission-planning",
    )


def _budget() -> WorkflowBudget:
    return WorkflowBudget(
        model_calls_remaining=12,
        tool_calls_remaining=8,
        workflow_quanta_remaining=32,
        deadline_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


def _record(constraints: list[str] | None = None) -> WorkflowRecord:
    return WorkflowRecord(
        workflow_id="wf-planning",
        execution_engine="langgraph_v3",
        workflow_type="deep_research",
        definition_version="3",
        user_id="user-planning",
        agent_id="agent-planning",
        session_id="session-planning",
        ingress_run_id="ingress-run-planning",
        ingress_trace_id="a" * 32,
        idempotency_key="workflow-submission-planning",
        submission_digest="b" * 64,
        objective="Compare native graph execution",
        deliverables=["research report"],
        constraints=constraints or ["cite evidence"],
        inputs={"research_questions": ["How does recovery work?"]},
        phase="planning",
        budget=_budget(),
        seed_artifact_refs=["artifact://seed/evidence"],
    )


def _valid_proposal(constraints: list[str] | None = None) -> dict[str, object]:
    def acceptance(criterion: str) -> dict[str, object]:
        return {
            "schema_version": "workflow_step_acceptance_v2",
            "output": {
                "artifact_type": "research_report",
                "description": "Bounded evidence",
            },
            "criteria": [
                {"criterion_id": criterion, "statement": "Evidence exists"}
            ],
        }
    trusted_constraints = constraints or ["cite evidence"]
    return {
        "schema_version": "workflow_plan_v2",
        "nodes": [
            {
                "node_id": "collect_a",
                "display_title": "Collect A",
                "objective": "Collect source A",
                "depends_on": [],
                "acceptance_contract": acceptance("source_a"),
            },
            {
                "node_id": "collect_b",
                "display_title": "Collect B",
                "objective": "Collect source B",
                "depends_on": [],
                "acceptance_contract": acceptance("source_b"),
            },
            {
                "node_id": "synthesize",
                "display_title": "Synthesize",
                "objective": "Write the report",
                "depends_on": ["collect_a", "collect_b"],
                "acceptance_contract": acceptance("report"),
            },
        ],
        "deliverable_bindings": [
            {"deliverable": "research report", "producer_node_id": "synthesize"}
        ],
        "constraint_bindings": [
            {
                "constraint_id": f"constraint_{index}",
                "statement": statement,
                "owner_node_ids": ["collect_a", "collect_b"],
                "verifier_node_id": "synthesize",
                "severity": "required",
            }
            for index, statement in enumerate(trusted_constraints)
        ],
    }


def _planning_probe(
    tmp_path,
    proposal,
    *,
    constraints: list[str] | None = None,
    response_text: str | None = None,
):
    registry = ToolRegistry()
    registry.register(ProbeTool())
    registry.seal()
    adapter = _PlannerAdapter(proposal, response_text=response_text)
    artifact_store = LocalWorkflowArtifactStore(tmp_path / "artifacts")
    services = WorkflowGraphRuntimeServices(
        provider_registry={"planner": adapter},
        tool_registry=registry,
        context_service=ContextService(),
        operation_store=SQLiteToolOperationStore(tmp_path / "operations.sqlite3"),
        workflow_identity=PersistedWorkflowIdentity(
            user_id="user-planning",
            session_id="session-planning",
            agent_id="agent-planning",
            workflow_thread_id="workflow-thread-planning",
            turn_origin_id="ingress-run-planning",
        ),
        cancel_reader=lambda _assignment: None,
        stream_writer=lambda _assignment, _fact: None,
        invocation_claim_store=InMemoryGraphInvocationClaimStore(),
    )
    assistant_app = AssistantTurnGraphApp()
    context = WorkflowGraphRuntimeContext(
        assistant_graph_app=assistant_app,
        artifact_store=artifact_store,
        context_compiler=WorkflowContextCompiler(artifact_store=artifact_store),
        branch_context_factory=_RecordingContextFactory(),
        services=services,
        invocation_token="workflow-planning-invocation",
    )
    planner_graph = build_workflow_planner_profile_graph(
        assistant_graph_app=assistant_app
    )
    planning = build_workflow_planning_subgraph(planner_graph=planner_graph)
    parent = StateGraph(
        PlanningSubgraphState,
        context_schema=WorkflowGraphRuntimeContext,
    )
    parent.add_node("workflow_planning", planning)
    parent.add_edge(START, "workflow_planning")
    parent.add_edge("workflow_planning", END)
    app = parent.compile(
        checkpointer=InMemorySaver(),
        name="WorkflowPlanningProbe",
    )
    initial = initial_workflow_graph_state(
        workflow=_record(constraints),
        submission=_submission(constraints),
        admitted_plan=None,
        workflow_thread_id="workflow-thread-planning",
        invocation_run_id="workflow-invoke-planning",
        invocation_trace_id="trace-workflow-invoke-planning",
    )
    return app, planning, context, initial, adapter


def _config() -> dict[str, object]:
    return {"configurable": {"thread_id": "workflow-thread-planning"}}


def test_planner_child_is_native_subgraph_and_admission_is_deterministic(tmp_path):
    app, planning, context, initial, adapter = _planning_probe(
        tmp_path, _valid_proposal()
    )

    async def execute():
        parts = [
            part
            async for part in app.astream(
                initial,
                config=_config(),
                context=context,
                stream_mode=["updates", "tasks", "checkpoints"],
                subgraphs=True,
                version="v2",
            )
        ]
        return parts, await app.aget_state(_config())

    parts, snapshot = asyncio.run(execute())

    assert snapshot.values["admitted_plan"]["version"] == 2
    assert snapshot.values["phase"] == "admitted"
    namespaces = [tuple(part.get("ns") or ()) for part in parts]
    flattened = "/".join(segment for namespace in namespaces for segment in namespace)
    assert "planner_profile:" in flattened
    assert [(name, graph.name) for name, graph in app.get_subgraphs(recurse=True)] == [
        ("workflow_planning", "WorkflowPlanningSubgraph"),
        ("workflow_planning|planner_profile", "AssistantTurnGraph.planner"),
    ]
    assert "workflow_planning:planner_profile:assistant" in app.get_graph(
        xray=True
    ).nodes
    assert len(adapter.requests) == 1
    assert adapter.requests[0].tools == []
    assert adapter.requests[0].response_format == {"type": "json_object"}
    assert adapter.requests[0].max_tokens == 8_192
    assert "workflow_plan_v2" in adapter.requests[0].user_query
    assert '"workflow_plan"' in adapter.requests[0].user_query
    assert ProbeTool.name not in adapter.requests[0].user_query
    assert snapshot.values["planner_assignment"]["available_tool_names"] == []
    assert snapshot.values["planner_assignment"]["explicit_tool_allowlist"] == []
    encoded = json.dumps(snapshot.values, ensure_ascii=True, default=str)
    assert not any(segment in encoded for namespace in namespaces for segment in namespace)
    contexts = context.branch_context_factory.contexts
    assert contexts
    assert len({id(item.agent_state) for item in contexts}) == len(contexts)
    assert len({id(item.tool_executor) for item in contexts}) == len(contexts)
    assert {id(item.invocation_claim_store) for item in contexts} == {
        id(context.services.invocation_claim_store)
    }
    assert len({item.invocation_token for item in contexts}) == 1
    assert all(
        snapshot.values["planner_assignment"]["assignment_ref"]
        not in item.invocation_token
        for item in contexts
    )
    assert {item.graph_profile for item in contexts} == {"planner"}


def test_planner_owner_or_thread_mismatch_fails_before_provider(tmp_path):
    app, _planning, context, initial, adapter = _planning_probe(
        tmp_path, _valid_proposal()
    )
    wrong_identity = context.services.workflow_identity.model_copy(
        update={"agent_id": "other-agent"}
    )
    wrong_context = replace(
        context,
        services=replace(context.services, workflow_identity=wrong_identity),
    )

    async def execute():
        return [
            part
            async for part in app.astream(
                initial,
                config=_config(),
                context=wrong_context,
                stream_mode=["updates"],
                subgraphs=True,
                version="v2",
            )
        ]

    with pytest.raises(ValueError, match="identity mismatch"):
        asyncio.run(execute())
    assert adapter.requests == []


def test_non_json_planner_response_has_bounded_failure_state(tmp_path):
    app, _planning, context, initial, _adapter = _planning_probe(
        tmp_path,
        _valid_proposal(),
        response_text="ordinary prose response",
    )

    async def execute():
        async for _part in app.astream(
            initial,
            config=_config(),
            context=context,
            stream_mode=["updates"],
            subgraphs=True,
            version="v2",
        ):
            pass
        return await app.aget_state(_config())

    snapshot = asyncio.run(execute())
    child_response = snapshot.values["planner_child_state"].get("final_response")

    assert snapshot.values["status"] == "failed"
    assert snapshot.values["phase"] == "failed"
    assert [error["code"] for error in snapshot.values["errors"]] == [
        "workflow_plan_rejected"
    ]
    assert snapshot.values["planner_result"]["error_code"] == (
        "workflow_plan_invalid"
    )
    assert child_response is not None
    assert child_response["message"]
    with pytest.raises(json.JSONDecodeError):
        json.loads(child_response["message"])


def test_planning_admits_all_sixty_four_trusted_constraints(tmp_path):
    constraints = [f"trusted constraint {index}" for index in range(64)]
    app, _planning, context, initial, _adapter = _planning_probe(
        tmp_path,
        _valid_proposal(constraints),
        constraints=constraints,
    )

    async def execute():
        async for _part in app.astream(
            initial,
            config=_config(),
            context=context,
            stream_mode=["updates"],
            subgraphs=True,
            version="v2",
        ):
            pass
        return await app.aget_state(_config())

    snapshot = asyncio.run(execute())
    assert snapshot.values["phase"] == "admitted"
    assert len(snapshot.values["admitted_plan"]["constraint_bindings"]) == 64


def _invalid_proposal(case: str) -> dict[str, object]:
    proposal = json.loads(json.dumps(_valid_proposal()))
    nodes = proposal["nodes"]
    assert isinstance(nodes, list)
    if case == "cycle":
        nodes[0]["depends_on"] = ["synthesize"]
    elif case == "unknown_dependency":
        nodes[0]["depends_on"] = ["missing"]
    elif case == "missing_deliverable":
        proposal["deliverable_bindings"] = []
    elif case == "non_terminal_producer":
        proposal["deliverable_bindings"][0]["producer_node_id"] = "collect_a"
    elif case == "required_without_verifier":
        proposal["constraint_bindings"][0]["verifier_node_id"] = None
    elif case == "verifier_not_downstream":
        proposal["constraint_bindings"][0]["verifier_node_id"] = "collect_a"
    elif case == "planner_node":
        nodes[0]["node_id"] = "plan"
        nodes[2]["depends_on"][0] = "plan"
        proposal["constraint_bindings"][0]["owner_node_ids"][0] = "plan"
    else:  # pragma: no cover - guarded by parametrization
        raise AssertionError(case)
    return proposal


@pytest.mark.parametrize(
    "case",
    [
        "cycle",
        "unknown_dependency",
        "missing_deliverable",
        "non_terminal_producer",
        "required_without_verifier",
        "verifier_not_downstream",
        "planner_node",
    ],
)
def test_invalid_planner_proposal_fails_before_worker_routing(tmp_path, case):
    app, _planning, context, initial, _adapter = _planning_probe(
        tmp_path / case, _invalid_proposal(case)
    )

    async def execute():
        parts = [
            part
            async for part in app.astream(
                initial,
                config=_config(),
                context=context,
                stream_mode=["updates", "tasks", "checkpoints"],
                subgraphs=True,
                version="v2",
            )
        ]
        return parts, await app.aget_state(_config())

    parts, snapshot = asyncio.run(execute())

    assert snapshot.values["status"] == "failed"
    assert snapshot.values["phase"] == "failed"
    assert snapshot.values["admitted_plan"] is None
    assert snapshot.values["execution_generation_by_node"] == {}
    assert [error["code"] for error in snapshot.values["errors"]] == [
        "workflow_plan_rejected"
    ]
    task_names = [
        str(part.get("data", {}).get("name", ""))
        for part in parts
        if part.get("type") == "tasks" and isinstance(part.get("data"), dict)
    ]
    assert not any("worker" in name for name in task_names)


def test_planning_graph_name_and_nodes_are_stable(tmp_path):
    _first_app, first, _context, _initial, _adapter = _planning_probe(
        tmp_path / "first", _valid_proposal()
    )
    _second_app, second, _context2, _initial2, _adapter2 = _planning_probe(
        tmp_path / "second", _valid_proposal()
    )

    assert first.name == second.name == "WorkflowPlanningSubgraph"
    assert set(first.get_graph().nodes) == {
        "__start__",
        "prepare_planner",
        "planner_profile",
        "project_planner",
        "admit_plan",
        "__end__",
    }
