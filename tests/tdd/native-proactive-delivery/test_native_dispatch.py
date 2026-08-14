from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import ExecutionInfo, Runtime
from langgraph.store.memory import InMemoryStore
import pytest

from assistant_agent.agent_server import services
from assistant_agent.agent_server.services import AgentServerExecutionOwner
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.root_graph import (
    ProactiveDeliveryUnavailableError,
    build_assistant_root_graph,
    delivery_dispatch_node,
    route_after_execution,
)
from assistant_agent.native_agent.state import FastAgentState, PlanningState
from assistant_agent.proactive_delivery import (
    ProactiveDeliveryIntent,
    ProactiveDispatchState,
    SQLiteProactiveDeliveryStore,
)


class _Memory:
    backend_id = "disabled"

    async def recall(self, **_kwargs: Any) -> tuple[str, ...]:
        return ()

    async def commit(self, **_kwargs: Any) -> None:
        return None


def _branch(schema, name: str):
    def answer(_state):
        return {"messages": [AIMessage(content="answer-sentinel")]}

    builder = StateGraph(schema, context_schema=AssistantRunContext)
    builder.add_node("answer", answer)
    builder.add_edge(START, "answer")
    builder.add_edge("answer", END)
    return builder.compile(name=name)


def _graph(store=None):
    return build_assistant_root_graph(
        memory_backend=_Memory(),
        fast_agent=_branch(FastAgentState, "AssistantFastAgent"),
        planning_graph=_branch(PlanningState, "AssistantPlanningGraph"),
        proactive_delivery_store=store,
    )


def _intent(
    message_id: str = "message-1",
    *,
    mode: str = "durable",
) -> ProactiveDeliveryIntent:
    return ProactiveDeliveryIntent(
        message_id=message_id,
        kind="system.notice",
        content="content-sentinel",
        delivery_mode=mode,
    )


def _state(*, pending=()):
    return {
        "messages": [HumanMessage(content="request-sentinel")],
        "execution_mode": "fast",
        "pending_deliveries": tuple(pending),
    }


def _runtime() -> Runtime[AssistantRunContext]:
    return Runtime(
        context=AssistantRunContext(
            user_id="user-sentinel",
            tenant_id="tenant-sentinel",
        ),
        execution_info=ExecutionInfo(
            checkpoint_id="checkpoint-sentinel",
            checkpoint_ns="",
            task_id="task-sentinel",
            thread_id="thread-sentinel",
            run_id="run-sentinel",
        ),
    )


def _edges(graph) -> set[tuple[str, str]]:
    return {(edge.source, edge.target) for edge in graph.edges}


def test_parent_graph_predeclares_native_dispatch_after_both_modes() -> None:
    graph = _graph().get_graph()

    assert "delivery_dispatch" in graph.nodes
    assert ("delivery_dispatch", "memory_commit") in _edges(graph)
    assert route_after_execution(_state()) == "memory_commit"
    assert route_after_execution(_state(pending=[_intent()])) == "delivery_dispatch"


def test_dispatch_uses_native_runtime_identity_and_clears_intents(tmp_path) -> None:
    store = SQLiteProactiveDeliveryStore(tmp_path / "delivery.sqlite3")

    result = delivery_dispatch_node(
        _state(pending=[_intent()]),
        _runtime(),
        store=store,
    )

    assert result["pending_deliveries"] == ()
    dispatch = result["delivery_dispatch"]
    assert isinstance(dispatch, ProactiveDispatchState)
    assert dispatch.status == "queued"
    record = store.get("message-1")
    assert record.message.user_id == "user-sentinel"
    assert record.message.session_id == "thread-sentinel"
    assert record.message.source_run_id == "run-sentinel"
    assert record.message.source_trace_id == "run-sentinel"


def test_native_retry_is_idempotent_by_stable_message_identity(tmp_path) -> None:
    store = SQLiteProactiveDeliveryStore(tmp_path / "delivery.sqlite3")
    state = _state(pending=[_intent()])

    first = delivery_dispatch_node(state, _runtime(), store=store)
    second = delivery_dispatch_node(state, _runtime(), store=store)

    assert first == second
    assert store.get("message-1").status == "queued"


def test_dispatch_reports_ephemeral_offline_skip(tmp_path) -> None:
    store = SQLiteProactiveDeliveryStore(tmp_path / "delivery.sqlite3")

    result = delivery_dispatch_node(
        _state(pending=[_intent(mode="connection_ephemeral")]),
        _runtime(),
        store=store,
    )

    dispatch = result["delivery_dispatch"]
    assert isinstance(dispatch, ProactiveDispatchState)
    assert dispatch.status == "skipped"
    assert dispatch.issue_code == "connection_offline"
    assert store.get("message-1").status == "skipped_offline"


def test_dispatch_fails_closed_without_store_or_native_identity() -> None:
    state = _state(pending=[_intent()])

    with pytest.raises(ProactiveDeliveryUnavailableError):
        delivery_dispatch_node(state, _runtime(), store=None)
    with pytest.raises(ProactiveDeliveryUnavailableError):
        delivery_dispatch_node(
            state,
            Runtime(
                context=AssistantRunContext(
                    user_id="user-sentinel",
                    tenant_id="tenant-sentinel",
                )
            ),
            store=object(),
        )


def test_checkpoint_contains_only_delivery_data(tmp_path) -> None:
    state = _state(pending=[_intent()])
    serialized = json.dumps(
        {
            **state,
            "messages": ["request-sentinel"],
            "pending_deliveries": [
                item.model_dump(mode="json") for item in state["pending_deliveries"]
            ],
        },
        ensure_ascii=False,
    )

    assert "message-1" in serialized
    assert "SQLiteProactiveDeliveryStore" not in serialized
    assert str(tmp_path) not in serialized


def test_agent_server_composes_store_into_native_parent_graph(monkeypatch, tmp_path) -> None:
    path = tmp_path / "configured.sqlite3"
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")
    monkeypatch.setenv("PROACTIVE_DELIVERY_STORE_PATH", str(path))
    monkeypatch.delenv("MULTIMODAL_AGENT_MCP_ENABLED", raising=False)

    owner = asyncio.run(AgentServerExecutionOwner.compose(store=InMemoryStore()))
    try:
        assert owner.proactive_delivery_store.path == path
        assert "delivery_dispatch" in owner.graph.get_graph().nodes
        assert "AgentServerGraphWorker" not in inspect.getsource(services)
    finally:
        asyncio.run(owner.aclose())


def test_production_dispatch_has_no_legacy_runtime_import() -> None:
    from assistant_agent.agent_server import media_app, proactive_delivery
    from assistant_agent.native_agent import root_graph

    source = "\n".join(
        inspect.getsource(module)
        for module in (services, media_app, proactive_delivery, root_graph)
    )

    assert "assistant_agent.runtime" not in source
