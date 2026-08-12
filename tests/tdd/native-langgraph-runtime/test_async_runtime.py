from __future__ import annotations

import asyncio
from typing import Any

import pytest

from assistant_agent.runtime.assistant_graph_app import (
    GraphExecutionError,
    GraphStreamResult,
)
from assistant_agent.runtime.event_sink import ListEventSink
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
