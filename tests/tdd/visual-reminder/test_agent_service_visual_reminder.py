from __future__ import annotations

import asyncio
import json

import pytest

from assistant_agent.api import agent_service_websocket as agent_service
from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.proactive_messages import ProactiveMessage
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


class _ArtifactDeliveryHub:
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
        "get_gateway_artifact_delivery_hub",
        lambda: _ArtifactDeliveryHub(),
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


def test_repeated_assistant_control_is_rejected_without_replacing_manager(monkeypatch) -> None:
    async def run() -> None:
        runtime = AgentGraphRuntime(registry=ToolRegistry(), config=ProviderConfig())
        gateway = _GatewayManager()
        state = _state(gateway)
        monkeypatch.setattr(agent_service, "_get_shared_agent_runtime", lambda: runtime)

        handler = agent_service.AssistantControlHandler()
        await handler.handle(
            session_id="vendor-session",
            body={"number": "u1", "callType": "VIDEO"},
            state=state,
        )
        original = state.visual_reminder_manager

        with pytest.raises(
            agent_service.AgentServiceProtocolError,
            match="assistantControl already received",
        ):
            await handler.handle(
                session_id="vendor-session",
                body={"number": "u1", "callType": "VIDEO"},
                state=state,
            )

        assert state.visual_reminder_manager is original
        assert runtime.visual_reminder_registry.peek("u1", "runtime-session") is original
        runtime.close()

    asyncio.run(run())


def test_video_user_must_match_visual_reminder_connection_owner(monkeypatch) -> None:
    async def run() -> None:
        runtime = AgentGraphRuntime(registry=ToolRegistry(), config=ProviderConfig())
        gateway = _GatewayManager()
        state = _state(gateway)
        monkeypatch.setattr(agent_service, "_get_shared_agent_runtime", lambda: runtime)

        await agent_service.AssistantControlHandler().handle(
            session_id="vendor-session",
            body={"number": "u1", "callType": "VIDEO"},
            state=state,
        )

        with pytest.raises(
            agent_service.AgentServiceProtocolError,
            match="video userNumber does not match assistantControl number",
        ):
            await agent_service.VideoHandler().handle(
                session_id="vendor-session",
                body={"userNumber": "u2"},
                state=state,
            )

        assert state.video_ingestion is None
        runtime.close()

    asyncio.run(run())


def test_video_is_rejected_before_connection_control_handshake() -> None:
    async def run() -> None:
        state = _state(_GatewayManager())

        with pytest.raises(
            agent_service.AgentServiceProtocolError,
            match="video requires assistantControl handshake",
        ):
            await agent_service.VideoHandler().handle(
                session_id="vendor-session",
                body={"userNumber": "u1"},
                state=state,
            )

        assert state.video_ingestion is None
        assert state.video_observer is None

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
    message = ProactiveMessage(
        message_id="reminder-1",
        user_id="u1",
        session_id="runtime-session",
        kind="visual_reminder",
        content="水烧开了",
        delivery_mode="connection_ephemeral",
    )

    envelope = agent_service._proactive_message_chat_response(state, message)
    body = json.loads(envelope["body"])

    assert envelope["message"] == "chatResponse"
    assert envelope["sessionId"] == "response-session"
    assert body["message"]["chatIndex"] == "visual-reminder:reminder-1"
    assert body["message"]["content"]["intentResult"] == {
        "description": "水烧开了",
        "status": "SUCCESS",
    }


def test_proactive_sink_waits_for_active_chat_and_reports_server_sent() -> None:
    asyncio.run(_proactive_sink_waits_for_active_chat_and_reports_server_sent())


async def _proactive_sink_waits_for_active_chat_and_reports_server_sent() -> None:
    class WebSocket:
        def __init__(self) -> None:
            self.sent = []

        async def send_text(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

    websocket = WebSocket()
    state = _state(_GatewayManager())
    state.response_session_id = "response-session"
    release_chat = asyncio.Event()

    async def active_chat() -> None:
        await release_chat.wait()

    chat_task = asyncio.create_task(active_chat())
    state.chat_tasks.add(chat_task)
    sink = agent_service._AgentServiceProactiveMessageSink(
        websocket=websocket,
        state=state,
    )
    delivery = asyncio.create_task(
        sink.publish(
            ProactiveMessage(
                message_id="reminder-1",
                user_id="u1",
                session_id="runtime-session",
                kind="visual_reminder",
                content="水烧开了",
                delivery_mode="connection_ephemeral",
            )
        )
    )
    await asyncio.sleep(0)

    assert websocket.sent == []

    release_chat.set()
    await chat_task
    attempt = await delivery

    assert attempt.message_id == "reminder-1"
    assert attempt.status == "sent"
    assert attempt.delivery_scope == "server_transport"
    body = json.loads(websocket.sent[0]["body"])
    assert body["message"]["chatIndex"] == "visual-reminder:reminder-1"
