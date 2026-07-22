"""Agent-Service WebSocket interrupt contract."""

from __future__ import annotations

import asyncio
import json
from threading import Event

from fastapi.testclient import TestClient

from assistant_agent.api import agent_service_websocket as agent_service_module
from assistant_agent.api.app import create_app
from assistant_agent.gateway import GatewaySessionManager
from assistant_agent.realtime import RealtimeAgentEvent, RealtimeAgentResult


class _InterruptibleBackend:
    def __init__(self) -> None:
        self.started = Event()
        self.cancelled = Event()
        self.calls = 0

    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            while not cancel_token.is_cancelled():
                await asyncio.sleep(0.001)
            self.cancelled.set()
            return RealtimeAgentResult(
                status="cancelled",
                run_id=request.run_id,
                response_text='{"response_type":"answer","answer":"不应发送的旧回复"}',
            )

        assert event_sink is not None
        await event_sink(RealtimeAgentEvent(type="response.chunk", text="新回复"))
        return RealtimeAgentResult(
            status="completed",
            run_id=request.run_id,
            response_text='{"response_type":"answer","answer":"新回复"}',
        )


def test_agent_service_is_the_only_media_websocket_route() -> None:
    route_paths = {route.path for route in create_app().routes}

    assert "/agent-service/{version}" in route_paths
    assert "/ws/gateway" in route_paths
    assert "/ws/realtime/media" not in route_paths


def test_interrupt_cancels_active_turn_suppresses_old_output_and_keeps_connection(monkeypatch) -> None:
    backend = _InterruptibleBackend()
    manager = GatewaySessionManager(backend_factory=lambda: backend, start_reaper=False)
    monkeypatch.setattr(
        agent_service_module,
        "_create_agent_service_gateway_manager",
        lambda: manager,
    )

    with TestClient(create_app()).websocket_connect("/agent-service/v1") as websocket:
        websocket.send_json(
            _envelope(
                "assistantControl",
                {"number": "10086", "callType": "VIDEO"},
            )
        )
        assert _body(websocket.receive_json())["code"] == 0

        websocket.send_json(_chat("chat-1", "请执行一个长任务"))
        assert backend.started.wait(timeout=2.0)

        websocket.send_json(_envelope("interrupt", {"number": "10086"}))
        interrupt_ack = websocket.receive_json()

        assert interrupt_ack["message"] == "interrupt"
        assert _body(interrupt_ack) == {"code": 0, "message": "interrupted"}
        assert backend.cancelled.wait(timeout=2.0)

        websocket.send_json(_chat("chat-2", "继续新任务"))
        next_response = websocket.receive_json()

        assert next_response["message"] == "chatResponse"
        assert _body(next_response)["message"]["chatIndex"] == "chat-2"
        assert _body(next_response)["message"]["content"]["intentResult"]["description"] == "新回复"

        websocket.send_json(_envelope("interrupt", {"number": "10086"}))
        idle_interrupt_ack = websocket.receive_json()
        assert idle_interrupt_ack["message"] == "interrupt"
        assert _body(idle_interrupt_ack) == {"code": 0, "message": "interrupted"}


def _chat(chat_index: str, text: str) -> dict:
    return _envelope(
        "chat",
        {
            "chatIndex": chat_index,
            "userNumber": "10086",
            "contents": [
                {
                    "speakerNumber": "10086",
                    "speechContent": text,
                    "time": "2026-07-21T12:00:00+08:00",
                }
            ],
            "stream": False,
        },
    )


def _envelope(message: str, body: dict) -> dict:
    return {"message": message, "body": json.dumps(body, ensure_ascii=False)}


def _body(envelope: dict) -> dict:
    return json.loads(envelope["body"])
