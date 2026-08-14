"""RED/GREEN coverage for the native parent-graph memory boundary."""

from __future__ import annotations

import asyncio
from functools import partial
import hashlib
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import NodeError
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime, ServerInfo
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command

from assistant_agent.config import ProviderConfig
from assistant_agent.memory.mem0.models import (
    Mem0IngestionResult,
    Mem0RecallMemory,
)
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.memory import (
    MemoryBackendConfigurationError,
    create_memory_backend,
    memory_commit_degraded,
    memory_commit_node,
    memory_recall_degraded,
    memory_recall_node,
)
from assistant_agent.native_agent.state import AssistantRootState, WorkerState


class ProbeBackend:
    backend_id = "probe"

    def __init__(self, *, fail_commit: bool = False) -> None:
        self.fail_commit = fail_commit
        self.recall_calls: list[dict[str, Any]] = []
        self.commit_calls: list[dict[str, Any]] = []

    async def recall(self, **kwargs: Any) -> tuple[str, ...]:
        self.recall_calls.append(kwargs)
        return ("用户喜欢简洁回答",)

    async def commit(self, **kwargs: Any) -> None:
        self.commit_calls.append(kwargs)
        if self.fail_commit:
            raise RuntimeError("third-party unavailable")


class _User(dict):
    identity = "user-1"
    permissions = ()


def _server_info() -> ServerInfo:
    return ServerInfo(
        assistant_id="assistant-native-v1",
        graph_id="assistant-native-v1",
        user=_User(),
    )


def _run_memory_graph(backend: ProbeBackend) -> dict[str, Any]:
    async def answer(_state: AssistantRootState) -> dict[str, Any]:
        return {"messages": [AIMessage(content="最终回答")]}

    builder = StateGraph(AssistantRootState, context_schema=AssistantRunContext)
    builder.add_node("recall", partial(memory_recall_node, backend=backend))
    builder.add_node("answer", answer)
    builder.add_node(
        "commit",
        partial(memory_commit_node, backend=backend),
        error_handler=memory_commit_degraded,
    )
    builder.add_edge(START, "recall")
    builder.add_edge("recall", "answer")
    builder.add_edge("answer", "commit")
    builder.add_edge("commit", END)
    graph = builder.compile(store=InMemoryStore())
    return asyncio.run(
        graph.ainvoke(
            {
                "messages": [HumanMessage(content="你好")],
                "execution_mode": "fast",
            },
            config={
                "configurable": {
                    "thread_id": "thread-1",
                    "assistant_id": "assistant-native-v1",
                    "graph_id": "assistant-native-v1",
                    "langgraph_auth_user": _User(),
                }
            },
            context=AssistantRunContext(),
        )
    )


def test_custom_backend_runs_once_at_parent_graph_boundaries() -> None:
    backend = ProbeBackend()

    result = _run_memory_graph(backend)

    assert result["memory_context"] == ("用户喜欢简洁回答",)
    assert result["memory_status"] == "ready"
    assert len(backend.recall_calls) == 1
    assert len(backend.commit_calls) == 1
    assert backend.recall_calls[0]["identity"] == "user-1"
    assert backend.recall_calls[0]["thread_id"] == "thread-1"
    assert backend.commit_calls[0]["thread_id"] == "thread-1"


def test_commit_failure_preserves_final_ai_message() -> None:
    backend = ProbeBackend(fail_commit=True)

    result = _run_memory_graph(backend)

    assert result["messages"][-1].content == "最终回答"
    assert len(backend.commit_calls) == 1


def test_commit_node_leaves_failure_to_langgraph_node_policy() -> None:
    """Catches a domain node swallowing errors before error_handler can recover."""

    backend = ProbeBackend(fail_commit=True)
    runtime = Runtime(
        context=AssistantRunContext(),
        store=InMemoryStore(),
        server_info=_server_info(),
    )

    with pytest.raises(RuntimeError, match="third-party unavailable"):
        asyncio.run(
            memory_commit_node(
                {
                    "messages": [AIMessage(content="最终回答")],
                    "execution_mode": "fast",
                },
                runtime,
                backend=backend,
            )
        )


def test_disabled_and_degraded_recall_are_checkpoint_safe() -> None:
    disabled = create_memory_backend(ProviderConfig(provider_mode="mock"))
    runtime = Runtime(
        context=AssistantRunContext(),
        store=InMemoryStore(),
        server_info=_server_info(),
    )

    empty = asyncio.run(
        memory_recall_node(
            {"messages": [], "execution_mode": "fast"},
            runtime,
            backend=disabled,
        )
    )
    degraded = memory_recall_degraded(
        {"messages": [], "execution_mode": "fast"},
        NodeError(node="memory_recall", error=RuntimeError("secret backend response")),
    )

    assert empty == {"memory_context": (), "memory_status": "empty"}
    assert isinstance(degraded, Command)
    assert degraded.update == {"memory_context": (), "memory_status": "degraded"}
    assert degraded.goto == "execution_router"


class FakeMem0Client:
    def __init__(self) -> None:
        self.commits = []

    def recall_long_term_memory(self, _identity):
        from datetime import datetime, timezone

        return [
            Mem0RecallMemory(
                memory_id="m-1",
                text="来自 Mem0",
                created_at=datetime.now(timezone.utc),
            )
        ]

    def ingest_completed_turn(self, turn):
        self.commits.append(turn)
        return Mem0IngestionResult(accepted=True)


def test_mem0_backend_uses_run_identity_without_legacy_ledger() -> None:
    client = FakeMem0Client()
    backend = create_memory_backend(
        ProviderConfig(
            provider_mode="real",
            memory_backend="mem0",
            mem0_base_url="http://mem0.invalid",
            chat_provider="deepseek",
            chat_api_key="test-key",
            chat_base_url="https://api.deepseek.com/v1",
            chat_model="deepseek-chat",
        ),
        mem0_client=client,
    )

    _run_memory_graph(backend)

    assert client.commits[0].source_turn == "thread-1"
    assert client.commits[0].user_text == "你好"
    assert client.commits[0].assistant_text == "最终回答"


class FakeLangMemManager:
    def __init__(self) -> None:
        self.values = []

    async def ainvoke(self, value, *, config):
        self.values.append((value, config))


def test_langmem_uses_runtime_store_and_third_party_is_protocol_only() -> None:
    store = InMemoryStore()
    identity = hashlib.sha256(
        "assistant_agent:native_memory\0user-1".encode()
    ).hexdigest()[:40]
    namespace = ("assistant_agent", f"memory_subject_{identity}")
    store.put(namespace, "m-1", {"content": "来自 LangMem"})
    manager = FakeLangMemManager()
    backend = create_memory_backend(
        ProviderConfig(
            provider_mode="real",
            memory_backend="langmem",
            langmem_model="memory-model",
            chat_provider="deepseek",
            chat_api_key="test-key",
            chat_base_url="https://api.deepseek.com/v1",
            chat_model="deepseek-chat",
        ),
        langmem_manager=manager,
    )
    runtime = Runtime(
        context=AssistantRunContext(),
        store=store,
        server_info=_server_info(),
    )

    recalled = asyncio.run(
        memory_recall_node(
            {"messages": [HumanMessage(content="你好")], "execution_mode": "fast"},
            runtime,
            backend=backend,
        )
    )

    assert recalled == {
        "memory_context": ("来自 LangMem",),
        "memory_status": "ready",
    }
    assert create_memory_backend(custom_backend=ProbeBackend()).backend_id == "probe"
    assert "memory_backend" not in WorkerState.__annotations__


def test_remote_memory_fails_closed_in_mock_mode() -> None:
    try:
        create_memory_backend(
            ProviderConfig(provider_mode="mock", memory_backend="mem0")
        )
    except MemoryBackendConfigurationError as exc:
        assert "real provider mode" in str(exc)
    else:  # pragma: no cover - makes a silent fallback unmistakable.
        raise AssertionError("remote memory must not silently fall back")
