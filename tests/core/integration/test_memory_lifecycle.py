from __future__ import annotations

import asyncio
from contextlib import nullcontext
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
import pytest

from assistant_agent.native_agent import memory_graph as memory_graph_module
from assistant_agent.native_agent import memory as memory_module
from assistant_agent.config import ChatConfig, MemoryConfig
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.providers import MockAssistantChatModel
from assistant_agent.native_agent.state import AssistantAgentState
from scripts import run_server


class _User(dict):
    identity = "user-sentinel"
    permissions = ()


class _Memory:
    backend_id = "probe"

    def __init__(self) -> None:
        self.events: list[str] = []

    async def recall(self, **_kwargs: Any):
        self.events.append("recall")
        return ("memory-sentinel",)

    async def commit(self, **_kwargs: Any):
        self.events.append("commit")


class _FailingMemory(_Memory):
    async def recall(self, **_kwargs: Any):
        self.events.append("recall")
        raise ConnectionError("recall-failure-sentinel")


class _FailingCommitMemory(_Memory):
    async def commit(self, **_kwargs: Any):
        self.events.append("commit")
        raise ConnectionError("commit-failure-sentinel")


class _Runs:
    def __init__(self) -> None:
        self.cancellations: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []

    async def list(self, thread_id: str, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "run_id": f"memory-{thread_id}",
                "thread_id": thread_id,
                "assistant_id": "memory-assistant-sentinel",
                "status": "pending",
                "metadata": {"assistant_agent_run_kind": "memory_extraction"},
                "multitask_strategy": "enqueue",
            },
            {
                "run_id": f"chat-{thread_id}",
                "thread_id": thread_id,
                "assistant_id": "chat-assistant-sentinel",
                "status": "pending",
                "metadata": {},
                "multitask_strategy": "enqueue",
            },
        ]

    async def cancel(self, thread_id: str, run_id: str, **kwargs: Any) -> None:
        self.cancellations.append({"thread_id": thread_id, "run_id": run_id, **kwargs})

    async def create(self, **kwargs: Any) -> None:
        self.requests.append(kwargs)


class _Threads:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        return {"thread_id": kwargs["thread_id"]}


class _FailingThreads(_Threads):
    async def create(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        raise ConnectionError("refresh-failure-sentinel")


class _Client:
    def __init__(self) -> None:
        self.threads = _Threads()
        self.runs = _Runs()


class _FailingRefreshClient(_Client):
    def __init__(self) -> None:
        super().__init__()
        self.threads = _FailingThreads()


class _PagedRuns(_Runs):
    def __init__(self) -> None:
        super().__init__()
        self.pending = [
            {
                "run_id": f"memory-{index}",
                "metadata": {"assistant_agent_run_kind": "memory_extraction"},
            }
            for index in range(101)
        ]

    async def list(
        self,
        _thread_id: str,
        *,
        limit: int,
        offset: int,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        return self.pending[offset : offset + limit]

    async def cancel(self, thread_id: str, run_id: str, **kwargs: Any) -> None:
        await super().cancel(thread_id, run_id, **kwargs)
        self.pending = [run for run in self.pending if run["run_id"] != run_id]


class _PagedClient(_Client):
    def __init__(self) -> None:
        super().__init__()
        self.runs = _PagedRuns()


def _assistant_graph(backend: _Memory, *, checkpointer=None):
    middleware_module = importlib.import_module(
        "assistant_agent.native_agent.memory_middleware"
    )
    return create_agent(
        model=MockAssistantChatModel(),
        tools=[],
        state_schema=AssistantAgentState,
        context_schema=AssistantRunContext,
        middleware=[middleware_module.MemoryLifecycleMiddleware(backend)],
        checkpointer=checkpointer,
        name="AssistantAgent",
    )


@pytest.mark.core_invariant("MEMORY-001")
def test_unified_chat_recall_once_then_rolls_back_and_enqueues_extraction(
    monkeypatch,
) -> None:
    client = _Client()
    backend = _Memory()
    middleware_module = importlib.import_module(
        "assistant_agent.native_agent.memory_middleware"
    )
    monkeypatch.setattr(middleware_module, "get_client", lambda: client)
    graph = _assistant_graph(backend, checkpointer=InMemorySaver())

    config = {
        "configurable": {
            "thread_id": "thread-sentinel",
            "assistant_id": "assistant-sentinel",
            "graph_id": "graph-sentinel",
            "langgraph_auth_user": _User(),
        }
    }

    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [
                    HumanMessage(
                        content=[
                            {"type": "text", "text": "request-sentinel"},
                            {
                                "type": "file",
                                "source": "uploaded",
                                "id": "legacy-video-id",
                            },
                        ]
                    )
                ]
            },
            context=AssistantRunContext(),
            config=config,
        )
    )

    memory_thread_id = str(
        uuid5(NAMESPACE_URL, "assistant-agent:memory:thread-sentinel")
    )
    assert backend.events == ["recall"]
    assert "memory_context" not in result
    assert tuple(graph.get_state(config).values["memory_context"]) == (
        "memory-sentinel",
    )
    assert "trusted_runtime_facts" not in result
    assert client.threads.requests == [
        {
            "thread_id": memory_thread_id,
            "graph_id": "assistant-memory-v1",
            "metadata": {"assistant_agent_source_thread_id": "thread-sentinel"},
            "if_exists": "do_nothing",
        }
    ]
    assert client.runs.cancellations == [
        {
            "thread_id": memory_thread_id,
            "run_id": f"memory-{memory_thread_id}",
            "wait": True,
            "action": "rollback",
        }
    ]
    request = client.runs.requests[0]
    assert request["thread_id"] == memory_thread_id
    assert request["assistant_id"] == "assistant-memory-v1"
    assert request["metadata"] == {"assistant_agent_run_kind": "memory_extraction"}
    assert request["after_seconds"] == 1800
    assert request["multitask_strategy"] == "enqueue"
    assert request["input"]["messages"][0].content == [
        {"type": "text", "text": "request-sentinel"}
    ]


@pytest.mark.core_invariant("MEMORY-001")
def test_memory_refresh_rolls_back_every_pending_page_before_enqueue(
    monkeypatch,
) -> None:
    client = _PagedClient()
    middleware_module = importlib.import_module(
        "assistant_agent.native_agent.memory_middleware"
    )
    monkeypatch.setattr(middleware_module, "get_client", lambda: client)

    asyncio.run(
        _assistant_graph(_Memory()).ainvoke(
            {"messages": [HumanMessage(content="request-sentinel")]},
            context=AssistantRunContext(),
            config={
                "configurable": {
                    "thread_id": "thread-paged-sentinel",
                    "langgraph_auth_user": _User(),
                }
            },
        )
    )

    assert len(client.runs.cancellations) == 101
    assert len(client.runs.requests) == 1


@pytest.mark.core_invariant("MEMORY-001")
def test_disable_memory_skips_recall_and_extraction(monkeypatch) -> None:
    client = _Client()
    backend = _Memory()
    middleware_module = importlib.import_module(
        "assistant_agent.native_agent.memory_middleware"
    )
    monkeypatch.setattr(middleware_module, "get_client", lambda: client)
    graph = _assistant_graph(backend)

    result = asyncio.run(
        graph.ainvoke(
            {"messages": [HumanMessage(content="request-sentinel")]},
            context=AssistantRunContext(enable_memory=False),
            config={"configurable": {"thread_id": "thread-disabled-sentinel"}},
        )
    )

    assert backend.events == []
    assert "memory_context" not in result
    assert client.threads.requests == []
    assert client.runs.cancellations == []
    assert client.runs.requests == []


@pytest.mark.core_invariant("MEMORY-001")
def test_recall_reports_error_after_native_retries() -> None:
    backend = _FailingMemory()
    graph = _assistant_graph(backend)

    with pytest.raises(ConnectionError, match="recall-failure-sentinel"):
        asyncio.run(
            graph.ainvoke(
                {"messages": [HumanMessage(content="request-sentinel")]},
                context=AssistantRunContext(),
                config={
                    "configurable": {
                        "thread_id": "thread-degraded-sentinel",
                        "assistant_id": "assistant-sentinel",
                        "graph_id": "graph-sentinel",
                        "langgraph_auth_user": _User(),
                    }
                },
            )
        )

    assert backend.events == ["recall", "recall", "recall"]


@pytest.mark.core_invariant("MEMORY-001")
def test_refresh_reports_error_after_native_retries(monkeypatch) -> None:
    client = _FailingRefreshClient()
    middleware_module = importlib.import_module(
        "assistant_agent.native_agent.memory_middleware"
    )
    monkeypatch.setattr(middleware_module, "get_client", lambda: client)
    graph = _assistant_graph(_Memory())

    with pytest.raises(ConnectionError, match="refresh-failure-sentinel"):
        asyncio.run(
            graph.ainvoke(
                {"messages": [HumanMessage(content="request-sentinel")]},
                context=AssistantRunContext(),
                config={
                    "configurable": {
                        "thread_id": "thread-refresh-failure-sentinel",
                        "langgraph_auth_user": _User(),
                    }
                },
            )
        )

    assert len(client.threads.requests) == 3


@pytest.mark.core_invariant("MEMORY-001")
def test_independent_memory_graph_extracts_without_recall_or_agent() -> None:
    backend = _Memory()
    graph = memory_graph_module.build_memory_extraction_graph(backend=backend)

    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [
                    HumanMessage(content="request-sentinel"),
                    AIMessage(content="existing-answer-sentinel"),
                ],
            },
            context=AssistantRunContext(),
            config={
                "configurable": {
                    "assistant_id": "assistant-sentinel",
                    "graph_id": "graph-sentinel",
                    "langgraph_auth_user": _User(),
                }
            },
        )
    )

    assert backend.events == ["commit"]
    assert [message.content for message in result["messages"]] == [
        "request-sentinel",
        "existing-answer-sentinel",
    ]


@pytest.mark.core_invariant("MEMORY-001")
def test_independent_memory_graph_reports_error_after_native_retries() -> None:
    backend = _FailingCommitMemory()
    graph = memory_graph_module.build_memory_extraction_graph(backend=backend)

    with pytest.raises(ConnectionError, match="commit-failure-sentinel"):
        asyncio.run(
            graph.ainvoke(
                {
                    "messages": [
                        HumanMessage(content="request-sentinel"),
                        AIMessage(content="existing-answer-sentinel"),
                    ],
                },
                context=AssistantRunContext(),
                config={
                    "configurable": {
                        "assistant_id": "assistant-sentinel",
                        "graph_id": "graph-sentinel",
                        "langgraph_auth_user": _User(),
                    }
                },
            )
        )

    assert backend.events == ["commit", "commit", "commit"]


@pytest.mark.core_invariant("MEMORY-001")
def test_langmem_manager_uses_a_durable_structured_schema(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def create_manager(_model, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        memory_module.importlib,
        "import_module",
        lambda _name: SimpleNamespace(create_memory_store_manager=create_manager),
    )
    monkeypatch.setattr(
        memory_module, "create_chat_model", lambda *_args, **_kwargs: object()
    )

    memory_module._create_langmem_manager(
        MemoryConfig(memory_backend="langmem", langmem_model="memory-model"),
        provider_mode="real",
        chat_config=ChatConfig(),
        store=InMemoryStore(),
    )

    schema = captured["schemas"][0]
    assert set(schema.model_json_schema()["properties"]) == {"content", "kind"}
    assert schema.model_validate({"content": "stable-sentinel", "kind": "stable_fact"})


@pytest.mark.core_invariant("MEMORY-001")
def test_dev_server_keeps_capacity_for_chat_while_memory_extracts(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(run_server, "hold_dev_server_lock", lambda: nullcontext())
    monkeypatch.setattr(run_server, "require_available_port", lambda *_args: None)

    def capture_command(command, **_kwargs):
        captured["command"] = list(command)
        config_index = captured["command"].index("--config")
        config_path = Path(captured["command"][config_index + 1])
        captured["config"] = json.loads(config_path.read_text(encoding="utf-8"))
        return 0

    monkeypatch.setattr(run_server, "run_command_with_log", capture_command)

    assert run_server.main(["--backend", "dev", "--no-env-file"]) == 0
    option_index = captured["command"].index("--n-jobs-per-worker")
    assert int(captured["command"][option_index + 1]) >= 2
    assert captured["config"]["env"] == {}
