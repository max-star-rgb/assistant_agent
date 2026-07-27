"""Gateway connection ownership and session-owned relay contract."""

from __future__ import annotations

import asyncio

from assistant_agent.gateway.bridge import GatewayBridge, GatewayConnectionPolicy
from assistant_agent.gateway.protocol import CALL_HANGUP, CALL_INCOMING, frame
from assistant_agent.gateway.session import GatewaySessionManager
from assistant_agent.gateway.transport import Endpoint, InMemoryDuplex
from assistant_agent.gateway.runtime_types import RealtimeAgentResult


async def _receive(endpoint: Endpoint, *, timeout: float = 1.0) -> dict:
    return await asyncio.wait_for(anext(endpoint.__aiter__()), timeout=timeout)


class _HangupBackend:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        self.started.set()
        while not cancel_token.is_cancelled():
            await asyncio.sleep(0.001)
        self.cancelled.set()
        return RealtimeAgentResult(status="cancelled", run_id=request.run_id)


def test_newest_connection_takes_over_session_relay_without_cancelling_run() -> None:
    asyncio.run(_assert_newest_connection_takes_over_session_relay())


async def _assert_newest_connection_takes_over_session_relay() -> None:
    runtime_bridge_ep, runtime_session_ep = InMemoryDuplex.create_pair()
    first_bridge_ep, first_client_ep = InMemoryDuplex.create_pair()
    second_bridge_ep, second_client_ep = InMemoryDuplex.create_pair()
    bridge = GatewayBridge(
        connection_policy=GatewayConnectionPolicy(detach_grace_s=0.05)
    )

    first_task = asyncio.create_task(
        bridge.bridge(
            client_id="client-1",
            client_ep=first_bridge_ep,
            runtime_ep=runtime_bridge_ep,
            user_id="user-1",
            session_id="session-1",
        )
    )
    await asyncio.sleep(0)

    await runtime_session_ep.send(
        frame(
            type="run.started",
            user_id="user-1",
            session_id="session-1",
            run_id="run-1",
            turn_id="turn-1",
        )
    )
    assert (await _receive(first_client_ep))["type"] == "run.started"

    second_task = asyncio.create_task(
        bridge.bridge(
            client_id="client-2",
            client_ep=second_bridge_ep,
            runtime_ep=runtime_bridge_ep,
            user_id="user-1",
            session_id="session-1",
        )
    )
    await asyncio.wait_for(first_task, timeout=1.0)

    # Superseding the old connection is an ownership transfer, not a transport
    # disconnect, so it must not synthesize run.cancel.
    try:
        unexpected = await _receive(runtime_session_ep, timeout=0.05)
    except asyncio.TimeoutError:
        unexpected = None
    assert unexpected is None

    await runtime_session_ep.send(
        frame(
            type="stream.chunk",
            user_id="user-1",
            session_id="session-1",
            run_id="run-1",
            turn_id="turn-1",
            payload={"text": "new owner only"},
        )
    )
    delivered = await _receive(second_client_ep)
    assert delivered["type"] == "stream.chunk"
    assert delivered["payload"]["text"] == "new owner only"

    try:
        stale_delivery = await _receive(first_client_ep, timeout=0.05)
    except asyncio.TimeoutError:
        stale_delivery = None
    assert stale_delivery is None

    # A true disconnect first detaches delivery and leaves the run alive during
    # the reconnect grace window.
    await second_client_ep.close()
    await asyncio.wait_for(second_task, timeout=1.0)
    try:
        early_cancel = await _receive(runtime_session_ep, timeout=0.02)
    except asyncio.TimeoutError:
        early_cancel = None
    assert early_cancel is None

    cancel = await _receive(runtime_session_ep)
    assert cancel["type"] == "run.cancel"
    assert cancel["run_id"] == "run-1"
    assert cancel["payload"] == {
        "source": "gateway_disconnect",
        "reason": "reconnect_grace_expired",
    }

    await runtime_session_ep.close()


def test_detached_connection_replays_outbox_after_cursor_on_resume() -> None:
    asyncio.run(_assert_detached_connection_replays_outbox_after_cursor_on_resume())


async def _assert_detached_connection_replays_outbox_after_cursor_on_resume() -> None:
    runtime_bridge_ep, runtime_session_ep = InMemoryDuplex.create_pair()
    first_bridge_ep, first_client_ep = InMemoryDuplex.create_pair()
    bridge = GatewayBridge(
        connection_policy=GatewayConnectionPolicy(
            detach_grace_s=0.2,
            outbox_max_frames=8,
        )
    )
    first_task = asyncio.create_task(
        bridge.bridge(
            client_id="client-1",
            client_ep=first_bridge_ep,
            runtime_ep=runtime_bridge_ep,
            user_id="user-1",
            session_id="session-1",
        )
    )
    await asyncio.sleep(0)
    await runtime_session_ep.send(
        frame(
            type="run.started",
            user_id="user-1",
            session_id="session-1",
            run_id="run-1",
            turn_id="turn-1",
        )
    )
    started = await _receive(first_client_ep)
    assert started["delivery_cursor"] == 1

    await first_client_ep.close()
    await asyncio.wait_for(first_task, timeout=1.0)
    await runtime_session_ep.send(
        frame(
            type="stream.chunk",
            session_id="session-1",
            run_id="run-1",
            turn_id="turn-1",
            payload={"text": "during detach"},
        )
    )

    second_bridge_ep, second_client_ep = InMemoryDuplex.create_pair()
    second_task = asyncio.create_task(
        bridge.bridge(
            client_id="client-2",
            client_ep=second_bridge_ep,
            runtime_ep=runtime_bridge_ep,
            user_id="user-1",
            session_id="session-1",
        )
    )
    await asyncio.sleep(0)
    await second_client_ep.send(
        frame(
            type="session.resume",
            user_id="user-1",
            session_id="session-1",
            payload={"cursor": 1},
        )
    )

    replayed = await _receive(second_client_ep)
    attached = await _receive(second_client_ep)
    assert replayed["type"] == "stream.chunk"
    assert replayed["delivery_cursor"] == 2
    assert replayed["payload"]["text"] == "during detach"
    assert attached["type"] == "session.attached"
    assert attached["payload"] == {
        "state": "ACTIVE",
        "cursor": 2,
        "replayed": 1,
        "replay_truncated": False,
        "earliest_available_cursor": 1,
    }

    try:
        unexpected_cancel = await _receive(runtime_session_ep, timeout=0.22)
    except asyncio.TimeoutError:
        unexpected_cancel = None
    assert unexpected_cancel is None

    await second_client_ep.close()
    await asyncio.wait_for(second_task, timeout=1.0)
    await runtime_session_ep.close()


def test_hangup_destroys_logical_agent_session_and_connection_can_start_another() -> None:
    asyncio.run(_assert_hangup_destroys_logical_agent_session())


async def _assert_hangup_destroys_logical_agent_session() -> None:
    backend = _HangupBackend()
    manager = GatewaySessionManager(
        backend_factory=lambda: backend,
        start_reaper=False,
    )
    bridge = GatewayBridge(session_manager=manager)
    bridge_ep, client_ep = InMemoryDuplex.create_pair()
    bridge_task = asyncio.create_task(
        bridge.bridge(
            client_id="client-1",
            client_ep=bridge_ep,
            user_id="user-1",
        )
    )

    await client_ep.send(
        frame(
            type=CALL_INCOMING,
            user_id="user-1",
            session_id="call-1",
            payload={"session_id": "call-1"},
        )
    )
    ready = await _receive(client_ep)
    assert ready["type"] == "call.ready"
    assert manager.active_count() == 1

    await client_ep.send(
        frame(
            type="message.user",
            user_id="user-1",
            session_id="call-1",
            payload={"text": "long task", "run_id": "run-1"},
        )
    )
    await asyncio.wait_for(backend.started.wait(), timeout=1.0)
    started = await _receive(client_ep)
    assert started["type"] == "run.started"

    await client_ep.send(
        frame(
            type=CALL_HANGUP,
            user_id="user-1",
            session_id="call-1",
        )
    )
    ack = await _receive(client_ep)
    assert ack["type"] == "call.hangup_ack"
    assert ack["payload"] == {
        "cancelled_active_run": True,
        "session_closed": True,
    }
    await asyncio.wait_for(backend.cancelled.wait(), timeout=1.0)
    for _ in range(20):
        if manager.active_count() == 0:
            break
        await asyncio.sleep(0.01)
    assert manager.active_count() == 0

    await client_ep.send(
        frame(
            type=CALL_INCOMING,
            user_id="user-1",
            session_id="call-2",
            payload={"session_id": "call-2"},
        )
    )
    next_ready = await _receive(client_ep)
    assert next_ready["type"] == "call.ready"
    assert next_ready["session_id"] == "call-2"
    assert manager.active_count() == 1

    await client_ep.close()
    await asyncio.wait_for(bridge_task, timeout=1.0)
    await manager.close()
