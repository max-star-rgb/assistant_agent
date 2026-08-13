from __future__ import annotations

import json
import threading
import time

from fastapi.testclient import TestClient

from assistant_agent.agent_server.media_app import app


class _ScriptedClient:
    def __init__(self) -> None:
        self.created_threads = []
        self.requested_thread_ids = []
        self.runs = []
        self.cancelled = []

    async def create_thread(self, *, metadata, thread_id=None):
        self.created_threads.append(metadata)
        self.requested_thread_ids.append(thread_id)
        return thread_id or "thread-native-1"

    async def stream_run(self, **kwargs):
        self.runs.append(kwargs)
        kwargs["on_run_created"]("run-native-1")
        yield {
            "event": "values",
            "data": {
                "assistant_state": {
                    "run": {"status": "completed"},
                    "final_response": {"message": "native answer"},
                }
            },
            "id": "event-1",
        }

    async def cancel_run(self, *, thread_id, run_id):
        self.cancelled.append((thread_id, run_id))

    async def join_thread(self, *, thread_id, last_event_id):
        if False:
            yield {}


class _BlockingClient(_ScriptedClient):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.released = threading.Event()
        self.cancelled_event = threading.Event()

    async def stream_run(self, **kwargs):
        self.runs.append(kwargs)
        run_id = f"run-native-{len(self.runs)}"
        kwargs["on_run_created"](run_id)
        self.started.set()
        await __import__("asyncio").to_thread(self.released.wait, 2)
        yield {
            "event": "values",
            "data": {
                "assistant_state": {
                    "run": {"status": "completed"},
                    "final_response": {"message": f"answer-{run_id}"},
                }
            },
            "id": f"event-{run_id}",
        }

    async def cancel_run(self, *, thread_id, run_id):
        await super().cancel_run(thread_id=thread_id, run_id=run_id)
        self.cancelled_event.set()
        self.released.set()


class _VideoIngestion:
    def __init__(self) -> None:
        self.calls = []
        self.cleaned = []
        self.cleanup_event = threading.Event()

    def ingest(self, session_id, frame_index, video_hex, video_config, timestamp):
        self.calls.append((session_id, frame_index, video_hex, video_config, timestamp))
        return type("Frame", (), {"video_id": "video-native-1"})()

    def cleanup(self, video_id):
        self.cleaned.append(video_id)
        self.cleanup_event.set()


def _frame(message, body, session_id="vendor-session"):
    return {"message": message, "sessionId": session_id, "body": json.dumps(body)}


def test_custom_route_maps_control_chat_interrupt_and_ack_to_native_resources() -> None:
    scripted = _ScriptedClient()
    app.state.agent_server_client_factory = lambda: scripted
    with TestClient(app) as client:
        with client.websocket_connect("/agent-service/v1?sessionId=vendor-session") as ws:
            ws.send_json(
                _frame(
                    "assistantControl",
                    {"number": "user-1", "callType": "AUDIO"},
                )
            )
            control = ws.receive_json()
            assert json.loads(control["body"])["code"] == 0

            ws.send_json(
                _frame(
                    "chat",
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
                    },
                )
            )
            progress = ws.receive_json()
            final = ws.receive_json()
            assert progress["message"] == "chatProgress"
            final_body = json.loads(final["body"])
            assert final_body["message"]["content"]["intentResult"]["status"] == "SUCCESS"
            assert scripted.runs[0]["thread_id"] == scripted.requested_thread_ids[0]

            ws.send_json(_frame("interrupt", {"number": "user-1"}))
            interrupted = ws.receive_json()
            assert json.loads(interrupted["body"])["message"] == "interrupted"
            assert scripted.cancelled == []

            delivery_id = final_body["deliveryId"]
            ws.send_json(
                _frame(
                    "chatResponseAck",
                    {"chatIndex": "chat-1", "deliveryId": delivery_id},
                )
            )
            ack = ws.receive_json()
            assert json.loads(ack["body"])["deliveryId"] == delivery_id

    assert scripted.created_threads == [{"user_id": "user-1", "protocol": "agent-service-v1"}]


def test_reconnect_reuses_the_native_thread_for_same_vendor_session_and_user() -> None:
    scripted = _ScriptedClient()
    app.state.agent_server_client_factory = lambda: scripted
    with TestClient(app) as client:
        for _ in range(2):
            with client.websocket_connect("/agent-service/v1?sessionId=stable-call") as ws:
                ws.send_json(
                    _frame(
                        "assistantControl",
                        {"number": "user-1", "callType": "AUDIO"},
                        session_id="stable-call",
                    )
                )
                assert json.loads(ws.receive_json()["body"])["code"] == 0
    assert scripted.requested_thread_ids[0] == scripted.requested_thread_ids[1]
    assert scripted.requested_thread_ids[0] not in {None, "stable-call"}


def test_interrupt_is_received_while_native_run_stream_is_active() -> None:
    scripted = _BlockingClient()
    app.state.agent_server_client_factory = lambda: scripted
    with TestClient(app) as client:
        with client.websocket_connect("/agent-service/v1") as ws:
            ws.send_json(_frame("assistantControl", {"number": "user-1", "callType": "AUDIO"}))
            ws.receive_json()
            ws.send_json(
                _frame(
                    "chat",
                    {
                        "chatIndex": "chat-active",
                        "userNumber": "user-1",
                        "contents": [{"speakerNumber": "user-1", "time": "1", "speechContent": "wait"}],
                        "stream": True,
                    },
                )
            )
            assert ws.receive_json()["message"] == "chatProgress"
            assert scripted.started.wait(1)
            ws.send_json(_frame("interrupt", {"number": "user-1"}))
            assert ws.receive_json()["message"] == "interrupt"
            assert scripted.cancelled_event.wait(1)
    assert scripted.cancelled == [(scripted.requested_thread_ids[0], "run-native-1")]


def test_followup_chat_is_submitted_to_native_enqueue_without_waiting_for_first() -> None:
    scripted = _BlockingClient()
    app.state.agent_server_client_factory = lambda: scripted
    with TestClient(app) as client:
        with client.websocket_connect("/agent-service/v1") as ws:
            ws.send_json(_frame("assistantControl", {"number": "user-1", "callType": "AUDIO"}))
            ws.receive_json()
            for index in ("one", "two"):
                ws.send_json(
                    _frame(
                        "chat",
                        {
                            "chatIndex": index,
                            "userNumber": "user-1",
                            "contents": [{"speakerNumber": "user-1", "time": index, "speechContent": index}],
                            "stream": True,
                        },
                    )
                )
                assert ws.receive_json()["message"] == "chatProgress"
            deadline = time.monotonic() + 1
            while len(scripted.runs) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(scripted.runs) == 2
            assert all(run["multitask_strategy"] == "enqueue" for run in scripted.runs)
            scripted.released.set()


def test_duplicate_chat_index_does_not_create_a_second_native_run() -> None:
    scripted = _BlockingClient()
    app.state.agent_server_client_factory = lambda: scripted
    with TestClient(app) as client:
        with client.websocket_connect("/agent-service/v1") as ws:
            ws.send_json(_frame("assistantControl", {"number": "user-1", "callType": "AUDIO"}))
            ws.receive_json()
            request = _frame(
                "chat",
                {
                    "chatIndex": "duplicate",
                    "userNumber": "user-1",
                    "contents": [
                        {"speakerNumber": "user-1", "time": "1", "speechContent": "hello"}
                    ],
                    "stream": True,
                },
            )
            ws.send_json(request)
            assert ws.receive_json()["message"] == "chatProgress"
            assert scripted.started.wait(1)
            ws.send_json(request)
            duplicate = ws.receive_json()
            assert duplicate["message"] == "chat"
            assert "already submitted" in json.loads(duplicate["body"])["message"]
            assert len(scripted.runs) == 1
            scripted.released.set()


def test_disconnect_best_effort_cancels_the_active_native_run() -> None:
    scripted = _BlockingClient()
    app.state.agent_server_client_factory = lambda: scripted
    with TestClient(app) as client:
        with client.websocket_connect("/agent-service/v1") as ws:
            ws.send_json(_frame("assistantControl", {"number": "user-1", "callType": "AUDIO"}))
            ws.receive_json()
            ws.send_json(
                _frame(
                    "chat",
                    {
                        "chatIndex": "disconnect",
                        "userNumber": "user-1",
                        "contents": [{"speakerNumber": "user-1", "time": "1", "speechContent": "wait"}],
                        "stream": True,
                    },
                )
            )
            ws.receive_json()
            assert scripted.started.wait(1)
        assert scripted.cancelled_event.wait(1)
    assert scripted.cancelled == [(scripted.requested_thread_ids[0], "run-native-1")]


def test_video_is_edge_ingested_and_only_stable_reference_enters_graph_context() -> None:
    scripted = _ScriptedClient()
    ingestion = _VideoIngestion()
    app.state.agent_server_client_factory = lambda: scripted
    app.state.video_ingestion_factory = lambda: ingestion
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/agent-service/v1") as ws:
                ws.send_json(_frame("assistantControl", {"number": "user-1", "callType": "VIDEO"}))
                ws.receive_json()
                ws.send_json(
                    _frame(
                        "video",
                        {
                            "userNumber": "user-1",
                            "videoIndex": "7",
                            "contents": [
                                {
                                    "speakerNumber": "user-1",
                                    "time": "9",
                                    "videoContent": "00000165",
                                }
                            ],
                            "videoConfig": {"codec": "H264"},
                        },
                    )
                )
                assert ws.receive_json()["message"] == "videoResponse"
                ws.send_json(
                    _frame(
                        "chat",
                        {
                            "chatIndex": "video-chat",
                            "userNumber": "user-1",
                            "contents": [
                                {"speakerNumber": "user-1", "time": "10", "speechContent": "what do you see"}
                            ],
                            "stream": True,
                        },
                    )
                )
                ws.receive_json()
                ws.receive_json()
    finally:
        del app.state.video_ingestion_factory
    assert ingestion.calls[0][1:] == ("7", "00000165", {"codec": "H264"}, "9")
    request_input = scripted.runs[0]["input"]["request_input"]
    assert request_input["video_ids"] == ["video-native-1"]
    assert "videoContent" not in json.dumps(
        {
            "input": scripted.runs[0]["input"],
            "context": scripted.runs[0]["context"],
        }
    )
    assert ingestion.cleanup_event.wait(1)
    assert ingestion.cleaned == ["video-native-1"]
