from __future__ import annotations

import json

from fastapi.testclient import TestClient

from assistant_agent.agent_server.media_app import app


class _ScriptedClient:
    def __init__(self) -> None:
        self.created_threads = []
        self.runs = []
        self.cancelled = []

    async def create_thread(self, *, metadata):
        self.created_threads.append(metadata)
        return "thread-native-1"

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
            assert scripted.runs[0]["thread_id"] == "thread-native-1"

            ws.send_json(_frame("interrupt", {"number": "user-1"}))
            interrupted = ws.receive_json()
            assert json.loads(interrupted["body"])["message"] == "interrupted"
            assert scripted.cancelled == [("thread-native-1", "run-native-1")]

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
