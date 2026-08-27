from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from blockbuster import blockbuster_ctx
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from langgraph_sdk.auth.types import StudioUser
from pydantic import PrivateAttr, ValidationError

from assistant_agent.coding import config as coding_config_module
from assistant_agent.agent_server import services
from assistant_agent.agent_server.services import AgentServerExecutionOwner
from assistant_agent.native_agent import coding_agent as coding_agent_module
from assistant_agent.native_agent.coding_agent import build_coding_agent
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import RecursionFinalSynthesisMiddleware
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.native_agent.state import AssistantRootInput, FastAgentState
from assistant_agent.native_agent.tool_call_limits import PerToolCallLimitMiddleware


class _CodingWriteModel(MockAssistantChatModel):
    _calls: int = PrivateAttr(default=0)

    def _response_message(self, messages, **kwargs):
        del messages, kwargs
        self._calls += 1
        if self._calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {
                            "file_path": "/approved.txt",
                            "content": "approved-sentinel",
                        },
                        "id": "write-file-sentinel",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="finished-sentinel")


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
def test_parent_graph_has_native_fast_planning_and_coding_branches(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    owner = asyncio.run(_open_owner())
    try:
        graph = owner.graph.get_graph()
        nodes = {name for name in graph.nodes if not name.startswith("__error_handler__")}
        assert owner.graph.name == "AssistantRootGraph"
        assert nodes == {
            "__start__", "memory_recall", "execution_router", "fast_agent",
            "planning_agent", "coding_agent", "refresh_memory_extraction",
            "__end__",
        }
        assert graph.nodes["fast_agent"].data.name == "AssistantFastAgent"
        assert graph.nodes["planning_agent"].data.name == "AssistantPlanningAgent"
        async_tool_names = {
            "start_async_task",
            "check_async_task",
            "update_async_task",
            "cancel_async_task",
            "list_async_tasks",
        }
        assert async_tool_names <= set(
            graph.nodes["fast_agent"].data.get_graph().nodes["tools"].data.tools_by_name
        )
        assert async_tool_names <= set(
            graph.nodes["planning_agent"].data.get_graph().nodes["tools"].data.tools_by_name
        )
        assert owner.worker_graph.name == "AssistantBackgroundWorker"
        worker_tool_names = set(
            owner.worker_graph.get_graph().nodes["tools"].data.tools_by_name
        )
        assert not async_tool_names & worker_tool_names
        assert all(
            (tool.metadata or {}).get("effect") == "read"
            for tool in owner.tools
            if tool.name in worker_tool_names
        )
        planning = graph.nodes["planning_agent"].data.get_graph()
        assert {"model", "tools"} <= set(planning.nodes)
        assert not {"supervisor", "controls", "worker", "join"} & set(
            planning.nodes
        )
        coding = graph.nodes["coding_agent"].data
        assert coding.name == "AssistantCodingAgent"
        coding_tools = set(coding.get_graph().nodes["tools"].data.tools_by_name)
        assert {
            "write_todos", "ls", "read_file", "write_file", "edit_file",
            "delete", "glob", "grep", "execute", "task",
        } <= coding_tools
        assert not {
            "coding_propose_patch", "coding_patch_apply", "run_validation",
            "create_commit", "apply_merge",
        } & coding_tools
        assert owner.graph.checkpointer is None
    finally:
        asyncio.run(owner.aclose())


@pytest.mark.core_invariant("LOOP-001")
def test_coding_agent_uses_only_tool_call_policy(
    monkeypatch,
) -> None:
    captured: list[object] = []

    def recording_create_deep_agent(*args: Any, **kwargs: Any):
        del args
        captured.extend(kwargs["middleware"])
        return object()

    monkeypatch.setattr(
        coding_agent_module,
        "create_deep_agent",
        recording_create_deep_agent,
    )
    build_coding_agent(
        MockAssistantChatModel(),
        object(),
        repo_id="repo-sentinel",
    )

    model_limits = [
        item for item in captured if isinstance(item, ModelCallLimitMiddleware)
    ]
    tool_limits = [
        item for item in captured if isinstance(item, PerToolCallLimitMiddleware)
    ]
    finalizers = [
        item
        for item in captured
        if isinstance(item, RecursionFinalSynthesisMiddleware)
    ]
    assert model_limits == []
    assert [item.max_parallel_calls_per_tool for item in tool_limits] == [12]
    assert [item.step_reserve for item in finalizers] == [8]


@pytest.mark.core_invariant("LOOP-001")
def test_production_composition_reuses_fast_configuration_without_run_call_limits(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    limits: list[tuple[int | None, int | None]] = []
    planning_limits: list[tuple[int | None, int | None]] = []
    fast_agents: list[object] = []
    fast_names: list[str | None] = []
    planning_fast_agents: list[object] = []
    real_fast = services.build_fast_agent
    real_planning = services.build_planning_agent

    def recording_fast(*args: Any, **kwargs: Any):
        limits.append((kwargs.get("model_call_limit"), kwargs.get("tool_call_limit")))
        fast_names.append(kwargs.get("name"))
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
        assert limits == [(None, None), (None, None)]
        assert fast_names == [None, "AssistantBackgroundWorker"]
        assert planning_limits == [(None, None)]
        assert planning_fast_agents == [fast_agents[0]]
    finally:
        asyncio.run(owner.aclose())


@pytest.mark.core_invariant("RUN-001")
@pytest.mark.core_invariant("LOOP-001")
def test_both_modes_finish_with_standard_ai_messages(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    owner = asyncio.run(_open_owner())

    async def run_modes():
        return [
            await owner.graph.ainvoke(
                {"messages": [HumanMessage(content="request-sentinel")]},
                context=AssistantRunContext(execution_mode=mode),
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
@pytest.mark.parametrize("execution_mode", ["fast", "planning", "coding"])
def test_runtime_context_selects_execution_route(
    monkeypatch,
    execution_mode: str,
) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    owner = asyncio.run(_open_owner())
    try:
        result = asyncio.run(
            owner.graph.ainvoke(
                {"messages": [HumanMessage(content="request-sentinel")]},
                context=AssistantRunContext(
                    execution_mode=execution_mode,
                ),
                config=_server_config(),
            )
        )
        assert result["execution_mode"] == execution_mode
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
                {"messages": [HumanMessage(content="request-sentinel")]},
                context=AssistantRunContext(execution_mode=mode),
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
        assert AssistantRunContext().execution_mode == "fast"
        assert isinstance(result["messages"][-1], AIMessage)
    finally:
        asyncio.run(owner.aclose())


@pytest.mark.core_invariant("RUN-001")
@pytest.mark.core_invariant("IDENT-001")
def test_public_input_separates_mode_from_non_identity_runtime_context() -> None:
    value = AssistantRootInput.model_validate(
        {"messages": [HumanMessage(content="request-sentinel")]}
    )
    context = AssistantRunContext.model_validate({"execution_mode": "coding"})
    assert len(value.messages) == 1
    assert set(type(value).model_fields) == {"messages"}
    assert context.execution_mode == "coding"
    assert set(type(context).model_fields) == {"execution_mode", "enable_memory"}
    assert context.enable_memory is True
    mode_schema = AssistantRunContext.model_json_schema()["properties"][
        "execution_mode"
    ]
    memory_schema = AssistantRunContext.model_json_schema()["properties"][
        "enable_memory"
    ]
    assert mode_schema["enum"] == ["fast", "planning", "coding"]
    assert memory_schema["default"] is True
    with pytest.raises(ValidationError):
        AssistantRootInput.model_validate({"messages": [], "execution_mode": "fast"})
    with pytest.raises(ValidationError):
        AssistantRunContext.model_validate({"execution_mode": "legacy-sentinel"})


@pytest.mark.core_invariant("LOOP-001")
@pytest.mark.parametrize("decision, expected_write", [("approve", True), ("reject", False)])
def test_coding_mutation_interrupts_before_execution_and_resumes_once(
    tmp_path: Path,
    decision: str,
    expected_write: bool,
) -> None:
    class WorkspaceService:
        def resolve(self, identity: str, thread_id: str, repo_id: str):
            assert (identity, thread_id, repo_id) == (
                "user-sentinel",
                f"coding-hitl-{decision}",
                "repo-sentinel",
            )
            return SimpleNamespace(root=tmp_path)

    agent = build_coding_agent(
        _CodingWriteModel(),
        WorkspaceService(),
        repo_id="repo-sentinel",
    )
    builder = StateGraph(FastAgentState, context_schema=AssistantRunContext)
    builder.add_node("coding", agent)
    builder.add_edge(START, "coding")
    builder.add_edge("coding", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {
        "configurable": {
            "thread_id": f"coding-hitl-{decision}",
            "langgraph_auth_user": SimpleNamespace(identity="user-sentinel"),
        }
    }
    context = AssistantRunContext(execution_mode="coding")

    interrupted = graph.invoke(
        {"messages": [HumanMessage(content="write-sentinel")]},
        config=config,
        context=context,
    )
    output = tmp_path / "approved.txt"
    assert interrupted["__interrupt__"][0].value["action_requests"][0][
        "name"
    ] == "write_file"
    assert not output.exists()

    resume = {"decisions": [{"type": decision}]}
    if decision == "reject":
        resume["decisions"][0]["message"] = "rejected-sentinel"
    resumed = graph.invoke(
        Command(resume=resume),
        config=config,
        context=context,
    )
    assert output.exists() is expected_write
    if expected_write:
        assert output.read_text(encoding="utf-8") == "approved-sentinel"
    assert resumed["messages"][-1].content == "finished-sentinel"
