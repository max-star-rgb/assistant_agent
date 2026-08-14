from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.store.memory import InMemoryStore
from langgraph_sdk.auth.types import StudioUser
from pydantic import ValidationError
import pytest

from assistant_agent.agent_server.services import AgentServerExecutionOwner
from assistant_agent.native_agent.context import AssistantRunContext
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
    finally:
        asyncio.run(owner.aclose())


@pytest.mark.core_invariant("LOOP-001")
def test_parent_graph_has_fast_and_planning_native_branches(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    owner = asyncio.run(_open_owner())
    try:
        nodes = set(owner.graph.get_graph().nodes)
        assert owner.graph.name == "AssistantRootGraph"
        assert {"fast_agent", "planning_graph", "memory_recall", "memory_commit"} <= nodes
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


@pytest.mark.core_invariant("RUN-001")
@pytest.mark.core_invariant("IDENT-001")
def test_public_input_separates_mode_from_non_identity_runtime_context() -> None:
    value = AssistantRootInput.model_validate(
        {"messages": [HumanMessage(content="request-sentinel")], "execution_mode": "fast"}
    )
    context = AssistantRunContext.model_validate({})

    assert value.execution_mode == "fast"
    assert set(type(context).model_fields) == {
        "entry_profile",
        "media_capabilities",
    }
    with pytest.raises(ValidationError):
        AssistantRootInput.model_validate(
            {"messages": [], "execution_mode": "legacy-sentinel"}
        )
