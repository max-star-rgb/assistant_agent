from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

from langgraph.checkpoint.memory import InMemorySaver

from assistant_agent.context.service import ContextService
from assistant_agent.runtime.assistant_graph_app import AssistantTurnGraphApp
from assistant_agent.runtime.chat_adapter import ChatResult
from assistant_agent.runtime.tool_operation_barrier import SQLiteToolOperationStore
from assistant_agent.tools.registry import ToolRegistry
from assistant_agent.workflows.artifacts import LocalWorkflowArtifactStore
from assistant_agent.workflows.context import WorkflowContextCompiler
from assistant_agent.workflows.durable_graph import build_durable_workflow_graph
from assistant_agent.workflows.durable_graph_nodes import (
    WorkflowProfileBranchState,
    build_verifier_branch_subgraph,
    build_worker_branch_subgraph,
    verifier_child_runtime_context,
    worker_child_runtime_context,
)
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
)
from assistant_agent.workflows.graph_publish import (
    SQLiteWorkflowPublishStore,
    SQLiteWorkflowPublisher,
)
from tests.core.support import ProbeTool


def acceptance(criterion: str) -> dict[str, object]:
    return {
        "schema_version": "workflow_step_acceptance_v2",
        "output": {"artifact_type": "research_report", "description": "Evidence"},
        "criteria": [{"criterion_id": criterion, "statement": "Evidence exists"}],
    }


def proposal(dependencies: dict[str, list[str]]) -> dict[str, object]:
    terminal = sorted(
        node_id
        for node_id in dependencies
        if not any(node_id in parents for parents in dependencies.values())
    )[-1]
    return {
        "schema_version": "workflow_plan_v2",
        "nodes": [
            {
                "node_id": node_id,
                "display_title": node_id,
                "objective": f"execute {node_id}",
                "depends_on": parents,
                "acceptance_contract": acceptance(f"criterion_{node_id}"),
            }
            for node_id, parents in dependencies.items()
        ],
        "deliverable_bindings": [
            {"deliverable": "report", "producer_node_id": terminal}
        ],
        "constraint_bindings": [],
    }


class PlannerAdapter:
    provider = "scripted"
    model = "planner-probe"

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def chat(self, _request):
        return ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            response_text=json.dumps({"workflow_plan": self.payload}),
            usage={"input_tokens": 1, "output_tokens": 1},
        )


class WorkerAdapter:
    provider = "scripted"
    model = "worker-probe"

    def __init__(
        self,
        root_nodes: set[str] | None = None,
        all_nodes: set[str] | None = None,
        responses: dict[str, str] | None = None,
    ) -> None:
        self._root_nodes = root_nodes or set()
        self._all_nodes = all_nodes or set(self._root_nodes)
        self._responses = responses or {}
        self._barrier = (
            threading.Barrier(len(self._root_nodes))
            if len(self._root_nodes) > 1
            else None
        )
        self._lock = threading.Lock()
        self._barrier_seen: set[str] = set()
        self.active = 0
        self.max_concurrency = 0
        self.requests = []

    def chat(self, request):
        self.requests.append(request)
        node_id = next(
            node
            for node in sorted(self._all_nodes)
            if f"execute {node}" in request.user_query
        )
        with self._lock:
            self.active += 1
            self.max_concurrency = max(self.max_concurrency, self.active)
        try:
            wait_at_barrier = False
            if node_id in self._root_nodes and self._barrier is not None:
                with self._lock:
                    if node_id not in self._barrier_seen:
                        self._barrier_seen.add(node_id)
                        wait_at_barrier = True
            if wait_at_barrier:
                self._barrier.wait(timeout=5)
            return ChatResult(
                provider=self.provider,
                model=self.model,
                finish_reason="stop",
                response_text=self._responses.get(
                    node_id,
                    json.dumps(
                        {
                            "workflow_control": {
                                "outcome": "completed",
                                "summary": f"completed {node_id}",
                            }
                        }
                    ),
                ),
                usage={"input_tokens": 1, "output_tokens": 1},
            )
        finally:
            with self._lock:
                self.active -= 1


class VerifierAdapter:
    provider = "scripted"
    model = "verifier-probe"

    def __init__(self, responses: list[dict[str, object]] | None = None) -> None:
        self.responses = list(responses or [{"status": "verified", "summary": "verified"}])
        self.requests = []

    def chat(self, request):
        self.requests.append(request)
        payload = self.responses.pop(0)
        return ChatResult(
            provider=self.provider,
            model=self.model,
            finish_reason="stop",
            response_text=json.dumps({"workflow_verification": payload}),
            usage={"input_tokens": 1, "output_tokens": 1},
        )


def workflow_probe(
    tmp_path,
    dependencies: dict[str, list[str]],
    *,
    worker_responses: dict[str, str] | None = None,
    plan_payload: dict[str, object] | None = None,
    verifier_responses: list[dict[str, object]] | None = None,
):
    registry = ToolRegistry()
    registry.register(ProbeTool())
    registry.seal()
    planner = PlannerAdapter(plan_payload or proposal(dependencies))
    roots = {node for node, parents in dependencies.items() if not parents}
    worker = WorkerAdapter(roots, set(dependencies), worker_responses)
    verifier = VerifierAdapter(verifier_responses)
    artifact_store = LocalWorkflowArtifactStore(tmp_path / "artifacts")
    services = WorkflowGraphRuntimeServices(
        provider_registry={"planner": planner, "worker": worker, "verifier": verifier},
        tool_registry=registry,
        context_service=ContextService(),
        operation_store=SQLiteToolOperationStore(tmp_path / "operations.sqlite3"),
        memory_host=object(),
        workflow_identity=PersistedWorkflowIdentity(
            user_id="user-send",
            session_id="session-send",
            agent_id="agent-send",
            workflow_thread_id="workflow-thread-send",
            turn_origin_id="ingress-send",
        ),
        cancel_reader=lambda _assignment: None,
        stream_writer=lambda _assignment, _fact: None,
        publish_store=SQLiteWorkflowPublishStore(tmp_path / "publish.sqlite3"),
        publisher=SQLiteWorkflowPublisher(tmp_path / "publish-effects.sqlite3"),
    )
    assistant_app = AssistantTurnGraphApp()
    context = WorkflowGraphRuntimeContext(
        assistant_graph_app=assistant_app,
        artifact_store=artifact_store,
        context_compiler=WorkflowContextCompiler(artifact_store=artifact_store),
        branch_context_factory=BranchProfileContextFactory(),
        services=services,
    )
    planning = build_workflow_planning_subgraph(
        planner_graph=build_workflow_planner_profile_graph(
            assistant_graph_app=assistant_app
        )
    )
    worker_branch = build_worker_branch_subgraph(
        worker_graph=assistant_app.namespaced_graph_for_profile(
            "worker",
            state_schema=WorkflowProfileBranchState,
            context_schema=WorkflowGraphRuntimeContext,
            child_state_key="worker_child_state",
            runtime_context_resolver=worker_child_runtime_context,
        )
    )
    verifier_branch = build_verifier_branch_subgraph(
        verifier_graph=assistant_app.namespaced_graph_for_profile(
            "verifier",
            state_schema=WorkflowProfileBranchState,
            context_schema=WorkflowGraphRuntimeContext,
            child_state_key="verifier_child_state",
            runtime_context_resolver=verifier_child_runtime_context,
        )
    )
    app = build_durable_workflow_graph(
        planning_subgraph=planning,
        worker_branch_subgraph=worker_branch,
        verifier_branch_subgraph=verifier_branch,
        checkpointer=InMemorySaver(),
    )
    budget = WorkflowBudget(
        model_calls_remaining=64,
        tool_calls_remaining=64,
        workflow_quanta_remaining=64,
        deadline_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    record = WorkflowRecord(
        workflow_id="wf-send",
        execution_engine="langgraph_v3",
        workflow_type="deep_research",
        definition_version="3",
        user_id="user-send",
        agent_id="agent-send",
        session_id="session-send",
        ingress_run_id="ingress-send",
        idempotency_key="send-idempotency",
        submission_digest="a" * 64,
        objective="execute graph",
        deliverables=["report"],
        phase="planning",
        budget=budget,
    )
    submission = WorkflowSubmission(
        workflow_type="deep_research",
        objective="execute graph",
        deliverables=["report"],
        inputs={"research_questions": ["question"]},
        requested_budget={
            "model_calls": 64,
            "tool_calls": 64,
            "workflow_quanta": 64,
            "deadline_seconds": 3600,
        },
        durability_reasons=["multi_stage"],
        idempotency_key="send-idempotency",
    )
    initial = initial_workflow_graph_state(
        workflow=record,
        submission=submission,
        admitted_plan=None,
        workflow_thread_id="workflow-thread-send",
        invocation_run_id="invoke-send",
        invocation_trace_id="trace-send",
    )
    return app, context, initial, worker, artifact_store


def config() -> dict[str, object]:
    return {"configurable": {"thread_id": "workflow-thread-send"}}
