import asyncio


def test_media_client_opens_websocket_with_protocol_sized_receive_limit() -> None:
    from scripts.media_simulator import (
        AGENT_SERVICE_MAX_MESSAGE_BYTES,
        _open_media_session,
    )

    calls: list[dict] = []

    class Socket:
        async def send(self, frame: str) -> None:
            _ = frame

        async def recv(self) -> str:
            return '{}'

    class Websockets:
        async def connect(self, url: str, **kwargs):
            calls.append({"url": url, **kwargs})
            return Socket()

    asyncio.run(
        _open_media_session(
            Websockets(),
            server="http://127.0.0.1:8089",
            session_id="session-sentinel",
            user_number="10086",
            call_type="AUDIO",
            model_name=None,
            chat_progress=True,
            chat_response_ack=True,
        )
    )

    assert AGENT_SERVICE_MAX_MESSAGE_BYTES > 1_133_976
    assert calls == [
        {
            "url": "ws://127.0.0.1:8089/agent-service/v1?sessionId=session-sentinel",
            "max_size": AGENT_SERVICE_MAX_MESSAGE_BYTES,
        }
    ]
