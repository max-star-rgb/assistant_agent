from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from blockbuster import blockbuster_ctx
from deepagents.backends import FilesystemBackend, LocalShellBackend
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
from assistant_agent.runtime import thread_resources as thread_resources_module
from assistant_agent.runtime.local_backend import create_local_backend
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
    with blockbuster_ctx(scanned_modules=thread_resources_module):
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
        assert owner.worker_graph.name == "AssistantGeneralPurposeWorker"
        assert owner.graph.checkpointer is None
    finally:
        asyncio.run(owner.aclose())


@pytest.mark.core_invariant("LOOP-001")
def test_local_shell_backend_resolves_cwd_and_os_absolute_paths(tmp_path: Path) -> None:
    home_root = tmp_path / "home"
    agent_home = home_root / "assistant_agent"
    agent_home.mkdir(parents=True)
    (agent_home / "cwd-sentinel.txt").write_text("cwd", encoding="utf-8")
    host_file = tmp_path / "host-sentinel.txt"
    host_file.write_text("host", encoding="utf-8")
    manager = thread_resources_module.ThreadResourceManager(
        thread_resources_module.ThreadResourceConfig(root=tmp_path / "threads")
    )
    backend = create_local_backend(manager, agent_home=agent_home).default

    assert isinstance(backend, LocalShellBackend)
    assert {entry["path"] for entry in backend.ls(".").entries or []} == {
        str(agent_home / "cwd-sentinel.txt")
    }
    assert "/tmp/" in {entry["path"] for entry in backend.ls("/.").entries or []}
    assert backend.read(str(host_file)).file_data["content"] == "host"
    assert backend.execute("pwd").output.strip() == str(agent_home)


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
    context = AssistantRunContext.model_validate(
        {"enable_memory": False, "require_tool_approval": False}
    )
    assert set(type(context).model_fields) == {
        "enable_memory",
        "require_tool_approval",
    }
    assert context.enable_memory is False
    assert context.require_tool_approval is False
    assert (
        AssistantRunContext.model_json_schema()["properties"]["enable_memory"][
            "default"
        ]
        is True
    )
    assert (
        AssistantRunContext.model_json_schema()["properties"][
            "require_tool_approval"
        ]["default"]
        is True
    )
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
def test_async_task_tracks_parent_correlation_without_workspace_state(
    monkeypatch,
) -> None:
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
    middleware = build_async_subagent_middleware()
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
                "metadata": {
                    ASSISTANT_RUNTIME_METADATA_KEY: {
                        "entry_profile": "agent_server",
                    }
                },
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
    asyncio.run(
        async_delegation._update_async_task(
            task["task_id"],
            "follow-up-sentinel",
            runtime({task["task_id"]: task}),
        )
    )

    assert "workspace_id" not in task
    for call in (
        client.threads.create.await_args,
        *client.runs.create.await_args_list,
    ):
        assert call.kwargs["metadata"][ASSISTANT_RUNTIME_METADATA_KEY] == {
            "entry_profile": "async_worker"
        }
