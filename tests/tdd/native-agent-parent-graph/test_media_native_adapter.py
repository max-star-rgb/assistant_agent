"""RED/GREEN coverage for the thin /agent-service/v1 native SDK adapter."""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from assistant_agent.agent_server.media_app import (
    _cancel_active_runs,
    _run_chat,
    media_graph_input,
    native_response_from_state,
)
from assistant_agent.agent_server.media_protocol import (
    MediaProtocolError,
    parse_chat,
    parse_envelope,
)
from assistant_agent.agent_server.media_session import MediaConnectionSession


def _chat(*, mode: str = "fast"):
    return parse_chat(
        parse_envelope(
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
                                "speechContent": "你好",
                            }
                        ],
                        "stream": True,
                        "assistantMode": mode,
                    }
                ),
            }
        )
    )


@pytest.mark.parametrize("mode", ["fast", "planning"])
def test_media_mode_maps_directly_to_structured_graph_input(mode: str) -> None:
    chat = _chat(mode=mode)

    assert chat.execution_mode == mode
    assert media_graph_input(chat, video_ids=["video-1"]) == {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "你好"},
                    {"type": "video", "id": "video-1"},
                ],
            }
        ],
        "execution_mode": mode,
    }


def test_media_rejects_retired_standard_deep_research_modes() -> None:
    with pytest.raises(MediaProtocolError, match="fast or planning"):
        _chat(mode="deep_research")


def test_native_response_uses_latest_standard_ai_message() -> None:
    assert native_response_from_state(
        {
            "messages": [
                HumanMessage(content="question"),
                AIMessage(content="old"),
                AIMessage(content=[{"type": "text", "text": "final"}]),
            ]
        }
    ) == {"message": "final"}
    assert native_response_from_state(
        {"messages": [{"role": "assistant", "content": "serialized"}]}
    ) == {"message": "serialized"}


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent = []

    async def send_json(self, value) -> None:
        self.sent.append(value)


class FakeClient:
    def __init__(self) -> None:
        self.stream_kwargs = None
        self.cancelled = []

    async def stream_run(self, **kwargs):
        self.stream_kwargs = kwargs
        kwargs["on_run_created"]("run-1")
        yield {
            "event": "values",
            "id": "event-1",
            "data": {"messages": [AIMessage(content="native answer")]},
        }

    async def cancel_run(self, *, thread_id: str, run_id: str) -> None:
        self.cancelled.append((thread_id, run_id))


def test_run_chat_uses_versioned_assistant_and_native_run_protocol() -> None:
    websocket = FakeWebSocket()
    client = FakeClient()
    session = MediaConnectionSession(connection_id="connection-1")
    session.bind_control(
        protocol_session_id="vendor-session",
        user_id="user-1",
        thread_id="thread-1",
        media_capabilities=("video",),
    )
    session.bind_video("video-1")

    asyncio.run(
        _run_chat(
            websocket,
            session=session,
            client=client,
            chat=_chat(mode="planning"),
            response_session_id="vendor-session",
            delivery_id="delivery-1",
            send_lock=asyncio.Lock(),
            interrupted_chats=set(),
        )
    )

    assert client.stream_kwargs["assistant_id"] == "assistant-native-v1"
    assert client.stream_kwargs["input"] == media_graph_input(
        _chat(mode="planning"), video_ids=["video-1"]
    )
    assert client.stream_kwargs["context"] == {
        "user_id": "user-1",
        "tenant_id": "media-service",
        "entry_profile": "agent_service",
        "media_capabilities": ["video"],
    }
    body = json.loads(websocket.sent[-1]["body"])
    assert body["message"]["content"]["intentResult"]["description"] == "native answer"


def test_interrupt_and_session_state_are_native_resource_correlation_only() -> None:
    client = FakeClient()
    session = MediaConnectionSession(connection_id="connection-1")
    session.bind_control(
        protocol_session_id="vendor-session",
        user_id="user-1",
        thread_id="thread-1",
    )
    session.bind_run(chat_index="chat-1", run_id="run-1")

    asyncio.run(_cancel_active_runs(session=session, client=client))

    assert client.cancelled == [("thread-1", "run-1")]
    assert set(type(session).__dataclass_fields__).isdisjoint(
        {"graph_phase", "checkpoint", "cancel_token", "assistant_state"}
    )
