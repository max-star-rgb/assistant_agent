from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

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
from assistant_agent.agent_server import async_delegation
from assistant_agent.agent_server.async_delegation import (
    BACKGROUND_AGENT_NAME,
    build_async_subagent_middleware,
)
from assistant_agent.coding import config as coding_config_module
from assistant_agent.native_agent import assistant_agent as assistant_agent_module
from assistant_agent.native_agent.assistant_agent import (
    RecursionFinalSynthesisMiddleware,
    build_assistant_agent,
)
from assistant_agent.native_agent.context import (
    ASSISTANT_RUNTIME_METADATA_KEY,
    AssistantRunContext,
)
from assistant_agent.native_agent.providers import MockAssistantChatModel
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
def test_main_graph_is_the_native_deep_agent(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    owner = asyncio.run(_open_owner())
    try:
        graph = owner.graph.get_graph()
        nodes = set(graph.nodes)
        assert {
            "MemoryLifecycleMiddleware.before_agent",
            "model",
            "tools",
            "MemoryLifecycleMiddleware.after_agent",
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
        assert "assistant_agent" not in nodes
        tools = set(graph.nodes["tools"].data.tools_by_name)
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
        assert owner.graph.name == "AssistantAgent"
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
    context = AssistantRunContext.model_validate({"enable_memory": False})
    assert set(type(context).model_fields) == {"enable_memory"}
    assert context.enable_memory is False
    assert AssistantRunContext.model_json_schema()["properties"]["enable_memory"]["default"] is True
    with pytest.raises(ValidationError):
        AssistantRunContext.model_validate({"execution_mode": "fast"})


@pytest.mark.core_invariant("RUN-001")
@pytest.mark.core_invariant("IDENT-001")
def test_native_assistant_input_schema_exposes_only_messages(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    owner = asyncio.run(_open_owner())
    try:
        schema = owner.graph.get_input_jsonschema()
        assert set(schema["properties"]) == {"messages"}
        assert schema["required"] == ["messages"]
    finally:
        asyncio.run(owner.aclose())


@pytest.mark.core_invariant("LOOP-001")
def test_async_task_reuses_the_creation_snapshot_for_later_worker_runs(
    monkeypatch,
) -> None:
    snapshot = "a" * 40
    moved_head = "b" * 40
    observed_heads: list[str] = []

    class WorkspaceService:
        head = snapshot

        def repository_head(self, repo_id: str) -> str:
            assert repo_id == "repo-sentinel"
            observed_heads.append(self.head)
            return self.head

    client = SimpleNamespace(
        threads=SimpleNamespace(
            create=AsyncMock(return_value={"thread_id": "worker-thread"})
        ),
        runs=SimpleNamespace(
            create=AsyncMock(
                side_effect=[{"run_id": "worker-run"}, {"run_id": "worker-run-2"}]
            )
        ),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(async_delegation, "get_client", lambda **_kwargs: client)
    service = WorkspaceService()
    middleware = build_async_subagent_middleware(service, "repo-sentinel")
    start = next(tool for tool in middleware.tools if tool.name == "start_async_task")

    def runtime(async_tasks=None):
        return SimpleNamespace(
            state={
                "memory_context": ("memory-sentinel",),
                "async_tasks": async_tasks or {},
            },
            config={
                "configurable": {"thread_id": "parent-thread"},
                "run_id": "parent-run",
            },
            tool_call_id="tool-call-sentinel",
            server_info=SimpleNamespace(user=SimpleNamespace(identity="user-sentinel")),
        )

    started = asyncio.run(
        start.coroutine(
            description="task-sentinel",
            subagent_type=BACKGROUND_AGENT_NAME,
            runtime=runtime(),
        )
    )
    task = next(iter(started.update["async_tasks"].values()))
    service.head = moved_head
    asyncio.run(
        async_delegation._update_async_task(
            task["task_id"],
            "follow-up-sentinel",
            runtime({task["task_id"]: task}),
        )
    )

    def metadata_snapshot(call) -> str:
        return call.kwargs["metadata"][ASSISTANT_RUNTIME_METADATA_KEY][
            "repository_snapshot_sha"
        ]

    assert observed_heads == [snapshot]
    assert task["repository_snapshot_sha"] == snapshot
    assert metadata_snapshot(client.threads.create.await_args) == snapshot
    assert metadata_snapshot(client.runs.create.await_args_list[0]) == snapshot
    assert metadata_snapshot(client.runs.create.await_args_list[1]) == snapshot
