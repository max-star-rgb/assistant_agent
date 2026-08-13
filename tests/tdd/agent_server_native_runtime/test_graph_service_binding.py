from __future__ import annotations

from langgraph.runtime import Runtime

from assistant_agent.agent_server.context import AgentServerRunContext
from assistant_agent.runtime.graph_runtime import (
    GraphExecutionServices,
    GraphRuntimeContext,
    bind_runtime_node,
)
from assistant_agent.runtime.assistant_loop_graph import build_assistant_loop_graph


def test_serializable_server_context_resolves_run_owned_services() -> None:
    executor = object()
    adapter = object()
    claims = object()
    services = GraphExecutionServices(
        tool_executor=executor,
        chat_adapter=adapter,
        invocation_claim_store=claims,
    )
    worker_context = GraphRuntimeContext(services=services)
    server_context = AgentServerRunContext(
        user_id="user-sentinel",
        tenant_id="tenant-sentinel",
    )

    def probe_node(state: dict[str, object]) -> dict[str, object]:
        return {
            **state,
            "executor_seen": state["tool_executor"],
            "adapter_seen": state["chat_adapter"],
        }

    wrapped = bind_runtime_node(
        "probe",
        probe_node,
        trace=False,
        runtime_context_resolver=lambda _state, _runtime: worker_context,
    )

    result = wrapped({}, Runtime(context=server_context))

    assert result == {"executor_seen": executor, "adapter_seen": adapter}
    assert server_context.model_dump(mode="json") == {
        "user_id": "user-sentinel",
        "tenant_id": "tenant-sentinel",
        "assistant_mode": "standard",
        "entry_profile": "agent_server",
        "media_capabilities": [],
    }


def test_worker_service_owner_is_not_shared_between_runs() -> None:
    first = GraphExecutionServices(
        tool_executor=object(),
        chat_adapter=object(),
        invocation_claim_store=object(),
    )
    second = GraphExecutionServices(
        tool_executor=object(),
        chat_adapter=object(),
        invocation_claim_store=object(),
    )

    assert first.tool_executor is not second.tool_executor
    assert first.chat_adapter is not second.chat_adapter
    assert first.invocation_claim_store is not second.invocation_claim_store


def test_assistant_graph_declares_public_context_and_run_service_resolver() -> None:
    server_context = AgentServerRunContext(
        user_id="user-sentinel",
        tenant_id="tenant-sentinel",
    )
    worker_context = GraphRuntimeContext(
        services=GraphExecutionServices(
            tool_executor=object(),
            chat_adapter=object(),
            invocation_claim_store=object(),
        )
    )
    resolutions: list[AgentServerRunContext] = []

    def resolve(_state, runtime):
        resolutions.append(runtime.context)
        return worker_context

    graph = build_assistant_loop_graph(
        context_schema=AgentServerRunContext,
        runtime_context_resolver=resolve,
        graph_name="ServerContextProbe",
    )

    schema = graph.get_context_jsonschema()

    assert schema["title"] == "AgentServerRunContext"
    assert set(schema["required"]) == {"user_id", "tenant_id"}
    assert resolutions == []
