from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from blockbuster import blockbuster_ctx
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langgraph.store.memory import InMemoryStore
from langgraph_sdk.auth.types import StudioUser
from pydantic import ValidationError

from assistant_agent.agent_server.services import AgentServerExecutionOwner
from assistant_agent.coding import config as coding_config_module
from assistant_agent.native_agent import assistant_agent as assistant_agent_module
from assistant_agent.native_agent.assistant_agent import (
    RecursionFinalSynthesisMiddleware,
    build_assistant_agent,
)
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.native_agent.state import AssistantRootInput
from assistant_agent.native_agent.tool_call_limits import PerToolCallLimitMiddleware


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


async def _open_owner_without_event_loop_blocking() -> AgentServerExecutionOwner:
    with blockbuster_ctx(scanned_modules=coding_config_module):
        return await AgentServerExecutionOwner.compose(store=InMemoryStore())


@pytest.mark.core_invariant("BOOT-001")
def test_mock_composition_opens_without_real_provider(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    owner = asyncio.run(_open_owner_without_event_loop_blocking())
    try:
        assert owner.model._llm_type == "assistant-agent-mock"
        assert owner.memory_backend.backend_id == "disabled"
        assert owner.memory_graph.name == "AssistantMemoryExtractionGraph"
    finally:
        asyncio.run(owner.aclose())


@pytest.mark.core_invariant("LOOP-001")
def test_parent_graph_has_one_native_deep_agent_route(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    owner = asyncio.run(_open_owner())
    try:
        graph = owner.graph.get_graph()
        nodes = set(graph.nodes)
        assert {
            "memory_recall",
            "assistant_agent",
            "refresh_memory_extraction",
        } <= nodes
        assert (
            not {
                "execution_router",
                "fast_agent",
                "planning_agent",
                "coding_agent",
            }
            & nodes
        )
        assistant = graph.nodes["assistant_agent"].data
        tools = set(assistant.get_graph().nodes["tools"].data.tools_by_name)
        assert {
            "write_todos",
            "task",
            "ls",
            "read_file",
            "write_file",
            "edit_file",
            "delete",
            "glob",
            "grep",
            "execute",
        } <= tools
        assert owner.graph.name == "AssistantRootGraph"
        assert assistant.name == "AssistantAgent"
        assert owner.worker_graph.name == "AssistantReadOnlyWorker"
        assert owner.graph.checkpointer is None
    finally:
        asyncio.run(owner.aclose())


@pytest.mark.core_invariant("LOOP-001")
def test_assistant_agent_has_bounded_parallel_tools_and_final_synthesis(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: list[object] = []

    def recording_create_deep_agent(*args: Any, **kwargs: Any):
        del args
        captured.extend(kwargs["middleware"])
        return object()

    monkeypatch.setattr(
        assistant_agent_module,
        "create_deep_agent",
        recording_create_deep_agent,
    )
    build_assistant_agent(
        MockAssistantChatModel(),
        [],
        backend=object(),
        worker_graph=RunnableLambda(lambda state: state),
        skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
    )

    assert not any(isinstance(item, ModelCallLimitMiddleware) for item in captured)
    assert [
        item.max_parallel_calls_per_tool
        for item in captured
        if isinstance(item, PerToolCallLimitMiddleware)
    ] == [12]
    assert [
        item.step_reserve
        for item in captured
        if isinstance(item, RecursionFinalSynthesisMiddleware)
    ] == [8]


@pytest.mark.core_invariant("RUN-001")
@pytest.mark.core_invariant("LOOP-001")
def test_unified_run_finishes_with_standard_ai_message(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    owner = asyncio.run(_open_owner())
    try:
        result = asyncio.run(
            owner.graph.ainvoke(
                {"messages": [HumanMessage(content="request-sentinel")]},
                context=AssistantRunContext(),
                config=_server_config(),
            )
        )
        assert isinstance(result["messages"][-1], AIMessage)
        assert "final_response" not in result
    finally:
        asyncio.run(owner.aclose())


@pytest.mark.core_invariant("RUN-001")
@pytest.mark.core_invariant("IDENT-001")
def test_public_input_and_context_expose_no_private_run_facts() -> None:
    value = AssistantRootInput.model_validate(
        {"messages": [HumanMessage(content="request-sentinel")]}
    )
    context = AssistantRunContext.model_validate({"enable_memory": False})

    assert len(value.messages) == 1
    assert set(type(value).model_fields) == {"messages"}
    assert set(type(context).model_fields) == {"enable_memory"}
    assert context.enable_memory is False
    assert (
        AssistantRunContext.model_json_schema()["properties"]["enable_memory"][
            "default"
        ]
        is True
    )
    with pytest.raises(ValidationError):
        AssistantRootInput.model_validate({"messages": [], "execution_mode": "fast"})
    with pytest.raises(ValidationError):
        AssistantRunContext.model_validate({"execution_mode": "fast"})
