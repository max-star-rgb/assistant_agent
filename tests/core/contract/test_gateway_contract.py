from __future__ import annotations

import json
from pathlib import Path

import pytest

from assistant_agent.agent_server.context import AgentServerRunContext
from assistant_agent.agent_server.media_protocol import MediaProtocolError, parse_chat, parse_envelope
from assistant_agent.agent_server.media_session import MediaConnectionSession


@pytest.mark.core_invariant("GATE-001")
def test_agent_server_owns_the_production_graph_and_authenticated_media_route() -> None:
    root = Path(__file__).resolve().parents[3]
    manifest = json.loads((root / "langgraph.json").read_text(encoding="utf-8"))
    assert manifest["graphs"] == {
        "assistant": "assistant_agent.agent_server.graph:assistant_graph"
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
def test_native_run_context_separates_user_tenant_and_media_capabilities() -> None:
    context = AgentServerRunContext.model_validate(
        {
            "user_id": "user-1",
            "tenant_id": "tenant-1",
            "assistant_mode": "standard",
            "entry_profile": "agent_service",
            "media_capabilities": ["audio"],
        }
    )
    assert context.user_id == "user-1"
    assert context.tenant_id == "tenant-1"
    assert context.media_capabilities == ("audio",)
