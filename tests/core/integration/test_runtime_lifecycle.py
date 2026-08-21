from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langgraph.store.memory import InMemoryStore
from langgraph_sdk.auth.types import StudioUser
from pydantic import PrivateAttr, ValidationError
import pytest

from assistant_agent.agent_server.services import AgentServerExecutionOwner
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import build_fast_agent
from assistant_agent.native_agent.models import (
    NativePlanNode,
    NativePlanProposal,
)
from assistant_agent.native_agent.planning_graph import build_planning_graph
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


class _PlanningProbeModel(MockAssistantChatModel):
    objectives: tuple[str, ...]

    _active_workers: int = PrivateAttr(default=0)
    _max_active_workers: int = PrivateAttr(default=0)

    def with_structured_output(self, _schema: Any, **_kwargs: Any):
        async def propose(_messages):
            return NativePlanProposal(
                schema_version="native_plan_v1",
                nodes=tuple(
                    NativePlanNode(node_id=f"worker-{index}", objective=objective)
                    for index, objective in enumerate(self.objectives, start=1)
                ),
            )

        return RunnableLambda(propose)

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


def _probe_last_human_text(messages) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return ""


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
        coding_nodes = set(graph.nodes["coding_graph"].data.get_graph().nodes)
        assert "prepare_repair" in coding_nodes
        assert {
            "approval",
            "apply_patch",
            "run_validation",
            "create_commit",
            "prepare_merge",
            "merge_approval",
            "apply_merge",
        }.issubset(coding_nodes)
        assert owner.graph.checkpointer is None
    finally:
        asyncio.run(owner.aclose())


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
    model = _PlanningProbeModel(
        objectives=("worker-a-sentinel", "worker-b-sentinel")
    )
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
        item.work_item_id: item.content for item in result["worker_results"]
    }
    assert model._max_active_workers == 2
    assert "worker-a-sentinel" in contents["worker-1"]
    assert "worker-b-sentinel" not in contents["worker-1"]
    assert "worker-b-sentinel" in contents["worker-2"]
    assert "worker-a-sentinel" not in contents["worker-2"]


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
        {"messages": [HumanMessage(content="request-sentinel")], "execution_mode": "fast"}
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
        "entry_profile",
        "media_capabilities",
        "realtime_media_mode",
        "visual_capability_token",
    }
    with pytest.raises(ValidationError):
        AssistantRootInput.model_validate(
            {"messages": [], "execution_mode": "legacy-sentinel"}
        )
