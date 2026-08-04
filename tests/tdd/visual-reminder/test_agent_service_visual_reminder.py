from __future__ import annotations

import asyncio
import json

from assistant_agent.api import agent_service_websocket as agent_service
from assistant_agent.config import ProviderConfig
from assistant_agent.media.video.visual_reminder import VisualReminderReservation
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.tools.registry import ToolRegistry


class _GatewayManager:
    def __init__(self) -> None:
        self.initialized = []
        self.closed = False

    async def initialize_session(self, **kwargs) -> None:
        self.initialized.append(kwargs)

    async def close(self) -> None:
        self.closed = True


class _RenderingRegistry:
    async def unregister(self, **_kwargs) -> bool:
        return True


def _state(gateway: _GatewayManager) -> agent_service.AgentServiceConnectionState:
    return agent_service.AgentServiceConnectionState(
        session_id="vendor-session",
        query_params={},
        runtime_session_id="runtime-session",
        gateway_manager=gateway,
    )


def test_video_control_registers_manager_and_disconnect_clears_it(monkeypatch) -> None:
    asyncio.run(_video_control_registers_manager_and_disconnect_clears_it(monkeypatch))


async def _video_control_registers_manager_and_disconnect_clears_it(monkeypatch) -> None:
    runtime = AgentGraphRuntime(registry=ToolRegistry(), config=ProviderConfig())
    gateway = _GatewayManager()
    state = _state(gateway)
    monkeypatch.setattr(agent_service, "_get_shared_agent_runtime", lambda: runtime)
    monkeypatch.setattr(
        agent_service,
        "get_rendering_3d_relay_registry",
        lambda: _RenderingRegistry(),
    )

    response = await agent_service.AssistantControlHandler().handle(
        session_id="vendor-session",
        body={"number": "u1", "callType": "VIDEO"},
        state=state,
    )
    manager = runtime.visual_reminder_registry.peek("u1", "runtime-session")

    assert json.loads(response["body"])["code"] == 0
    assert manager is state.visual_reminder_manager
    assert state.assistant_control_call_type == "VIDEO"

    await agent_service._cleanup_agent_service_connection(
        state,
        gateway_manager=gateway,
        close_code=1000,
        close_reason=None,
    )

    assert runtime.visual_reminder_registry.peek("u1", "runtime-session") is None
    assert manager.list_records() == []
    runtime.close()


def test_audio_control_does_not_register_visual_reminder_manager(monkeypatch) -> None:
    async def run() -> None:
        runtime = AgentGraphRuntime(registry=ToolRegistry(), config=ProviderConfig())
        gateway = _GatewayManager()
        state = _state(gateway)
        monkeypatch.setattr(agent_service, "_get_shared_agent_runtime", lambda: runtime)

        await agent_service.AssistantControlHandler().handle(
            session_id="vendor-session",
            body={"number": "u1", "callType": "AUDIO"},
            state=state,
        )

        assert state.visual_reminder_manager is None
        assert runtime.visual_reminder_registry.peek("u1", "runtime-session") is None
        runtime.close()

    asyncio.run(run())


def test_gateway_metadata_carries_structured_video_call_type() -> None:
    gateway = _GatewayManager()
    state = _state(gateway)
    state.assistant_control_call_type = "VIDEO"

    metadata = agent_service._agent_service_gateway_metadata(
        state=state,
        user_number="u1",
        chat_index="chat-1",
        content_count=1,
    )

    assert metadata["agent_service"]["call_type"] == "VIDEO"


def test_visual_reminder_response_uses_independent_media_chat_index() -> None:
    state = _state(_GatewayManager())
    state.response_session_id = "response-session"
    reservation = VisualReminderReservation(
        reminder_id="reminder-1",
        reservation_id="reservation-1",
        target="水已经烧开",
        message="水烧开了",
        similarity=0.9,
    )

    envelope = agent_service._visual_reminder_chat_response(state, reservation)
    body = json.loads(envelope["body"])

    assert envelope["message"] == "chatResponse"
    assert envelope["sessionId"] == "response-session"
    assert body["message"]["chatIndex"] == "visual-reminder:reminder-1"
    assert body["message"]["content"]["intentResult"] == {
        "description": "水烧开了",
        "status": "SUCCESS",
    }
