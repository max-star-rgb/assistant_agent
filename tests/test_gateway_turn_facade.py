from __future__ import annotations

import unittest

from assistant_agent.gateway import GatewaySessionManager
from assistant_agent.realtime import RealtimeAgentEvent, RealtimeAgentResult
from assistant_agent.services.gateway_turn_facade import GatewayTurnFacade, GatewayTurnRequest


class RecordingRealtimeBackend:
    def __init__(self) -> None:
        self.requests = []

    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        self.requests.append(request)
        assert event_sink is not None
        await event_sink(RealtimeAgentEvent(type="response.chunk", text="hello via gateway"))
        return RealtimeAgentResult(
            status="completed",
            run_id=request.run_id,
            trace_id="trace-turn-1",
            response_text="hello via gateway",
            expects_reply=True,
        )


class GatewayTurnFacadeTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_turn_collects_gateway_frames_and_backend_request(self) -> None:
        backend = RecordingRealtimeBackend()
        manager = GatewaySessionManager(backend_factory=lambda: backend, start_reaper=False)
        facade = GatewayTurnFacade(manager=manager)

        try:
            result = await facade.run_turn(
                GatewayTurnRequest(
                    user_id="user-1",
                    session_id="session-1",
                    text="hello",
                    metadata={"source": "http_gateway_turn"},
                    config={"tone": "concise"},
                    timeout_s=1,
                )
            )
        finally:
            await manager.close()

        assert [frame["type"] for frame in result.frames] == [
            "run.started",
            "stream.chunk",
            "run.end",
        ]
        assert result.status == "completed"
        assert result.reason == "completed"
        assert result.response_text == "hello via gateway"
        assert result.trace_id == "trace-turn-1"
        assert backend.requests[0].text == "hello"
        assert backend.requests[0].metadata["gateway"]["history"] == ["hello"]
        assert backend.requests[0].metadata["gateway"]["session_config"] == {"tone": "concise"}

    async def test_run_turn_returns_gateway_error_terminal_result(self) -> None:
        class ErrorBackend:
            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                return RealtimeAgentResult(
                    status="error",
                    run_id=request.run_id,
                    metadata={"error_message": "backend failed", "error_type": "RuntimeError"},
                )

        manager = GatewaySessionManager(backend_factory=ErrorBackend, start_reaper=False)
        facade = GatewayTurnFacade(manager=manager)

        try:
            result = await facade.run_turn(
                GatewayTurnRequest(user_id="user-1", session_id="session-err", text="fail", timeout_s=1)
            )
        finally:
            await manager.close()

        assert result.status == "error"
        assert result.reason == "error"
        assert result.terminal_frame["error"]["message"] == "backend failed"
