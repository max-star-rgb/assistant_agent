from __future__ import annotations

from dataclasses import dataclass
import asyncio
from types import SimpleNamespace

from langgraph.runtime import ExecutionInfo, Runtime
from langgraph.store.memory import InMemoryStore

from assistant_agent.agent_server.context import AgentServerRunContext
from assistant_agent.agent_server.graph import (
    AgentServerGraphInput,
    assistant_graph,
    build_agent_server_assistant_graph,
)
from assistant_agent.runtime.assistant_graph_state import (
    assistant_turn_state_from_request,
)
from assistant_agent.runtime.requests import UserRequest


@dataclass
class _BootstrapProbe:
    runtime_context: object

    def bootstrap(self, value, runtime):
        assert value == AgentServerGraphInput(
            turn_origin_id="turn-sentinel",
            text="request-sentinel",
        )
        assert runtime.context.user_id == "user-sentinel"
        assert runtime.execution_info is not None
        state = assistant_turn_state_from_request(
            UserRequest(
                user_id=runtime.context.user_id,
                session_id=runtime.execution_info.thread_id or "",
                text=value.text,
            ),
            run_id=runtime.execution_info.run_id or "",
            trace_id=runtime.execution_info.run_id or "",
        )
        state["turn_origin_id"] = value.turn_origin_id
        state["memory_origin_run_id"] = value.turn_origin_id
        return state

    def resolve(self, _parent, _child, runtime):
        assert runtime.execution_info is not None
        return self.runtime_context


def test_server_graph_bootstrap_binds_native_thread_and_run_identity() -> None:
    probe = _BootstrapProbe(runtime_context=object())
    graph = build_agent_server_assistant_graph(
        worker=probe,
        stop_after_bootstrap=True,
    )
    runtime = Runtime(
        context=AgentServerRunContext(
            user_id="user-sentinel",
            tenant_id="tenant-sentinel",
        ),
        execution_info=ExecutionInfo(
            checkpoint_id="checkpoint-sentinel",
            checkpoint_ns="",
            task_id="task-sentinel",
            thread_id="thread-native-sentinel",
            run_id="run-native-sentinel",
        ),
    )

    result = graph.nodes["bootstrap"].bound.func(
        {"request_input": {"turn_origin_id": "turn-sentinel", "text": "request-sentinel"}},
        runtime,
    )

    assistant_state = result["assistant_state"]
    assert assistant_state["request"]["session_id"] == "thread-native-sentinel"
    assert assistant_state["run"]["run_id"] == "run-native-sentinel"
    assert assistant_state["invocation_run_id"] == "run-native-sentinel"
    assert assistant_state["turn_origin_id"] == "turn-sentinel"


def test_server_graph_schema_accepts_product_input_not_checkpoint_internals() -> None:
    graph = build_agent_server_assistant_graph(
        worker=_BootstrapProbe(runtime_context=object()),
        stop_after_bootstrap=True,
    )

    schema = graph.get_input_jsonschema()

    request_schema = schema["$defs"]["AgentServerGraphInput"]
    assert set(request_schema["required"]) == {"turn_origin_id", "text"}
    assert "assistant_state" not in request_schema["properties"]
    assert graph.checkpointer is None
    assert graph.store is None


def test_factory_opens_execution_owner_only_for_native_runs(monkeypatch) -> None:
    opened: list[object] = []
    closed: list[object] = []

    class _Owner:
        memory_bundle = None
        worker = _BootstrapProbe(runtime_context=object())

        async def aclose(self):
            closed.append(self)

    async def open_owner(*, context, store, user):
        assert context.user_id == "user-sentinel"
        assert isinstance(store, InMemoryStore)
        assert user.identity == "service-sentinel"
        owner = _Owner()
        opened.append(owner)
        return owner

    monkeypatch.setattr(
        "assistant_agent.agent_server.graph.AgentServerExecutionOwner.open",
        open_owner,
    )
    store = InMemoryStore()
    user = SimpleNamespace(identity="service-sentinel", permissions=["assistant:invoke"])
    read_runtime = SimpleNamespace(
        access_context="assistants.read",
        execution_runtime=None,
        store=store,
        user=user,
    )
    execution_runtime = SimpleNamespace(
        access_context="threads.create_run",
        store=store,
        user=user,
        context=AgentServerRunContext(
            user_id="user-sentinel",
            tenant_id="tenant-sentinel",
        ),
    )
    execution_runtime.execution_runtime = execution_runtime
    execution_runtime.ensure_user = lambda: user

    async def exercise():
        async with assistant_graph(read_runtime) as read_graph:
            read_nodes = set(read_graph.get_graph().nodes)
        assert opened == []

        async with assistant_graph(execution_runtime) as run_graph:
            run_nodes = set(run_graph.get_graph().nodes)
            assert len(opened) == 1
            assert closed == []

        assert read_nodes == run_nodes
        assert len(closed) == 1
        assert run_graph.checkpointer is None
        assert run_graph.store is None

    asyncio.run(exercise())
