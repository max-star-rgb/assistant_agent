from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime
from langgraph.store.memory import InMemoryStore
from langgraph_sdk.auth.types import StudioUser
from pydantic import PrivateAttr, ValidationError
import pytest

from assistant_agent.agent_server import services
from assistant_agent.agent_server.services import AgentServerExecutionOwner
from assistant_agent.coding import review as coding_review
from assistant_agent.coding.config import CodingRepositoryConfig
from assistant_agent.coding.models import CodingAnalysisSnapshot, CodingReviewInput
from assistant_agent.coding.workspace import CodingWorkspaceError
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import build_fast_agent
from assistant_agent.native_agent.models import (
    BudgetUsage,
    NativePlanNode,
    NativePlanProposal,
    PlanDeliverable,
    WorkerCompletion,
)
from assistant_agent.native_agent.planning_budget import PlanningBudgetPolicy
from assistant_agent.native_agent.planning_graph import build_planning_graph
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.native_agent.state import AssistantRootInput
from assistant_agent.skills.loading import SkillCatalog
from assistant_agent.tools.native_boundary import configure_builtin_tool


def _server_config() -> dict[str, object]:
    return {
        "configurable": {
            "assistant_id": "assistant-sentinel",
            "graph_id": "graph-sentinel",
            "langgraph_auth_user": StudioUser("langgraph-studio-user"),
        }
    }


async def _open_owner() -> AgentServerExecutionOwner:
    return await AgentServerExecutionOwner.compose(store=InMemoryStore())


class _PlanningProbeModel(MockAssistantChatModel):
    objectives: tuple[str, ...]

    _active_workers: int = PrivateAttr(default=0)
    _max_active_workers: int = PrivateAttr(default=0)

    def _response_message(self, messages, **kwargs):
        visible_names = _probe_tool_names(kwargs.get("tools"))
        if "NativePlanProposal" in visible_names:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "NativePlanProposal",
                        "args": {
                            "schema_version": "native_plan_v2",
                            "nodes": [
                                {
                                    "node_id": f"worker-{index}",
                                    "objective": objective,
                                }
                                for index, objective in enumerate(
                                    self.objectives, start=1
                                )
                            ],
                            "deliverables": [
                                {
                                    "deliverable_id": "answer",
                                    "description": ("return the planning probe answer"),
                                    "producer_node_ids": ["worker-1"],
                                }
                            ],
                        },
                        "id": "planning-probe-proposal",
                        "type": "tool_call",
                    }
                ],
            )
        if "WorkerCompletion" in visible_names:
            objective = _probe_last_human_text(messages).split("\n", 1)[0]
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "WorkerCompletion",
                        "args": {
                            "status": "completed",
                            "content": f"completed:{objective}",
                        },
                        "id": f"completion-{objective}",
                        "type": "tool_call",
                    }
                ],
            )
        return super()._response_message(messages, **kwargs)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        objective = _probe_last_human_text(messages)
        if objective in self.objectives:
            self._active_workers += 1
            self._max_active_workers = max(
                self._max_active_workers,
                self._active_workers,
            )
            await asyncio.sleep(0.02)
            try:
                return self._generate(
                    messages,
                    stop=stop,
                    run_manager=run_manager,
                    **kwargs,
                )
            finally:
                self._active_workers -= 1
        return self._generate(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )


class _ZeroNodePlanningProbeModel(MockAssistantChatModel):
    def _response_message(self, messages, **kwargs):
        visible_names = _probe_tool_names(kwargs.get("tools"))
        if "NativePlanProposal" not in visible_names:
            return AIMessage(content="zero-node-final-answer-sentinel")
        if not any(
            isinstance(message, ToolMessage) and message.name == "planning_probe"
            for message in messages
        ):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "planning_probe",
                        "args": {},
                        "id": "planning-probe-call",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "NativePlanProposal",
                    "args": {
                        "schema_version": "native_plan_v2",
                        "nodes": [],
                        "deliverables": [
                            {
                                "deliverable_id": "answer",
                                "description": "answer from planning evidence",
                                "evidence_refs": ["planning-probe-call"],
                            }
                        ],
                    },
                    "id": "zero-node-plan-call",
                    "type": "tool_call",
                }
            ],
        )


class _PlanningRecoveryProbeAgent:
    name = "AssistantFastAgent"

    def __init__(self) -> None:
        self.planner_calls = 0
        self.worker_calls: Counter[str] = Counter()

    async def ainvoke(
        self,
        input: dict[str, Any],
        *,
        context: Any,
    ) -> dict[str, Any]:
        del context
        phase = input["agent_phase"]
        if phase == "planner":
            self.planner_calls += 1
            proposal = (
                _initial_recovery_probe_plan()
                if self.planner_calls == 1
                else _replacement_recovery_probe_plan()
            )
            return {
                "messages": list(input["messages"]),
                "structured_response": proposal,
                "phase_budget_usage": BudgetUsage(model_calls=1),
            }
        if phase == "worker":
            objective = str(input["messages"][0].content).split("\n", 1)[0]
            if (
                objective == "successful-worker-sentinel"
                and self.worker_calls[objective]
            ):
                raise AssertionError("frozen successful worker was replayed")
            self.worker_calls[objective] += 1
            if objective == "operational-failure-sentinel":
                raise TimeoutError("operational-probe")
            completion = WorkerCompletion(
                status="completed",
                content=f"{objective}-result",
            )
            return {
                "messages": [AIMessage(content=completion.content)],
                "structured_response": completion,
                "phase_budget_usage": BudgetUsage(model_calls=1),
            }
        assert phase == "finalizer"
        return {
            "messages": [AIMessage(content="recovery-final-answer-sentinel")],
            "phase_budget_usage": BudgetUsage(model_calls=1),
        }


def _initial_recovery_probe_plan() -> NativePlanProposal:
    return NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=(
            NativePlanNode(
                node_id="successful-worker",
                objective="successful-worker-sentinel",
            ),
            NativePlanNode(
                node_id="failed-worker",
                objective="operational-failure-sentinel",
            ),
        ),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="recovery probe",
                producer_node_ids=("successful-worker", "failed-worker"),
            ),
        ),
    )


def _replacement_recovery_probe_plan() -> NativePlanProposal:
    return NativePlanProposal(
        schema_version="native_plan_v2",
        nodes=(
            NativePlanNode(
                node_id="replacement-worker",
                objective="replacement-worker-sentinel",
                replaces_node_ids=("failed-worker",),
                frozen_dependency_ids=("successful-worker",),
            ),
        ),
        deliverables=(
            PlanDeliverable(
                deliverable_id="answer",
                description="recovered probe",
                producer_node_ids=("replacement-worker",),
                frozen_result_refs=("successful-worker",),
            ),
        ),
    )


def _probe_last_human_text(messages) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return ""


def _probe_tool_names(raw_tools: object) -> set[str]:
    if not isinstance(raw_tools, list):
        return set()
    return {
        function["name"]
        for item in raw_tools
        if isinstance(item, dict)
        and isinstance((function := item.get("function")), dict)
        and isinstance(function.get("name"), str)
    }


@pytest.mark.core_invariant("BOOT-001")
def test_mock_composition_opens_without_real_provider(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    owner = asyncio.run(_open_owner())
    try:
        assert owner.model._llm_type == "assistant-agent-mock"
        assert owner.memory_backend.backend_id == "disabled"
        assert owner.memory_graph.name == "AssistantMemoryExtractionGraph"
    finally:
        asyncio.run(owner.aclose())


@pytest.mark.core_invariant("LOOP-001")
def test_parent_graph_has_fast_planning_and_coding_native_branches(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    owner = asyncio.run(_open_owner())
    try:
        graph = owner.graph.get_graph()
        nodes = {
            name for name in graph.nodes if not name.startswith("__error_handler__")
        }
        assert owner.graph.name == "AssistantRootGraph"
        assert nodes == {
            "__start__",
            "capture_trusted_runtime_facts",
            "memory_recall",
            "execution_router",
            "fast_agent",
            "planning_graph",
            "coding_graph",
            "refresh_memory_extraction",
            "__end__",
        }
        assert graph.nodes["fast_agent"].data.name == "AssistantFastAgent"
        assert graph.nodes["planning_graph"].data.name == "AssistantPlanningGraph"
        assert graph.nodes["coding_graph"].data.name == "AssistantCodingGraph"
        assert (
            CodingRepositoryConfig(
                repo_id="core-probe",
                path=owner.coding_workspace_service.config.workspace_root.parent,
                target_branch="main",
            ).parallel_analysis_enabled
            is False
        )
        assert (
            CodingRepositoryConfig(
                repo_id="core-review-probe",
                path=owner.coding_workspace_service.config.workspace_root.parent,
                target_branch="main",
            ).code_review_enabled
            is False
        )
        planning_nodes = {
            name
            for name in graph.nodes["planning_graph"].data.get_graph().nodes
            if not name.startswith("__error_handler__")
        }
        assert planning_nodes == {
            "__start__",
            "planner",
            "assess_planner",
            "prepare_replan",
            "admit_plan",
            "scheduler",
            "reserve_wave_budget",
            "worker",
            "join",
            "reconcile_wave_budget",
            "assess_workers",
            "finalize",
            "controlled_finalize",
            "__end__",
        }
        planning_edges = {
            (edge.source, edge.target)
            for edge in graph.nodes["planning_graph"].data.get_graph().edges
        }
        assert planning_edges == {
            ("__start__", "planner"),
            ("planner", "assess_planner"),
            ("assess_planner", "admit_plan"),
            ("assess_planner", "planner"),
            ("assess_planner", "prepare_replan"),
            ("assess_planner", "controlled_finalize"),
            ("prepare_replan", "planner"),
            ("admit_plan", "planner"),
            ("admit_plan", "scheduler"),
            ("scheduler", "reserve_wave_budget"),
            ("reserve_wave_budget", "worker"),
            ("reserve_wave_budget", "finalize"),
            ("reserve_wave_budget", "controlled_finalize"),
            ("worker", "join"),
            ("join", "reconcile_wave_budget"),
            ("reconcile_wave_budget", "assess_workers"),
            ("assess_workers", "scheduler"),
            ("assess_workers", "prepare_replan"),
            ("assess_workers", "finalize"),
            ("assess_workers", "controlled_finalize"),
            ("finalize", "finalize"),
            ("finalize", "controlled_finalize"),
            ("finalize", "__end__"),
            ("controlled_finalize", "__end__"),
        }
        coding_graph = graph.nodes["coding_graph"].data.get_graph()
        coding_nodes = set(coding_graph.nodes)
        analysis_super_step_nodes = {
            "prepare_analysis",
            "analyze_workspace",
            "join_analysis",
        }
        assert analysis_super_step_nodes.issubset(coding_nodes)
        coding_edges = {
            (edge.source, edge.target) for edge in coding_graph.edges
        }
        assert {
            ("prepare_analysis", "analyze_workspace"),
            ("prepare_analysis", "join_analysis"),
            ("analyze_workspace", "join_analysis"),
            ("join_analysis", "inspect_and_draft"),
            ("inspect_and_draft", "validate_proposal"),
        }.issubset(coding_edges)
        assert {
            target
            for source, target in coding_edges
            if source == "analyze_workspace"
        } == {"join_analysis"}
        assert {
            source
            for source, target in coding_edges
            if target == "validate_proposal"
        } == {"inspect_and_draft"}
        assert {
            ("apply_patch", "plan_dependencies"),
            ("plan_dependencies", "plan_credentials"),
            ("plan_credentials", "plan_artifacts"),
            ("plan_artifacts", "run_validation"),
        }.issubset(coding_edges)
        assert {
            ("run_validation", "prepare_repair"),
            ("prepare_repair", "consume_repair_budget"),
            ("consume_repair_budget", "inspect_and_draft"),
        }.issubset(coding_edges)
        review_gate_nodes = {
            "prepare_review_snapshot",
            "run_code_review",
            "coding_review_decision",
        }
        assert review_gate_nodes.issubset(coding_nodes)
        assert {
            ("run_validation", "prepare_review_snapshot"),
            ("prepare_review_snapshot", "run_code_review"),
            ("run_code_review", "coding_review_decision"),
            ("coding_review_decision", "create_commit"),
            ("coding_review_decision", "summarize"),
        }.issubset(coding_edges)
        repair_lane_nodes = {
            "run_validation",
            "prepare_repair",
            "consume_repair_budget",
            "approval",
        }
        assert not any(
            source in repair_lane_nodes and target in analysis_super_step_nodes
            for source, target in coding_edges
        )
        assert {
            ("run_validation", "create_commit"),
            ("create_commit", "prepare_merge"),
            ("prepare_merge", "merge_approval"),
            ("merge_approval", "apply_merge"),
            ("apply_merge", "summarize"),
        }.issubset(coding_edges)
        assert {
            source for source, target in coding_edges if target == "create_commit"
        } == {"run_validation", "coding_review_decision"}
        assert owner.graph.checkpointer is None
    finally:
        asyncio.run(owner.aclose())


@pytest.mark.core_invariant("LOOP-001")
def test_production_composition_reuses_one_planning_budget_policy(monkeypatch) -> (
    None
):
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    monkeypatch.setenv("MAX_TOOL_ITERATIONS", "3")
    fast_policies: list[PlanningBudgetPolicy | None] = []
    planning_policies: list[PlanningBudgetPolicy | None] = []
    real_build_fast_agent = services.build_fast_agent
    real_build_planning_graph = services.build_planning_graph

    def recording_build_fast_agent(*args: Any, **kwargs: Any):
        fast_policies.append(kwargs.get("budget_policy"))
        return real_build_fast_agent(*args, **kwargs)

    def recording_build_planning_graph(*args: Any, **kwargs: Any):
        planning_policies.append(kwargs.get("budget_policy"))
        return real_build_planning_graph(*args, **kwargs)

    monkeypatch.setattr(services, "build_fast_agent", recording_build_fast_agent)
    monkeypatch.setattr(
        services,
        "build_planning_graph",
        recording_build_planning_graph,
    )

    owner = asyncio.run(_open_owner())
    try:
        assert len(fast_policies) == len(planning_policies) == 1
        assert isinstance(fast_policies[0], PlanningBudgetPolicy)
        assert fast_policies[0].base == 3
        assert fast_policies[0] is planning_policies[0]
    finally:
        asyncio.run(owner.aclose())


@pytest.mark.core_invariant("LOOP-001")
def test_coding_review_agent_consumes_eight_reads_then_structured_result(
    monkeypatch,
) -> None:
    """Catches ToolStrategy's final result consuming the read-Tool allowance."""

    @tool("review_read_probe")
    def review_read_probe() -> str:
        """Return one generic immutable review sentinel."""

        return "review-read-sentinel"

    class ReviewLoopProbeModel(MockAssistantChatModel):
        def _response_message(self, messages, **kwargs):
            completed_reads = sum(
                isinstance(message, ToolMessage)
                and message.name == "review_read_probe"
                for message in messages
            )
            if completed_reads < 8:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "review_read_probe",
                            "args": {},
                            "id": f"review-read-{completed_reads + 1}",
                            "type": "tool_call",
                        }
                    ],
                )
            structured_name = next(
                item["function"]["name"]
                for item in kwargs.get("tools", ())
                if item["function"]["name"] != "review_read_probe"
            )
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": structured_name,
                        "args": {
                            "status": "completed",
                            "findings": [],
                            "error_code": None,
                        },
                        "id": "review-result",
                        "type": "tool_call",
                    }
                ],
            )

    monkeypatch.setattr(
        coding_review,
        "build_coding_review_tools",
        lambda _service: [review_read_probe],
    )
    now = datetime.now(UTC)
    snapshot = CodingAnalysisSnapshot(
        materialization_schema_version="immutable_manifest_v2",
        snapshot_ref="snapshot-core-review-loop-0001",
        workspace_ref="workspace-core-review-loop-0001",
        base_commit="a" * 40,
        tree_digest="b" * 64,
        workspace_diff_digest="c" * 64,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    review_input = CodingReviewInput(
        workspace_ref=snapshot.workspace_ref,
        base_commit=snapshot.base_commit,
        patch_digest="d" * 64,
        workspace_diff_digest=snapshot.workspace_diff_digest,
        snapshot_materialization_schema_version=snapshot.materialization_schema_version,
        snapshot_created_at=snapshot.created_at,
        snapshot_expires_at=snapshot.expires_at,
        generation=1,
        snapshot_ref=snapshot.snapshot_ref,
        tree_digest=snapshot.tree_digest,
        validation_evidence_digest="e" * 64,
        review_tasks=coding_review.REVIEW_TASK_IDS,
    )
    graph = coding_review.create_coding_review_graph(
        model=ReviewLoopProbeModel(),
        workspace_service=object(),
    )

    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [],
                "coding_repo_id": "core-probe",
                "workspace_ref": snapshot.workspace_ref,
                "base_commit": snapshot.base_commit,
                "review_snapshot": snapshot,
                "review_input": review_input,
            },
            context=AssistantRunContext(),
        )
    )

    assert result["review_report"].status == "clean"
    assert all(item.status == "completed" for item in result["review_results"])


@pytest.mark.core_invariant("LOOP-001")
def test_coding_review_distinguishes_binding_failure_from_unavailable() -> None:
    """Catches current snapshot evidence becoming an approvable unavailable report."""

    now = datetime.now(UTC)
    snapshot = CodingAnalysisSnapshot(
        materialization_schema_version="immutable_manifest_v2",
        snapshot_ref="snapshot-core-review-binding-0001",
        workspace_ref="workspace-core-review-binding-0001",
        base_commit="a" * 40,
        tree_digest="b" * 64,
        workspace_diff_digest="c" * 64,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    review_input = CodingReviewInput(
        workspace_ref=snapshot.workspace_ref,
        base_commit=snapshot.base_commit,
        patch_digest="d" * 64,
        workspace_diff_digest=snapshot.workspace_diff_digest,
        snapshot_materialization_schema_version=snapshot.materialization_schema_version,
        snapshot_created_at=snapshot.created_at,
        snapshot_expires_at=snapshot.expires_at,
        generation=1,
        snapshot_ref=snapshot.snapshot_ref,
        tree_digest=snapshot.tree_digest,
        validation_evidence_digest="e" * 64,
        review_tasks=coding_review.REVIEW_TASK_IDS,
    )
    state = {
        "messages": [],
        "coding_repo_id": "core-probe",
        "workspace_ref": snapshot.workspace_ref,
        "base_commit": snapshot.base_commit,
        "analysis_snapshot": snapshot,
        "review_input": review_input,
        "review_task": coding_review.build_review_tasks()[0],
        "provider_search_profile": "none",
    }

    class BindingMismatchAgent:
        async def ainvoke(self, _state, *, config, context):
            del config, context
            content = "binding-sentinel\n"
            return {
                "structured_response": {
                    "status": "completed",
                    "findings": [],
                    "error_code": None,
                },
                "messages": (
                    ToolMessage(
                        content="",
                        name="coding_repo_read",
                        tool_call_id="binding-probe",
                        artifact={
                            "snapshot_ref": snapshot.snapshot_ref,
                            "tree_digest": "f" * 64,
                            "content_digest": hashlib.sha256(
                                content.encode("utf-8")
                            ).hexdigest(),
                            "result": {
                                "path": "src/probe.py",
                                "content": content,
                                "start_line": 1,
                                "end_line": 1,
                                "total_lines": 1,
                                "next_line": None,
                            },
                        },
                    ),
                ),
            }

    class UnavailableAgent:
        async def ainvoke(self, _state, *, config, context):
            del config, context
            raise RuntimeError("review-unavailable-sentinel")

    with pytest.raises(CodingWorkspaceError) as raised:
        asyncio.run(
            coding_review.review_workspace(
                state,
                None,
                review_agent=BindingMismatchAgent(),
            )
        )
    unavailable = asyncio.run(
        coding_review.review_workspace(
            state,
            None,
            review_agent=UnavailableAgent(),
        )
    )["review_results"][0]

    assert raised.value.code == "coding_review_binding_mismatch"
    assert unavailable.status == "unavailable"
    assert unavailable.error_code == "coding_review_task_failed"


@pytest.mark.core_invariant("RUN-001")
@pytest.mark.core_invariant("LOOP-001")
def test_both_modes_finish_with_standard_ai_messages(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    owner = asyncio.run(_open_owner())
    context = AssistantRunContext()

    async def run_modes():
        return [
            await owner.graph.ainvoke(
                {
                    "messages": [HumanMessage(content="request-sentinel")],
                    "execution_mode": mode,
                },
                context=context,
                config=_server_config(),
            )
            for mode in ("fast", "planning")
        ]

    try:
        results = asyncio.run(run_modes())
        assert all(isinstance(result["messages"][-1], AIMessage) for result in results)
        assert all("final_response" not in result for result in results)
    finally:
        asyncio.run(owner.aclose())


@pytest.mark.core_invariant("LOOP-001")
def test_planning_workers_reuse_one_agent_without_state_leakage() -> None:
    model = _PlanningProbeModel(objectives=("worker-a-sentinel", "worker-b-sentinel"))
    shared_agent = build_fast_agent(model, [])
    graph = build_planning_graph(model, shared_agent)

    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [HumanMessage(content="request-sentinel")],
                "memory_context": (),
                "memory_status": "empty",
            },
            context=AssistantRunContext(),
        )
    )

    contents = {
        work_item_id: item.content
        for work_item_id, item in result["frozen_worker_results"].items()
    }
    assert model._max_active_workers == 2
    assert "worker-a-sentinel" in contents["worker-1"]
    assert "worker-b-sentinel" not in contents["worker-1"]
    assert "worker-b-sentinel" in contents["worker-2"]
    assert "worker-a-sentinel" not in contents["worker-2"]


@pytest.mark.core_invariant("LOOP-001")
def test_planning_operational_failure_replans_without_replaying_success() -> None:
    agent = _PlanningRecoveryProbeAgent()
    graph = build_planning_graph(
        object(),
        agent,
        skill_catalog=SkillCatalog(),
    )

    async def collect_stream():
        return [
            part
            async for part in graph.astream(
                {
                    "messages": [HumanMessage(content="request-sentinel")],
                    "memory_context": (),
                    "memory_status": "empty",
                },
                context=AssistantRunContext(),
                stream_mode=["updates", "values"],
                version="v2",
            )
        ]

    parts = asyncio.run(collect_stream())
    node_path = [
        node_name
        for part in parts
        if part["type"] == "updates"
        for node_name in part["data"]
    ]
    final_state = [
        part["data"] for part in parts if part["type"] == "values"
    ][-1]
    recovery_path = ("assess_workers", "prepare_replan", "planner")

    assert any(
        tuple(node_path[index : index + len(recovery_path)]) == recovery_path
        for index in range(len(node_path) - len(recovery_path) + 1)
    )
    frozen_results = final_state["frozen_worker_results"]
    assert frozen_results["successful-worker"].content == (
        "successful-worker-sentinel-result"
    )
    assert frozen_results["replacement-worker"].content == (
        "replacement-worker-sentinel-result"
    )
    assert isinstance(final_state["messages"][-1], AIMessage)


@pytest.mark.core_invariant("RUN-001")
@pytest.mark.core_invariant("LOOP-001")
def test_zero_node_planning_plan_finishes_with_standard_ai_message() -> None:
    model = _ZeroNodePlanningProbeModel()

    @tool("planning_probe")
    def planning_probe(
        runtime: ToolRuntime[AssistantRunContext],
    ) -> str:
        """Return one generic offline planning sentinel."""

        del runtime
        return "planning-evidence-sentinel"

    probe = configure_builtin_tool(planning_probe, "read")
    catalog = SkillCatalog()
    shared_agent = build_fast_agent(model, [probe], skill_catalog=catalog)
    graph = build_planning_graph(
        model,
        shared_agent,
        tools=[probe],
        skill_catalog=catalog,
    )

    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [HumanMessage(content="zero-node-request-sentinel")],
                "memory_context": (),
                "memory_status": "empty",
            },
            context=AssistantRunContext(),
        )
    )

    assert result.get("frozen_worker_results", {}) == {}
    assert isinstance(result["messages"][-1], AIMessage)
    assert result["messages"][-1].content == "zero-node-final-answer-sentinel"


@pytest.mark.core_invariant("RUN-001")
@pytest.mark.core_invariant("LOOP-001")
def test_studio_input_without_execution_mode_defaults_to_fast(monkeypatch) -> None:
    """Catches Studio's standard messages-only input failing before the fast agent."""

    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    owner = asyncio.run(_open_owner())
    context = AssistantRunContext()

    try:
        result = asyncio.run(
            owner.graph.ainvoke(
                {"messages": [HumanMessage(content="studio-request-sentinel")]},
                context=context,
                config=_server_config(),
            )
        )
        public_input = AssistantRootInput.model_validate(
            {"messages": [HumanMessage(content="studio-request-sentinel")]}
        )

        assert public_input.execution_mode == "fast"
        assert isinstance(result["messages"][-1], AIMessage)
    finally:
        asyncio.run(owner.aclose())


@pytest.mark.core_invariant("RUN-001")
@pytest.mark.core_invariant("IDENT-001")
def test_public_input_separates_mode_from_non_identity_runtime_context() -> None:
    value = AssistantRootInput.model_validate(
        {
            "messages": [HumanMessage(content="request-sentinel")],
            "execution_mode": "fast",
        }
    )
    context = AssistantRunContext.model_validate({})

    coding = AssistantRootInput.model_validate(
        {
            "messages": [HumanMessage(content="coding-request-sentinel")],
            "execution_mode": "coding",
            "coding_repo_id": "repo-sentinel",
        }
    )

    assert coding.execution_mode == "coding"
    assert coding.coding_repo_id == "repo-sentinel"

    assert value.execution_mode == "fast"
    assert "run_type" not in type(value).model_fields
    assert set(type(context).model_fields) == {
        "assistant_execution_mode",
        "entry_profile",
        "media_capabilities",
        "realtime_media_mode",
        "visual_capability_token",
    }
    assert context.assistant_execution_mode is None
    with pytest.raises(ValidationError):
        AssistantRootInput.model_validate(
            {"messages": [], "execution_mode": "legacy-sentinel"}
        )
