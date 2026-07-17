import asyncio
import json

from scripts import run_client


def test_agent_service_ws_url_uses_media_endpoint_and_optional_session() -> None:
    assert (
        run_client.agent_service_ws_url("http://127.0.0.1:8000", session_id=None)
        == "ws://127.0.0.1:8000/agent-service/v1"
    )
    assert (
        run_client.agent_service_ws_url(
            "https://agent.example.test/base", session_id="media-s1"
        )
        == "wss://agent.example.test/base/agent-service/v1?sessionId=media-s1"
    )


def test_media_envelope_uses_stringified_body_and_session_id() -> None:
    envelope = run_client.media_envelope(
        "chat",
        {"chatIndex": "chat-1", "userNumber": "10086"},
        session_id="media-s1",
    )

    assert envelope["message"] == "chat"
    assert envelope["sessionId"] == "media-s1"
    assert isinstance(envelope["body"], str)
    assert json.loads(envelope["body"]) == {
        "chatIndex": "chat-1",
        "userNumber": "10086",
    }


def test_chat_body_matches_media_agent_protocol() -> None:
    body = run_client.chat_body(
        text="你好",
        chat_index="chat-7",
        user_number="10086",
        speaker_number="10086",
        stream=True,
        now=lambda: "2026-07-17T15:00:00+08:00",
    )

    assert body == {
        "chatIndex": "chat-7",
        "userNumber": "10086",
        "contents": [
            {
                "speakerNumber": "10086",
                "speechContent": "你好",
                "time": "2026-07-17T15:00:00+08:00",
            }
        ],
        "stream": True,
    }


def test_assistant_control_body_marks_run_client_for_observability() -> None:
    body = run_client.assistant_control_body(
        user_number="10086",
        call_type="AUDIO",
        model_name=None,
        chat_progress=False,
        chat_response_ack=False,
    )

    assert body["clientInfo"] == {
        "clientType": "run_client",
        "clientName": "scripts/run_client.py",
    }
    assert "clientCapabilities" not in body


def test_console_command_parsing_supports_new_session_and_exit() -> None:
    assert run_client.parse_console_command("/new") == ("new", None)
    assert run_client.parse_console_command("/new media-s2") == ("new", "media-s2")
    assert run_client.parse_console_command("/session media-s3") == ("new", "media-s3")
    assert run_client.parse_console_command("/quit") == ("quit", None)
    assert run_client.parse_console_command("你好") == ("chat", "你好")


def test_chat_response_description_extracts_only_agent_text() -> None:
    body = {
        "message": {
            "chatIndex": "chat-1",
            "content": {
                "intentResult": {
                    "description": "你好，我在。",
                    "status": "SUCCESS",
                }
            },
        },
        "final": True,
        "sequence": 1,
    }

    assert run_client.chat_response_description(body) == "你好，我在。"


def test_chat_send_prints_only_agent_response_text(capsys) -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.received = [
                {
                    "message": "chatProgress",
                    "body": json.dumps(
                        {"chatIndex": "chat-1", "status": "PROCESSING"},
                        ensure_ascii=False,
                    ),
                },
                {
                    "message": "chatResponse",
                    "body": json.dumps(
                        {
                            "message": {
                                "chatIndex": "chat-1",
                                "content": {
                                    "intentResult": {
                                        "description": "你好，我在。",
                                        "status": "SUCCESS",
                                    }
                                },
                            },
                            "final": True,
                        },
                        ensure_ascii=False,
                    ),
                },
            ]

        async def send(self, payload: str) -> None:
            self.sent.append(payload)

        async def recv(self) -> str:
            return json.dumps(self.received.pop(0), ensure_ascii=False)

    websocket = FakeWebSocket()
    ok = asyncio.run(
        run_client._send_chat_and_print_responses(
            websocket,
            text="你好",
            chat_index="chat-1",
            user_number="10086",
            session_id="s1",
            stream=False,
            chat_response_ack=False,
        )
    )

    assert ok is True
    assert capsys.readouterr().out == "你好，我在。\n"


def test_streaming_chat_send_prints_delta_text_once(capsys) -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.received = [
                {
                    "message": "chatResponse",
                    "body": json.dumps(
                        {
                            "message": {
                                "chatIndex": "chat-1",
                                "content": {
                                    "intentResult": {
                                        "description": "你",
                                        "status": "PROCESSING",
                                    }
                                },
                            },
                            "final": False,
                        },
                        ensure_ascii=False,
                    ),
                },
                {
                    "message": "chatResponse",
                    "body": json.dumps(
                        {
                            "message": {
                                "chatIndex": "chat-1",
                                "content": {
                                    "intentResult": {
                                        "description": "好",
                                        "status": "PROCESSING",
                                    }
                                },
                            },
                            "final": False,
                        },
                        ensure_ascii=False,
                    ),
                },
                {
                    "message": "chatResponse",
                    "body": json.dumps(
                        {
                            "message": {
                                "chatIndex": "chat-1",
                                "content": {
                                    "intentResult": {
                                        "description": "",
                                        "status": "SUCCESS",
                                    }
                                },
                            },
                            "final": True,
                        },
                        ensure_ascii=False,
                    ),
                },
            ]

        async def send(self, payload: str) -> None:
            self.sent.append(payload)

        async def recv(self) -> str:
            return json.dumps(self.received.pop(0), ensure_ascii=False)

    websocket = FakeWebSocket()
    ok = asyncio.run(
        run_client._send_chat_and_print_responses(
            websocket,
            text="你好",
            chat_index="chat-1",
            user_number="10086",
            session_id="s1",
            stream=True,
            chat_response_ack=False,
        )
    )

    assert ok is True
    assert capsys.readouterr().out == "你好\n"
