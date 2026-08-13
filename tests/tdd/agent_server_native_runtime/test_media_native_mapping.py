from __future__ import annotations

import asyncio
from dataclasses import fields
from pathlib import Path
from uuid import UUID

from assistant_agent.agent_server.auth import (
    allow_assistant_read,
    deny_all,
    authorize_thread_create,
    authorize_thread_read,
    authorize_thread_search,
    scope_store,
    authenticate,
    delegated_identity_signature,
)
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
        "client_capabilities",
        "media_capabilities",
        "video_ids",
        "submitted_chat_indexes",
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
        response={"message": "你好，收到", "output_refs": [], "citations": []},
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


def test_session_rejects_duplicate_chat_before_a_second_native_run() -> None:
    session = MediaConnectionSession(connection_id="connection-1")

    session.begin_chat("chat-1")

    try:
        session.begin_chat("chat-1")
    except ValueError as exc:
        assert str(exc) == "chatIndex already submitted on this connection"
    else:
        raise AssertionError("duplicate chatIndex must be rejected")


def test_agent_server_resource_authorization_scopes_native_resources_to_principal() -> None:
    class User:
        identity = "principal-1"
        permissions = []

    class Context:
        user = User()

    ctx = Context()
    create = {"metadata": {"protocol": "agent-service-v1"}}
    asyncio.run(authorize_thread_create(ctx, create))
    assert create["metadata"] == {
        "protocol": "agent-service-v1",
        "owner": "principal-1",
    }
    assert asyncio.run(authorize_thread_read(ctx, {"thread_id": UUID(int=1)})) == {
        "owner": "principal-1"
    }
    assert asyncio.run(authorize_thread_search(ctx, {})) == {"owner": "principal-1"}
    store = {"namespace": ("assistant_agent", "subject-1")}
    asyncio.run(scope_store(ctx, store))
    assert store["namespace"] == ("principal-1", "assistant_agent", "subject-1")
    assert asyncio.run(allow_assistant_read(ctx, {})) is True
    assert asyncio.run(deny_all(ctx, {})) is False


def test_mock_auth_can_model_two_end_users_without_trusting_run_context(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "mock")

    first = asyncio.run(
        authenticate(
            None,
            {b"x-assistant-user": b"user-a", b"x-assistant-tenant": b"tenant-a"},
        )
    )
    second = asyncio.run(
        authenticate(
            None,
            {b"x-assistant-user": b"user-b", b"x-assistant-tenant": b"tenant-a"},
        )
    )

    assert first["identity"] == "user-a"
    assert first["tenant_id"] == "tenant-a"
    assert second["identity"] == "user-b"
    assert first["identity"] != second["identity"]


def test_real_auth_requires_signed_end_user_delegation(monkeypatch) -> None:
    monkeypatch.setenv("MULTIMODAL_AGENT_PROVIDER_MODE", "real")
    monkeypatch.setenv("ASSISTANT_AGENT_SERVER_SERVICE_TOKEN", "service-secret")
    signature = delegated_identity_signature(
        secret="service-secret",
        identity="user-a",
        tenant_id="tenant-a",
    )

    user = asyncio.run(
        authenticate(
            "Bearer service-secret",
            {
                b"x-assistant-user": b"user-a",
                b"x-assistant-tenant": b"tenant-a",
                b"x-assistant-signature": signature.encode(),
            },
        )
    )

    assert user == {
        "identity": "user-a",
        "permissions": [],
        "is_authenticated": True,
        "tenant_id": "tenant-a",
    }


def test_success_projection_preserves_citations_durable_refs_and_generated_images(
    tmp_path: Path,
) -> None:
    image = tmp_path / "generated.png"
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    )
    envelope = parse_envelope(
        {
            "message": "chat",
            "body": '{"chatIndex":"chat-2","userNumber":"user-1",'
            '"contents":[{"speakerNumber":"user-1","time":"1",'
            '"speechContent":"draw"}],"stream":true}',
        }
    )
    response = success_chat_response(
        session_id=None,
        chat=parse_chat(envelope),
        response={
            "message": "answer [1]",
            "output_refs": [
                "/artifacts/generated/generated.png",
                "workflow://workflow-1",
            ],
            "citations": [
                {
                    "source_id": "source_1",
                    "title": "source",
                    "url": "https://example.com",
                    "start_index": 7,
                    "end_index": 10,
                }
            ],
        },
        delivery_id="delivery-2",
        capabilities={"urlCitationAnnotationsV1": True},
        artifact_dir=tmp_path,
    )
    body = parse_envelope(response).body
    result = body["message"]["content"]["intentResult"]
    assert result["annotations"][0]["url"] == "https://example.com"
    assert body["outputRefs"] == ["workflow://workflow-1"]
    assert result["detail"][0]["type"] == "IMAGE"
    assert result["detail"][0]["imageId"] == "generated"
