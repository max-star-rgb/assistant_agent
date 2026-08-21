from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from assistant_agent.agent_server.client import SdkAgentServerClient
from assistant_agent.agent_server.config import ASSISTANT_GRAPH_ID
from assistant_agent.agent_server.media_app import (
    _ProactiveDeliveryConnection,
    _VisualPerceptionConnection,
    _handle_frame,
    _native_thread_id,
)
from assistant_agent.agent_server.media_protocol import parse_envelope
from assistant_agent.agent_server.media_session import MediaConnectionSession


class _ExistingV1Threads:
    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> dict[str, Any]:
        self.create_calls.append(kwargs)
        return {
            "thread_id": kwargs["thread_id"],
            "metadata": {"assistant_graph_id": "assistant-native-v1"},
        }


class _NeverRuns:
    def __init__(self) -> None:
        self.stream_calls = 0

    async def stream(self, *_args: Any, **_kwargs: Any):
        self.stream_calls += 1
        if False:
            yield None


class _ArtifactHub:
    async def register(self, **_kwargs: Any) -> None:
        raise AssertionError("legacy media thread must be rejected before binding")


def _sdk_client() -> tuple[SdkAgentServerClient, Any]:
    sdk = SimpleNamespace(threads=_ExistingV1Threads(), runs=_NeverRuns())
    client = object.__new__(SdkAgentServerClient)
    client._client = sdk
    return client, sdk


def test_media_v2_thread_uuid_is_stable_and_does_not_collide_with_legacy_v1() -> None:
    """Catches a v2 reconnect resolving to the pre-versioned v1 UUID."""

    first = _native_thread_id(protocol_session_id="call-1", user_id="user-1")
    second = _native_thread_id(protocol_session_id="call-1", user_id="user-1")
    versioned_v1 = _native_thread_id(
        protocol_session_id="call-1",
        user_id="user-1",
        graph_id="assistant-native-v1",
    )
    legacy_v1 = str(
        uuid5(
            NAMESPACE_URL,
            "assistant-agent:agent-service-v1:user-1:call-1",
        )
    )

    assert first == second
    assert first != versioned_v1
    assert first != legacy_v1


def test_media_control_entry_rejects_existing_v1_thread_before_any_run() -> None:
    """Catches the ordinary media entry continuing a legacy thread into v2."""

    client, sdk = _sdk_client()
    session = MediaConnectionSession(connection_id="connection-1")
    frame = parse_envelope(
        {
            "message": "assistantControl",
            "sessionId": "call-1",
            "body": json.dumps({"number": "user-1", "callType": "AUDIO"}),
        }
    )

    with pytest.raises(ValueError, match="thread graph"):
        asyncio.run(
            _handle_frame(
                SimpleNamespace(scope={}, headers={}),
                session=session,
                client=client,
                frame=frame,
                send_lock=asyncio.Lock(),
                chat_tasks={},
                interrupted_chats=set(),
                video_ingestion=object(),
                artifact_hub=_ArtifactHub(),
                proactive_delivery=_ProactiveDeliveryConnection(),
                visual_module=object(),
                visual_perception=_VisualPerceptionConnection(),
            )
        )

    assert session.thread_id is None
    assert sdk.runs.stream_calls == 0
    assert sdk.threads.create_calls[0]["metadata"]["assistant_graph_id"] == (
        ASSISTANT_GRAPH_ID
    )
