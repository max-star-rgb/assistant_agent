from __future__ import annotations

from dataclasses import fields

from assistant_agent.agent_server.media_session import MediaConnectionSession
from assistant_agent.agent_server.media_protocol import (
    parse_envelope,
    parse_chat,
    success_chat_response,
)


def test_media_session_tracks_only_native_resource_correlation() -> None:
    names = {item.name for item in fields(MediaConnectionSession)}

    assert names == {
        "connection_id",
        "protocol_session_id",
        "user_id",
        "thread_id",
        "active_runs",
        "deliveries",
        "last_event_id",
    }


def test_chat_parser_and_success_projection_keep_vendor_wire_shape() -> None:
    envelope = parse_envelope(
        {
            "message": "chat",
            "sessionId": "vendor-session",
            "body": '{"chatIndex":"chat-1","userNumber":"user-1",'
            '"contents":[{"speakerNumber":"user-1","time":"1",'
            '"speechContent":"你好"}],"stream":true}',
        }
    )
    chat = parse_chat(envelope)
    response = success_chat_response(
        session_id="vendor-session",
        chat=chat,
        text="你好，收到",
        delivery_id="delivery-1",
    )

    assert chat.chat_index == "chat-1"
    assert chat.text == "你好"
    projected = parse_envelope(response)
    assert projected.message == "chatResponse"
    assert projected.body["message"]["chatIndex"] == "chat-1"
    assert projected.body["message"]["content"]["intentResult"] == {
        "description": "你好，收到",
        "status": "SUCCESS",
    }
    assert projected.body["deliveryId"] == "delivery-1"


def test_session_registers_one_run_per_chat_and_cancel_is_precise() -> None:
    session = MediaConnectionSession(connection_id="connection-1")
    session.bind_control(
        protocol_session_id="vendor-session",
        user_id="user-1",
        thread_id="thread-1",
    )
    session.bind_run(chat_index="chat-1", run_id="run-1")
    session.bind_run(chat_index="chat-1", run_id="run-1")

    assert session.active_runs == {"chat-1": "run-1"}
    assert session.active_run_targets() == (("thread-1", "run-1"),)
