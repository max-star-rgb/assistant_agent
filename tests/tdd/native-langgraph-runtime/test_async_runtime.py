from __future__ import annotations

import asyncio
from typing import Any

import pytest

from assistant_agent.api import agent_service_websocket, gateway_runtime, routes_agent
from assistant_agent.gateway.runtime_pool import GatewayRuntimePool
from assistant_agent.gateway.runtime_types import RealtimeAgentRequest
from assistant_agent.gateway.turn_facade import GatewayTurnFacade, GatewayTurnRequest
from assistant_agent.runtime.assistant_graph_app import (
    GraphExecutionError,
    GraphStreamResult,
)
from assistant_agent.runtime.event_sink import ListEventSink
from assistant_agent.runtime.event_stream import AgentRunStream
from assistant_agent.runtime.events import AgentEvent
from assistant_agent.runtime.assistant_run_service import run_assistant_request_stream
from assistant_agent.runtime.assistant_runtime_app import AssistantRuntimeApp
from assistant_agent.runtime.requests import AgentResponse, UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from tests.core.support import CancelledToken, offline_config, sealed_registry


class _SyncGraphProbe:
    def __init__(self, owner: "_GraphAppProbe") -> None:
        self.owner = owner

    def invoke(self, input_state: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.owner.invoke_calls += 1
        state = input_state["state"]
        state.set_response(AgentResponse(message="sync-sentinel"))
        return {**input_state, "state": state}


class _GraphAppProbe:
    def __init__(self) -> None:
        self.invoke_calls = 0
        self.arun_calls = 0
        self.graph = _SyncGraphProbe(self)

    def invoke(self, input_state: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self.graph.invoke(input_state, **kwargs)

    async def arun(self, input_state: dict[str, Any], **kwargs: Any) -> GraphStreamResult:
        self.arun_calls += 1
        state = input_state["state"]
        state.set_response(AgentResponse(message="async-sentinel"))
        return GraphStreamResult(final_state={**input_state, "state": state}, parts=())


class _FailingSyncGraph:
    def invoke(self, input_state: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        raise GraphExecutionError("graph-sentinel", "failure-sentinel")


class _FailingGraphApp:
    graph = _FailingSyncGraph()

    def invoke(self, input_state: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self.graph.invoke(input_state, **kwargs)

    async def arun(self, input_state: dict[str, Any], **kwargs: Any) -> GraphStreamResult:
        raise GraphExecutionError("graph-sentinel", "failure-sentinel")


def _request() -> UserRequest:
    return UserRequest(
        user_id="user-sentinel",
        session_id="session-sentinel",
        text="input-sentinel",
    )


def _runtime_with_graph_probe() -> tuple[AgentGraphRuntime, _GraphAppProbe]:
    runtime = AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        session_store=InMemorySessionStore(),
    )
    probe = _GraphAppProbe()
    runtime.assistant_graph_app = probe
    return runtime, probe


def test_arun_state_uses_native_graph_async_execution() -> None:
    """Replacing native ``arun`` with sync ``invoke`` must break async execution."""

    runtime, probe = _runtime_with_graph_probe()
    try:
        state = asyncio.run(
            runtime.arun_state(_request(), run_id="async-run-sentinel")
        )

        assert state.status == "completed"
        assert state.response is not None
        assert state.response.message == "async-sentinel"
        assert probe.arun_calls == 1
        assert probe.invoke_calls == 0
    finally:
        runtime.close()


def test_run_state_keeps_synchronous_graph_compatibility() -> None:
    """Removing the sync compatibility path must break existing callers."""

    runtime, probe = _runtime_with_graph_probe()
    try:
        state = runtime.run_state(_request(), run_id="sync-run-sentinel")

        assert state.status == "completed"
        assert state.response is not None
        assert state.response.message == "sync-sentinel"
        assert probe.invoke_calls == 1
        assert probe.arun_calls == 0
    finally:
        runtime.close()


def test_sync_and_async_graph_failures_share_cleanup_without_fake_terminal() -> None:
    """Swallowing async graph errors or retaining run memory must break parity."""

    runtime, _ = _runtime_with_graph_probe()
    runtime.assistant_graph_app = _FailingGraphApp()
    sync_sink = ListEventSink()
    async_sink = ListEventSink()
    released_run_ids: list[str] = []
    release_run_context = runtime.long_term_memory_service.release_run_context

    def track_release(**kwargs: Any) -> bool:
        released_run_ids.append(kwargs["run_id"])
        return release_run_context(**kwargs)

    runtime.long_term_memory_service.release_run_context = track_release
    try:
        with pytest.raises(GraphExecutionError) as sync_error:
            runtime.run_state(
                _request(),
                event_sink=sync_sink,
                run_id="sync-failure-sentinel",
            )
        with pytest.raises(GraphExecutionError) as async_error:
            asyncio.run(
                runtime.arun_state(
                    _request(),
                    event_sink=async_sink,
                    run_id="async-failure-sentinel",
                )
            )

        assert sync_error.value.code == async_error.value.code == "graph-sentinel"
        assert [event.type for event in sync_sink.events] == [
            "task_started",
            "graph_node_started",
            "graph_node_finished",
        ]
        assert [event.type for event in async_sink.events] == [
            "task_started",
            "graph_node_started",
            "graph_node_finished",
        ]
        assert released_run_ids == [
            "sync-failure-sentinel",
            "async-failure-sentinel",
        ]
    finally:
        runtime.long_term_memory_service.release_run_context = release_run_context
        runtime.close()


def test_arun_state_preserves_pre_graph_cancellation_terminal_events() -> None:
    """Executing the graph or emitting final output after cancellation must fail."""

    runtime, probe = _runtime_with_graph_probe()
    sink = ListEventSink()
    try:
        state = asyncio.run(
            runtime.arun_state(
                _request(),
                event_sink=sink,
                cancel_token=CancelledToken(),
                run_id="cancelled-run-sentinel",
            )
        )

        assert state.status == "cancelled"
        assert probe.arun_calls == 0
        assert probe.invoke_calls == 0
        assert [event.type for event in sink.events] == [
            "task_started",
            "task_cancelled",
        ]
    finally:
        runtime.close()


def test_service_stream_uses_native_async_runtime_without_thread_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoring the service-level ``to_thread`` bridge must break this run."""

    async def forbidden_to_thread(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("native graph stream must not use asyncio.to_thread")

    monkeypatch.setattr(asyncio, "to_thread", forbidden_to_thread)

    async def exercise() -> None:
        runtime, probe = _runtime_with_graph_probe()
        try:
            stream = run_assistant_request_stream(
                _request(),
                runtime=runtime,
                enable_conversation_history=False,
                run_id="service-async-run-sentinel",
            )

            events = [event async for event in stream]
            artifacts = await stream.result()

            assert artifacts.state.status == "completed"
            assert artifacts.state.response is not None
            assert artifacts.state.response.message == "async-sentinel"
            assert probe.arun_calls == 1
            assert probe.invoke_calls == 0
            assert [event.type for event in events] == [
                "task_started",
                "graph_node_started",
                "graph_node_finished",
                "response_delta",
                "final_response",
            ]
            assert all(isinstance(event, AgentEvent) for event in events)
        finally:
            runtime.close()

    asyncio.run(exercise())


def test_agent_run_stream_enqueues_directly_on_its_owner_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheduling same-loop events through the thread bridge must break delivery."""

    async def exercise() -> None:
        loop = asyncio.get_running_loop()
        stream: AgentRunStream[str] = AgentRunStream(loop=loop)

        def forbidden_threadsafe(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("same-loop publication must enqueue directly")

        monkeypatch.setattr(loop, "call_soon_threadsafe", forbidden_threadsafe)
        event = AgentEvent(type="task_started", session_id="session-sentinel")

        stream.emit(event)
        stream.set_result("result-sentinel")

        assert [item async for item in stream] == [event]
        assert await stream.result() == "result-sentinel"

    asyncio.run(exercise())


def test_default_gateway_factory_uses_pool_native_async_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injecting the pool's sync request hook must break the production factory."""

    async def forbidden_to_thread(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("default Gateway graph run must remain async-native")

    async def exercise() -> None:
        runtime, probe = _runtime_with_graph_probe()
        pool = GatewayRuntimePool(
            max_runtime_instances=1,
            runtime_factory=lambda: runtime,
            run_request_stream=(
                gateway_runtime._run_assistant_request_with_http_runtime_stream
            ),
            runtime_cleanup=lambda item: item.close(),
        )
        backend = gateway_runtime._default_gateway_backend_factory(pool)()
        capture_id = gateway_runtime.new_gateway_http_response_capture_id()
        monkeypatch.setattr(asyncio, "to_thread", forbidden_to_thread)
        try:
            result = await backend.run_turn(
                RealtimeAgentRequest(
                    user_id="user-sentinel",
                    session_id="session-sentinel",
                    run_id="gateway-native-run-sentinel",
                    text="input-sentinel",
                    metadata=gateway_runtime.gateway_http_capture_metadata(capture_id),
                )
            )

            assert result.status == "completed"
            assert result.response_text == "async-sentinel"
            assert probe.arun_calls == 1
            assert probe.invoke_calls == 0
            assert pool.idle_count == 1
            captured = gateway_runtime.pop_gateway_http_response(capture_id)
            assert captured is not None
            assert captured.response_text == "async-sentinel"
        finally:
            gateway_runtime.pop_gateway_http_response(capture_id)
            pool.close()

    asyncio.run(exercise())


def test_agent_service_manager_uses_runtime_app_native_async_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Injecting the Agent-Service sync wrapper must break its default manager."""

    async def forbidden_to_thread(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Agent-Service graph run must remain async-native")

    async def noop_session_lifecycle(*args: Any, **kwargs: Any) -> None:
        return None

    async def exercise() -> None:
        runtime, probe = _runtime_with_graph_probe()
        app = AssistantRuntimeApp(lambda: runtime)
        monkeypatch.setattr(routes_agent, "get_assistant_runtime_app", lambda: app)
        monkeypatch.setattr(
            agent_service_websocket,
            "_initialize_agent_service_session_memory",
            noop_session_lifecycle,
        )
        monkeypatch.setattr(
            agent_service_websocket,
            "_finalize_agent_service_session_memory",
            noop_session_lifecycle,
        )
        manager = agent_service_websocket._create_agent_service_gateway_manager()
        facade = GatewayTurnFacade(manager=manager)
        monkeypatch.setattr(asyncio, "to_thread", forbidden_to_thread)
        try:
            result = await facade.run_turn(
                GatewayTurnRequest(
                    user_id="user-sentinel",
                    session_id="session-sentinel",
                    text="input-sentinel",
                )
            )

            assert result.status == "completed"
            assert result.response_text == "async-sentinel"
            assert probe.arun_calls == 1
            assert probe.invoke_calls == 0
        finally:
            await facade.close()
            await manager.close()
            runtime.close()

    asyncio.run(exercise())


def test_gateway_pool_returns_runtime_after_async_failure_and_cancellation() -> None:
    """Returning or leaking a runtime before terminal failure/cancel must break reuse."""

    async def exercise_failure() -> None:
        runtime, _ = _runtime_with_graph_probe()
        runtime.assistant_graph_app = _FailingGraphApp()
        pool = GatewayRuntimePool(
            max_runtime_instances=1,
            runtime_factory=lambda: runtime,
            runtime_cleanup=lambda item: item.close(),
        )
        try:
            stream = pool.run_request_stream(
                _request(),
                enable_conversation_history=False,
                run_id="pool-failure-run-sentinel",
            )
            with pytest.raises(GraphExecutionError):
                _ = [event async for event in stream]
            assert pool.idle_count == 1
        finally:
            pool.close()

    async def exercise_cancellation() -> None:
        runtime, probe = _runtime_with_graph_probe()
        pool = GatewayRuntimePool(
            max_runtime_instances=1,
            runtime_factory=lambda: runtime,
            runtime_cleanup=lambda item: item.close(),
        )
        try:
            stream = pool.run_request_stream(
                _request(),
                enable_conversation_history=False,
                cancel_token=CancelledToken(),
                run_id="pool-cancelled-run-sentinel",
            )
            events = [event async for event in stream]
            artifacts = await stream.result()
            assert artifacts.state.status == "cancelled"
            assert [event.type for event in events] == [
                "task_started",
                "task_cancelled",
            ]
            assert probe.arun_calls == 0
            assert pool.idle_count == 1
        finally:
            pool.close()

    asyncio.run(exercise_failure())
    asyncio.run(exercise_cancellation())
