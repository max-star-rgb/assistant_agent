from __future__ import annotations

import asyncio
import json
import threading

import pytest
from fastapi.testclient import TestClient

from assistant_agent.agent_server.media_app import app
from assistant_agent.agent_server.media_protocol import proactive_chat_response
from assistant_agent.agent_server.proactive_delivery import (
    MediaProactiveDeliveryPump,
    ProactiveDeliveryAckError,
)
from assistant_agent.runtime.proactive_delivery import SQLiteProactiveDeliveryStore
from assistant_agent.runtime.proactive_messages import ProactiveMessage


def _message(
    message_id: str,
    *,
    user_id: str = "user-1",
    thread_id: str = "thread-1",
    mode: str = "durable",
) -> ProactiveMessage:
    return ProactiveMessage(
        message_id=message_id,
        user_id=user_id,
        session_id=thread_id,
        kind="system.notice",
        content="content-sentinel",
        delivery_mode=mode,
        source_run_id="run-1",
        source_trace_id="trace-1",
    )


def _body(envelope: dict[str, object]) -> dict[str, object]:
    return json.loads(str(envelope["body"]))


def _pump(
    store,
    sent,
    sent_event,
    *,
    connection_id: str,
    ack_capable: bool = True,
    thread_id: str = "thread-1",
    ack_timeout_seconds: float = 0.05,
):
    async def sender(value):
        sent.append(value)
        sent_event.set()

    return MediaProactiveDeliveryPump(
        store=store,
        user_id="user-1",
        thread_id=thread_id,
        connection_id=connection_id,
        protocol_session_id="vendor-session",
        ack_capable=ack_capable,
        sender=sender,
        ack_timeout_seconds=ack_timeout_seconds,
        lease_seconds=1.0,
        presence_ttl_seconds=60.0,
        poll_interval_seconds=0.001,
    )


def test_proactive_projection_uses_stable_delivery_and_chat_indexes() -> None:
    value = proactive_chat_response(
        session_id="vendor-session",
        message=_message("message-1"),
    )

    body = _body(value)
    assert value["message"] == "chatResponse"
    assert value["sessionId"] == "vendor-session"
    assert body["deliveryId"] == "message-1"
    assert body["message"]["chatIndex"] == "proactive:message-1"
    assert body["message"]["content"]["intentResult"] == {
        "description": "content-sentinel",
        "status": "SUCCESS",
    }
    assert body["final"] is True


def test_durable_pump_waits_for_matching_ack(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLiteProactiveDeliveryStore(tmp_path / "delivery.sqlite3")
        sent = []
        sent_event = asyncio.Event()
        pump = _pump(store, sent, sent_event, connection_id="connection-1")
        await pump.aopen()
        store.enqueue(_message("message-1"))

        delivery = asyncio.create_task(pump.adeliver_once())
        await asyncio.wait_for(sent_event.wait(), timeout=10.0)
        assert store.get("message-1").status == "leased"
        await pump.acknowledge(
            chat_index="proactive:message-1",
            delivery_id="message-1",
        )
        assert await delivery is True
        assert store.get("message-1").status == "acknowledged"
        await pump.aclose()

    asyncio.run(scenario())


def test_unacked_durable_message_is_resent_after_same_thread_reconnect(
    tmp_path,
) -> None:
    async def scenario() -> None:
        store = SQLiteProactiveDeliveryStore(tmp_path / "delivery.sqlite3")
        sent = []
        first_event = asyncio.Event()
        first = _pump(
            store,
            sent,
            first_event,
            connection_id="connection-1",
            ack_timeout_seconds=0.001,
        )
        await first.aopen()
        store.enqueue(_message("message-1"))
        assert await first.adeliver_once() is True
        assert store.get("message-1").status == "queued"
        await first.aclose()

        second_event = asyncio.Event()
        second = _pump(store, sent, second_event, connection_id="connection-2")
        await second.aopen()
        delivery = asyncio.create_task(second.adeliver_once())
        await asyncio.wait_for(second_event.wait(), timeout=10.0)
        await second.acknowledge(
            chat_index="proactive:message-1",
            delivery_id="message-1",
        )
        await delivery
        assert [_body(value)["deliveryId"] for value in sent] == [
            "message-1",
            "message-1",
        ]
        await second.aclose()

    asyncio.run(scenario())


def test_capability_and_ephemeral_boundaries(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLiteProactiveDeliveryStore(tmp_path / "delivery.sqlite3")
        sent = []
        sent_event = asyncio.Event()
        no_ack = _pump(
            store,
            sent,
            sent_event,
            connection_id="connection-1",
            ack_capable=False,
        )
        await no_ack.aopen()
        store.enqueue(_message("durable-1"))
        assert await no_ack.adeliver_once() is False
        assert sent == []
        assert store.get("durable-1").issue_code == "ack_capability_missing"

        ephemeral = store.enqueue(_message("ephemeral-1", mode="connection_ephemeral"))
        assert ephemeral.status == "queued"
        assert await no_ack.adeliver_once() is False
        assert sent == []
        await no_ack.aclose()

        ephemeral_store = SQLiteProactiveDeliveryStore(tmp_path / "ephemeral.sqlite3")
        ephemeral_sent = []
        ephemeral_event = asyncio.Event()
        ephemeral_pump = _pump(
            ephemeral_store,
            ephemeral_sent,
            ephemeral_event,
            connection_id="connection-2",
            ack_capable=False,
        )
        await ephemeral_pump.aopen()
        ephemeral_store.enqueue(_message("ephemeral-2", mode="connection_ephemeral"))
        assert await ephemeral_pump.adeliver_once() is True
        assert ephemeral_store.get("ephemeral-2").status == "sent_unacknowledged"
        await ephemeral_pump.aclose()

    asyncio.run(scenario())


def test_ack_and_claim_are_isolated_by_thread(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLiteProactiveDeliveryStore(tmp_path / "delivery.sqlite3")
        store.enqueue(_message("other-message", thread_id="thread-other"))
        sent = []
        sent_event = asyncio.Event()
        pump = _pump(store, sent, sent_event, connection_id="connection-1")
        await pump.aopen()

        assert await pump.adeliver_once() is False
        with pytest.raises(ProactiveDeliveryAckError):
            await pump.acknowledge(
                chat_index="proactive:wrong-message",
                delivery_id="other-message",
            )
        assert sent == []
        assert store.get("other-message").status == "queued"
        await pump.aclose()

    asyncio.run(scenario())


def test_custom_route_binds_pump_and_routes_proactive_ack(monkeypatch) -> None:
    class Client:
        async def create_thread(self, *, metadata, thread_id=None):
            return thread_id or "thread-1"

        async def cancel_run(self, *, thread_id, run_id):
            return None

    class Pump:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.opened = threading.Event()
            self.closed = threading.Event()
            self.acks = []

        async def aopen(self):
            self.opened.set()

        async def run(self):
            try:
                await asyncio.Event().wait()
            finally:
                self.closed.set()

        async def acknowledge(self, *, chat_index, delivery_id):
            self.acks.append((chat_index, delivery_id))

        async def aclose(self):
            self.closed.set()

    pumps = []

    def pump_factory(**kwargs):
        pump = Pump(**kwargs)
        pumps.append(pump)
        return pump

    monkeypatch.setattr(
        app.state,
        "agent_server_client_factory",
        lambda: Client(),
        raising=False,
    )
    monkeypatch.setattr(
        app.state,
        "proactive_delivery_store_factory",
        object,
        raising=False,
    )
    monkeypatch.setattr(
        app.state,
        "proactive_delivery_pump_factory",
        pump_factory,
        raising=False,
    )
    with TestClient(app) as client:
        with client.websocket_connect("/agent-service/v1") as websocket:
            websocket.send_json(
                {
                    "message": "assistantControl",
                    "sessionId": "vendor-session",
                    "body": json.dumps(
                        {
                            "number": "user-1",
                            "callType": "AUDIO",
                            "clientCapabilities": {"chatResponseAck": True},
                        }
                    ),
                }
            )
            assert json.loads(websocket.receive_json()["body"])["code"] == 0
            assert pumps[0].opened.wait(1.0)
            websocket.send_json(
                {
                    "message": "chatResponseAck",
                    "sessionId": "vendor-session",
                    "body": json.dumps(
                        {
                            "chatIndex": "proactive:message-1",
                            "deliveryId": "message-1",
                        }
                    ),
                }
            )
            response = websocket.receive_json()
            assert json.loads(response["body"])["code"] == 0

    assert pumps[0].acks == [("proactive:message-1", "message-1")]
    assert pumps[0].kwargs["thread_id"]
    assert pumps[0].kwargs["ack_capable"] is True
    assert pumps[0].closed.wait(1.0)
