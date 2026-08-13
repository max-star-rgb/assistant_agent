from __future__ import annotations

import asyncio

import pytest

from assistant_agent.gateway.runtime_adapter import GatewayRuntimeAdapter
from assistant_agent.gateway.runtime_types import RealtimeAgentRequest
from assistant_agent.runtime.assistant_graph_app import GraphExecutionError
from assistant_agent.runtime.assistant_run_service import (
    AssistantRunArtifacts,
    run_assistant_request_async,
)
from assistant_agent.runtime.event_stream import AgentRunStream
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.runtime.state import AgentState
from tests.core.support import offline_config, sealed_registry


def _request() -> UserRequest:
    return UserRequest(
        user_id="user-gateway-compat",
        session_id="session-gateway-compat",
        text="input-sentinel",
    )


def _runtime(*, allow_interrupt: bool = False) -> AgentGraphRuntime:
    return AgentGraphRuntime(
        registry=sealed_registry(),
        config=offline_config(),
        session_store=InMemorySessionStore(),
        allow_interrupt=allow_interrupt,
    )


def test_service_rejects_interrupt_enabled_runtime_composition() -> None:
    """Letting a product service consume an interrupt-enabled Runtime must fail."""

    runtime = _runtime(allow_interrupt=True)
    try:
        with pytest.raises(GraphExecutionError) as captured:
            asyncio.run(
                run_assistant_request_async(
                    _request(),
                    runtime=runtime,
                    enable_conversation_history=False,
                    run_id="run-service-interrupt-enabled",
                )
            )
        assert captured.value.code == "service_interrupt_runtime_forbidden"
    finally:
        runtime.close()


def test_service_fails_closed_if_runtime_unexpectedly_returns_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recording a waiting turn as product success or history must fail."""

    runtime = _runtime()

    async def return_waiting(request: UserRequest, **kwargs) -> AgentState:
        state = AgentState.from_request(
            request,
            run_id=kwargs.get("run_id") or "run-unexpected-waiting",
        )
        state.status = "waiting_user"
        return state

    monkeypatch.setattr(runtime, "arun_state", return_waiting)
    try:
        with pytest.raises(GraphExecutionError) as captured:
            asyncio.run(
                run_assistant_request_async(
                    _request(),
                    runtime=runtime,
                    enable_conversation_history=False,
                    run_id="run-unexpected-waiting",
                )
            )
        assert captured.value.code == "service_graph_waiting_state"
    finally:
        runtime.close()


def test_gateway_projects_unexpected_waiting_as_existing_error_only() -> None:
    """Adding waiting/resume realtime events or completing a waiting state must fail."""

    runtime = _runtime()
    waiting = AgentState.from_request(_request(), run_id="run-gateway-waiting")
    waiting.status = "waiting_user"

    def waiting_stream(request: UserRequest, **kwargs) -> AgentRunStream[AssistantRunArtifacts]:
        loop = asyncio.get_running_loop()
        stream: AgentRunStream[AssistantRunArtifacts] = AgentRunStream(loop=loop)
        stream.set_result(
            AssistantRunArtifacts(runtime=runtime, state=waiting, events=[])
        )
        return stream

    async def exercise() -> None:
        emitted = []

        async def collect(event) -> None:
            emitted.append(event)

        backend = GatewayRuntimeAdapter(
            run_request_stream=waiting_stream,
            load_env=False,
            enable_conversation_history=False,
        )
        result = await backend.run_turn(
            RealtimeAgentRequest(
                user_id="user-gateway-compat",
                session_id="session-gateway-compat",
                run_id="run-gateway-waiting",
                text="input-sentinel",
            ),
            event_sink=collect,
        )

        assert result.status == "error"
        assert result.response_text == ""
        assert [event.type for event in emitted] == ["error"]
        assert all(event.type not in {"waiting", "waiting_input", "resume"} for event in emitted)
        assert result.metadata["error_type"] == "GraphExecutionError"

    try:
        asyncio.run(exercise())
    finally:
        runtime.close()
