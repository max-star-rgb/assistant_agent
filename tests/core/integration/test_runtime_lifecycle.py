from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.store.memory import InMemoryStore
from langgraph_sdk.auth.types import StudioUser
from pydantic import ValidationError

from assistant_agent.agent_server import services
from assistant_agent.agent_server.services import AgentServerExecutionOwner
from assistant_agent.coding import review as coding_review
from assistant_agent.coding.config import CodingRepositoryConfig
from assistant_agent.coding.models import CodingAnalysisSnapshot, CodingReviewInput
from assistant_agent.coding.workspace import CodingWorkspaceError
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.native_agent.state import AssistantRootInput


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
def test_parent_graph_has_native_fast_planning_and_coding_branches(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    owner = asyncio.run(_open_owner())
    try:
        graph = owner.graph.get_graph()
        nodes = {name for name in graph.nodes if not name.startswith("__error_handler__")}
        assert owner.graph.name == "AssistantRootGraph"
        assert nodes == {
            "__start__", "memory_recall", "execution_router", "fast_agent",
            "planning_agent", "coding_graph", "refresh_memory_extraction",
            "__end__",
        }
        assert graph.nodes["fast_agent"].data.name == "AssistantFastAgent"
        assert graph.nodes["planning_agent"].data.name == "AssistantPlanningAgent"
        planning = graph.nodes["planning_agent"].data.get_graph()
        assert {"model", "tools"} <= set(planning.nodes)
        assert not {"supervisor", "controls", "worker", "join"} & set(
            planning.nodes
        )
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
        coding = graph.nodes["coding_graph"].data.get_graph()
        coding_nodes = set(coding.nodes)
        analysis_super_step_nodes = {
            "prepare_analysis",
            "analyze_workspace",
            "join_analysis",
        }
        assert analysis_super_step_nodes.issubset(coding_nodes)
        coding_edges = {(edge.source, edge.target) for edge in coding.edges}
        assert {
            ("prepare_analysis", "analyze_workspace"),
            ("prepare_analysis", "join_analysis"),
            ("analyze_workspace", "join_analysis"),
            ("join_analysis", "inspect_and_draft"),
            ("inspect_and_draft", "validate_proposal"),
        } <= coding_edges
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
        } <= coding_edges
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
        review_repair_nodes = {
            "consume_review_repair_budget",
            "consume_review_repair_context",
        }
        assert review_repair_nodes.issubset(coding_nodes)
        assert {
            ("run_validation", "prepare_review_snapshot"),
            ("prepare_review_snapshot", "run_code_review"),
            ("run_code_review", "coding_review_decision"),
            ("coding_review_decision", "create_commit"),
            ("coding_review_decision", "summarize"),
        }.issubset(coding_edges)
        assert {
            target
            for source, target in coding_edges
            if source == "coding_review_decision"
        } == {"consume_review_repair_budget", "create_commit", "summarize"}
        assert {
            target
            for source, target in coding_edges
            if source == "consume_review_repair_budget"
        } == {"consume_review_repair_context", "summarize"}
        assert {
            target
            for source, target in coding_edges
            if source == "consume_review_repair_context"
        } == {"inspect_and_draft", "summarize"}
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
        } <= coding_edges
        assert {
            source for source, target in coding_edges if target == "create_commit"
        } == {"run_validation", "coding_review_decision"}
        assert owner.graph.checkpointer is None
    finally:
        asyncio.run(owner.aclose())


@pytest.mark.core_invariant("LOOP-001")
def test_production_composition_reuses_one_fast_agent_with_native_call_limits(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    monkeypatch.setenv("MAX_TOOL_ITERATIONS", "3")
    limits: list[tuple[int | None, int | None]] = []
    planning_limits: list[tuple[int | None, int | None]] = []
    fast_agents: list[object] = []
    planning_fast_agents: list[object] = []
    real_fast = services.build_fast_agent
    real_planning = services.build_planning_agent

    def recording_fast(*args: Any, **kwargs: Any):
        limits.append((kwargs.get("model_call_limit"), kwargs.get("tool_call_limit")))
        result = real_fast(*args, **kwargs)
        fast_agents.append(result)
        return result

    def recording_planning(*args: Any, **kwargs: Any):
        planning_fast_agents.append(args[1])
        planning_limits.append(
            (kwargs.get("model_call_limit"), kwargs.get("tool_call_limit"))
        )
        return real_planning(*args, **kwargs)

    monkeypatch.setattr(services, "build_fast_agent", recording_fast)
    monkeypatch.setattr(services, "build_planning_agent", recording_planning)
    owner = asyncio.run(_open_owner())
    try:
        assert limits == [(3, None)]
        assert planning_limits == [(3, None)]
        assert planning_fast_agents == fast_agents
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


@pytest.mark.core_invariant("RUN-001")
@pytest.mark.core_invariant("LOOP-001")
def test_fast_and_planning_modes_finish_with_standard_ai_messages(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    owner = asyncio.run(_open_owner())

    async def run_modes():
        return [
            await owner.graph.ainvoke(
                {"messages": [HumanMessage(content="request-sentinel")], "execution_mode": mode},
                context=AssistantRunContext(),
                config=_server_config(),
            )
            for mode in ("fast", "planning")
        ]

    try:
        results = asyncio.run(run_modes())
        assert all(isinstance(item["messages"][-1], AIMessage) for item in results)
        assert all("final_response" not in item for item in results)
    finally:
        asyncio.run(owner.aclose())


@pytest.mark.core_invariant("RUN-001")
@pytest.mark.core_invariant("LOOP-001")
def test_studio_messages_only_input_defaults_to_fast(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    owner = asyncio.run(_open_owner())
    try:
        result = asyncio.run(
            owner.graph.ainvoke(
                {"messages": [HumanMessage(content="studio-request-sentinel")]},
                context=AssistantRunContext(),
                config=_server_config(),
            )
        )
        assert AssistantRootInput.model_validate({"messages": []}).execution_mode == "fast"
        assert isinstance(result["messages"][-1], AIMessage)
    finally:
        asyncio.run(owner.aclose())


@pytest.mark.core_invariant("RUN-001")
@pytest.mark.core_invariant("IDENT-001")
def test_public_input_separates_mode_from_non_identity_runtime_context() -> None:
    value = AssistantRootInput.model_validate(
        {"messages": [HumanMessage(content="request-sentinel")], "execution_mode": "fast"}
    )
    coding = AssistantRootInput.model_validate(
        {"messages": [], "execution_mode": "coding", "coding_repo_id": "repo-sentinel"}
    )
    context = AssistantRunContext.model_validate({})
    assert value.execution_mode == "fast"
    assert coding.coding_repo_id == "repo-sentinel"
    assert set(type(context).model_fields) == {
        "assistant_execution_mode", "entry_profile", "media_capabilities",
        "realtime_media_mode", "visual_capability_token",
    }
    with pytest.raises(ValidationError):
        AssistantRootInput.model_validate({"messages": [], "execution_mode": "legacy-sentinel"})
