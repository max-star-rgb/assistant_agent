from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from assistant_agent.agent_server.auth import authenticate
from assistant_agent.agent_server.media_protocol import MediaProtocolError, parse_chat, parse_envelope
from assistant_agent.agent_server.media_session import MediaConnectionSession
from assistant_agent.native_agent.context import AssistantRunContext


@pytest.mark.core_invariant("GATE-001")
def test_agent_server_owns_the_production_graph_and_authenticated_media_route() -> None:
    root = Path(__file__).resolve().parents[3]
    manifest = json.loads((root / "langgraph.json").read_text(encoding="utf-8"))
    assert manifest["graphs"] == {
        "assistant-native-v1": (
            "assistant_agent.agent_server.graph:native_assistant_graph"
        ),
        "assistant-memory-v1": (
            "assistant_agent.agent_server.graph:native_memory_graph"
        ),
    }
    assert manifest["http"] == {
        "app": "assistant_agent.agent_server.media_app:app",
        "enable_custom_route_auth": True,
    }


@pytest.mark.core_invariant("GATE-001")
def test_media_connection_only_correlates_native_thread_run_and_delivery_ids() -> None:
    session = MediaConnectionSession(connection_id="connection-1")
    session.bind_control(
        protocol_session_id="vendor-session",
        user_id="user-1",
        thread_id="thread-1",
    )
    session.bind_run(chat_index="chat-1", run_id="run-1")
    session.bind_delivery(delivery_id="delivery-1", chat_index="chat-1")
    assert session.active_run_targets() == (("thread-1", "run-1"),)
    session.acknowledge(delivery_id="delivery-1", chat_index="chat-1")
    assert session.deliveries == {}


@pytest.mark.core_invariant("GATE-001")
def test_media_wire_parser_is_strict_and_does_not_define_graph_lifecycle() -> None:
    envelope = parse_envelope(
        {
            "message": "chat",
            "sessionId": "vendor-session",
            "body": json.dumps(
                {
                    "chatIndex": "chat-1",
                    "userNumber": "user-1",
                    "contents": [
                        {
                            "speakerNumber": "user-1",
                            "time": "1",
                            "speechContent": "hello",
                        }
                    ],
                    "stream": True,
                }
            ),
        }
    )
    assert parse_chat(envelope).text == "hello"
    with pytest.raises(MediaProtocolError):
        parse_envelope({"message": "chat", "body": "not-json"})


@pytest.mark.core_invariant("IDENT-001")
def test_native_run_context_contains_capabilities_not_identity() -> None:
    context = AssistantRunContext.model_validate(
        {
            "entry_profile": "agent_service",
            "media_capabilities": ["audio"],
        }
    )
    assert set(type(context).model_fields) == {
        "entry_profile",
        "media_capabilities",
        "realtime_media_mode",
    }
    assert context.realtime_media_mode == "none"
    assert context.media_capabilities == ("audio",)


@pytest.mark.core_invariant("IDENT-001")
def test_agent_server_auth_is_tokenless_for_all_provider_modes(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "real")
    monkeypatch.setenv(
        "ASSISTANT_AGENT_SERVER_SERVICE_TOKEN",
        "configured-token-must-be-ignored",
    )

    user = asyncio.run(
        authenticate(
            "Bearer invalid-token",
            {b"x-assistant-user": b"user-sentinel"},
        )
    )

    assert user == {
        "identity": "user-sentinel",
        "permissions": ["assistant:developer"],
        "is_authenticated": True,
    }
