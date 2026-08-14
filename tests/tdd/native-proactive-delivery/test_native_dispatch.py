from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TypedDict

import pytest
from langgraph.runtime import ExecutionInfo, Runtime

from assistant_agent.agent_server.context import AgentServerRunContext
from assistant_agent.agent_server.graph import AgentServerGraphInput
from assistant_agent.agent_server.services import AgentServerGraphWorker
from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.assistant_graph_state import (
    AssistantTurnState,
    assistant_turn_state_from_request,
)
from assistant_agent.runtime.assistant_loop_graph import (
    build_assistant_loop_graph,
    build_namespaced_assistant_loop_graph,
    route_after_publish_response,
)
from assistant_agent.runtime.assistant_loop_nodes import (
    ProactiveDeliveryUnavailableError,
    delivery_dispatch_node,
)
from assistant_agent.runtime.graph_runtime import (
    GraphExecutionServices,
    GraphRuntimeContext,
)
from assistant_agent.runtime.proactive_delivery import (
    ProactiveDeliveryIntent,
    SQLiteProactiveDeliveryStore,
)
from assistant_agent.runtime.requests import UserRequest


def _intent(
    message_id: str = "message-1",
    *,
    mode: str = "durable",
) -> dict[str, object]:
    return ProactiveDeliveryIntent(
        message_id=message_id,
        kind="system.notice",
        content="content-sentinel",
        delivery_mode=mode,
    ).model_dump(mode="json")


def _state(*, pending: list[dict[str, object]] | None = None) -> AssistantTurnState:
    state = assistant_turn_state_from_request(
        UserRequest(
            user_id="user-sentinel",
            session_id="thread-sentinel",
            text="request-sentinel",
        ),
        run_id="run-sentinel",
        trace_id="trace-sentinel",
        agent_id="agent-sentinel",
    )
    state["pending_deliveries"] = pending or []
    return state


def _runtime(
    store=None,
    *,
    invocation_kind: str = "invoke",
) -> Runtime[GraphRuntimeContext]:
    return Runtime(
        context=GraphRuntimeContext(
            services=GraphExecutionServices(
                tool_executor=object(),
                chat_adapter=object(),
                invocation_claim_store=object(),
                proactive_delivery_store=store,
            ),
            invocation_kind=invocation_kind,
        )
    )


def _edges(graph) -> set[tuple[str, str]]:
    return {(edge.source, edge.target) for edge in graph.edges}


class ParentState(TypedDict):
    assistant_state: AssistantTurnState


def test_native_graph_routes_publish_to_dispatch_only_when_pending() -> None:
    graph = build_assistant_loop_graph().get_graph()

    assert "delivery_dispatch" in graph.nodes
    assert ("delivery_dispatch", "memory_commit") in _edges(graph)
    assert "time_travel_anchor" not in graph.nodes
    assert route_after_publish_response(_state()) == "memory_commit"
    assert route_after_publish_response(_state(pending=[_intent()])) == (
        "delivery_dispatch"
    )


def test_namespaced_graph_predeclares_same_native_dispatch() -> None:
    graph = build_namespaced_assistant_loop_graph(
        state_schema=ParentState,
        context_schema=GraphRuntimeContext,
        child_state_key="assistant_state",
        runtime_context_resolver=lambda _parent, _child, runtime: runtime.context,
        profile="standard",
        graph_name="NestedDeliveryTest",
    ).get_graph()

    assert "delivery_dispatch" in graph.nodes
    assert ("delivery_dispatch", "memory_commit") in _edges(graph)


def test_dispatch_enqueues_trusted_thread_identity_without_control_state(
    tmp_path,
) -> None:
    store = SQLiteProactiveDeliveryStore(tmp_path / "delivery.sqlite3")
    state = _state(pending=[_intent()])
    original_continuation = state["continuation"]

    result = delivery_dispatch_node(state, _runtime(store))

    assert not result["pending_deliveries"]
    assert result["delivery_dispatch"]["status"] == "queued"
    assert result["continuation"] == original_continuation
    record = store.get("message-1")
    assert record.message.user_id == "user-sentinel"
    assert record.message.session_id == "thread-sentinel"
    assert record.message.source_run_id == "run-sentinel"
    assert record.message.source_trace_id == "trace-sentinel"


def test_dispatch_reports_ephemeral_offline_skip(tmp_path) -> None:
    store = SQLiteProactiveDeliveryStore(tmp_path / "delivery.sqlite3")

    result = delivery_dispatch_node(
        _state(pending=[_intent(mode="connection_ephemeral")]),
        _runtime(store),
    )

    assert result["delivery_dispatch"]["status"] == "skipped"
    assert result["delivery_dispatch"]["issue_code"] == "connection_offline"
    assert store.get("message-1").status == "skipped_offline"


@pytest.mark.parametrize("invocation_kind", ["replay", "fork"])
def test_time_travel_clears_pending_without_store_write(
    invocation_kind,
    tmp_path,
) -> None:
    store = SQLiteProactiveDeliveryStore(tmp_path / "delivery.sqlite3")
    result = delivery_dispatch_node(
        _state(pending=[_intent()]),
        _runtime(store, invocation_kind=invocation_kind),
    )

    assert not result["pending_deliveries"]
    assert result["delivery_dispatch"]["status"] == "skipped"
    with pytest.raises(KeyError):
        store.get("message-1")


def test_dispatch_fails_closed_without_store() -> None:
    state = _state(pending=[_intent()])

    with pytest.raises(ProactiveDeliveryUnavailableError):
        delivery_dispatch_node(state, _runtime())

    assert state["pending_deliveries"] == [_intent()]


def test_checkpoint_contains_only_delivery_data(tmp_path) -> None:
    state = _state(pending=[_intent()])
    serialized = json.dumps(state, ensure_ascii=False)

    assert "message-1" in serialized
    assert "SQLiteProactiveDeliveryStore" not in serialized
    assert str(tmp_path) not in serialized


def test_config_and_agent_server_worker_bind_composition_store(tmp_path) -> None:
    path = tmp_path / "configured.sqlite3"
    config = ProviderConfig.from_env(
        {
            "PROACTIVE_DELIVERY_STORE_PATH": str(path),
            "PROACTIVE_DELIVERY_ACK_TIMEOUT_SECONDS": "3",
            "PROACTIVE_DELIVERY_LEASE_SECONDS": "7",
            "PROACTIVE_DELIVERY_PRESENCE_TTL_SECONDS": "11",
            "PROACTIVE_DELIVERY_POLL_INTERVAL_SECONDS": "0.1",
        }
    )
    assert config.proactive_delivery_store_path == str(path)
    assert config.proactive_delivery_ack_timeout_seconds == 3.0
    assert config.proactive_delivery_lease_seconds == 7.0
    assert config.proactive_delivery_presence_ttl_seconds == 11.0
    assert config.proactive_delivery_poll_interval_seconds == 0.1

    store = SQLiteProactiveDeliveryStore(path)
    worker = AgentServerGraphWorker(
        context=AgentServerRunContext(
            user_id="user-sentinel",
            tenant_id="tenant-sentinel",
        ),
        config=config,
        tool_executor=SimpleNamespace(
            registry=SimpleNamespace(
                list_specs=lambda: [],
                generation="generation-sentinel",
            )
        ),
        chat_adapter=object(),
        trace_store=object(),
        invocation_claim_store=object(),
        proactive_delivery_store=store,
    )
    runtime = Runtime(
        context=worker.context,
        execution_info=ExecutionInfo(
            checkpoint_id="checkpoint-sentinel",
            checkpoint_ns="",
            task_id="task-sentinel",
            thread_id="thread-sentinel",
            run_id="run-sentinel",
        ),
    )
    state = worker.bootstrap(
        AgentServerGraphInput(
            turn_origin_id="turn-sentinel",
            text="request-sentinel",
        ),
        runtime,
    )
    resolved = worker.resolve({}, state, runtime)

    assert resolved.proactive_delivery_store is store
    assert "proactive_delivery_store" not in runtime.context.model_dump(mode="json")
