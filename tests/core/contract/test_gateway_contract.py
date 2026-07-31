from __future__ import annotations

import asyncio

import pytest

from assistant_agent.gateway import GatewaySessionManager, frame
from assistant_agent.gateway.bridge import GatewayBridge, GatewayConnectionPolicy
from assistant_agent.gateway.protocol import CALL_HANGUP, CALL_INCOMING
from assistant_agent.gateway.runtime_types import RealtimeAgentEvent, RealtimeAgentResult
from assistant_agent.gateway.transport import Endpoint, InMemoryDuplex
from assistant_agent.gateway.turn_facade import GatewayTurnFacade, GatewayTurnRequest


class ControllableBackend:
    def __init__(self) -> None:
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.first_cancelled = asyncio.Event()
        self.requests = []
        self.statuses: list[str] = []

    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        self.requests.append(request)
        if request.text == "first-sentinel":
            self.statuses.append("first-started")
            self.first_started.set()
            while (
                not self.release_first.is_set()
                and not cancel_token.is_cancelled()
            ):
                await asyncio.sleep(0.001)
            if cancel_token.is_cancelled():
                self.statuses.append("first-cancelled")
                self.first_cancelled.set()
                return RealtimeAgentResult(
                    status="cancelled",
                    run_id=request.run_id,
                )
            self.statuses.append("first-completed")
        else:
            self.statuses.append("second-started")
        return RealtimeAgentResult(
            status="completed",
            run_id=request.run_id,
            response_text=request.text,
        )


class HangupBackend:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        self.started.set()
        while not cancel_token.is_cancelled():
            await asyncio.sleep(0.001)
        self.cancelled.set()
        return RealtimeAgentResult(
            status="cancelled",
            run_id=request.run_id,
        )


class ProvisionalTextBackend:
    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        assert event_sink is not None
        await event_sink(
            RealtimeAgentEvent(
                type="response.chunk",
                text="provisional-sentinel",
                payload={"token_streaming": True},
            )
        )
        return RealtimeAgentResult(
            status="completed",
            run_id=request.run_id,
            response_text="final-sentinel",
        )


async def _receive(
    endpoint: Endpoint,
    *,
    timeout: float = 1.0,
) -> dict:
    return await asyncio.wait_for(
        anext(endpoint.__aiter__()),
        timeout=timeout,
    )


async def _receive_until(
    endpoint: Endpoint,
    frame_type: str,
    run_id: str | None,
) -> dict:
    async for received in endpoint:
        if (
            received.get("type") == frame_type
            and received.get("run_id") == run_id
        ):
            return received
    raise AssertionError("endpoint-closed")


@pytest.mark.core_invariant("GATE-001")
def test_turn_facade_uses_terminal_response_instead_of_provisional_chunks() -> None:
    asyncio.run(_assert_turn_facade_uses_terminal_response_instead_of_provisional_chunks())


async def _assert_turn_facade_uses_terminal_response_instead_of_provisional_chunks() -> None:
    backend = ProvisionalTextBackend()
    manager = GatewaySessionManager(
        backend_factory=lambda: backend,
        start_reaper=False,
    )
    facade = GatewayTurnFacade(manager=manager)
    delivered_chunks: list[str] = []

    async def collect_chunk(text: str, _frame: dict) -> None:
        delivered_chunks.append(text)

    try:
        result = await facade.run_turn(
            GatewayTurnRequest(
                user_id="user-sentinel",
                session_id="session-sentinel",
                text="input-sentinel",
            ),
            on_stream_chunk=collect_chunk,
        )
    finally:
        await facade.close()
        await manager.close()

    assert delivered_chunks == ["provisional-sentinel"]
    assert result.response_text == "final-sentinel"
    assert result.payload["response_text"] == "final-sentinel"


@pytest.mark.core_invariant("GATE-001")
def test_followup_queues_without_interrupting_active_run() -> None:
    asyncio.run(_assert_followup_queues_without_interrupting_active_run())


async def _assert_followup_queues_without_interrupting_active_run() -> None:
    backend = ControllableBackend()
    manager = GatewaySessionManager(
        backend_factory=lambda: backend,
        start_reaper=False,
    )
    handle = await manager.acquire(user_id="user-sentinel")
    await handle.endpoint.send(
        frame(
            type="message.user",
            user_id="user-sentinel",
            session_id="session-sentinel",
            payload={
                "text": "first-sentinel",
                "run_id": "run-a-sentinel",
                "mode": "followup",
            },
        )
    )
    await asyncio.wait_for(backend.first_started.wait(), timeout=1.0)
    await handle.endpoint.send(
        frame(
            type="message.user",
            user_id="user-sentinel",
            session_id="session-sentinel",
            payload={
                "text": "second-sentinel",
                "run_id": "run-b-sentinel",
                "mode": "followup",
            },
        )
    )

    queued = await asyncio.wait_for(
        _receive_until(handle.endpoint, "run.queued", "run-b-sentinel"),
        timeout=1.0,
    )
    assert queued["type"] == "run.queued"
    assert queued["session_id"] == "session-sentinel"
    assert queued["run_id"] == "run-b-sentinel"
    assert queued["payload"]["reason"] == "session_busy"
    assert backend.first_cancelled.is_set() is False

    backend.release_first.set()
    terminal = await asyncio.wait_for(
        _receive_until(handle.endpoint, "run.end", "run-b-sentinel"),
        timeout=1.0,
    )
    assert terminal["type"] == "run.end"
    assert terminal["run_id"] == "run-b-sentinel"
    assert backend.statuses == [
        "first-started",
        "first-completed",
        "second-started",
    ]
    assert [request.text for request in backend.requests] == [
        "first-sentinel",
        "second-sentinel",
    ]
    await manager.close()


@pytest.mark.core_invariant("GATE-001")
def test_replace_cancels_before_replacement() -> None:
    asyncio.run(_assert_replace_cancels_before_replacement())


async def _assert_replace_cancels_before_replacement() -> None:
    backend = ControllableBackend()
    manager = GatewaySessionManager(
        backend_factory=lambda: backend,
        start_reaper=False,
    )
    handle = await manager.acquire(user_id="user-sentinel")
    await handle.endpoint.send(
        frame(
            type="message.user",
            user_id="user-sentinel",
            session_id="session-sentinel",
            payload={
                "text": "first-sentinel",
                "run_id": "run-a-sentinel",
                "mode": "followup",
            },
        )
    )
    await asyncio.wait_for(backend.first_started.wait(), timeout=1.0)
    await handle.endpoint.send(
        frame(
            type="message.user",
            user_id="user-sentinel",
            session_id="session-sentinel",
            payload={
                "text": "second-sentinel",
                "run_id": "run-b-sentinel",
                "mode": "replace",
            },
        )
    )

    await asyncio.wait_for(backend.first_cancelled.wait(), timeout=1.0)
    terminal = await asyncio.wait_for(
        _receive_until(handle.endpoint, "run.end", "run-b-sentinel"),
        timeout=1.0,
    )
    assert terminal["type"] == "run.end"
    assert terminal["run_id"] == "run-b-sentinel"
    assert backend.statuses == [
        "first-started",
        "first-cancelled",
        "second-started",
    ]
    assert [request.text for request in backend.requests] == [
        "first-sentinel",
        "second-sentinel",
    ]
    await manager.close()


@pytest.mark.core_invariant("GATE-001")
def test_invalid_turn_mode_is_rejected_before_runtime() -> None:
    asyncio.run(_assert_invalid_turn_mode_is_rejected_before_runtime())


async def _assert_invalid_turn_mode_is_rejected_before_runtime() -> None:
    backend = ControllableBackend()
    manager = GatewaySessionManager(
        backend_factory=lambda: backend,
        start_reaper=False,
    )
    handle = await manager.acquire(user_id="user-sentinel")
    await handle.endpoint.send(
        frame(
            type="message.user",
            user_id="user-sentinel",
            session_id="session-sentinel",
            payload={
                "text": "first-sentinel",
                "mode": "invalid-sentinel",
            },
        )
    )

    rejected = await asyncio.wait_for(
        _receive_until(handle.endpoint, "error", None),
        timeout=1.0,
    )
    assert rejected["type"] == "error"
    assert rejected["error"]["code"] == "invalid_turn_mode"
    assert backend.requests == []
    await manager.close()


@pytest.mark.core_invariant("GATE-001")
def test_new_connection_takes_over_without_cancelling_run() -> None:
    asyncio.run(_assert_new_connection_takes_over_without_cancelling_run())


async def _assert_new_connection_takes_over_without_cancelling_run() -> None:
    runtime_bridge_ep, runtime_session_ep = InMemoryDuplex.create_pair()
    first_bridge_ep, first_client_ep = InMemoryDuplex.create_pair()
    second_bridge_ep, second_client_ep = InMemoryDuplex.create_pair()
    bridge = GatewayBridge(
        connection_policy=GatewayConnectionPolicy(detach_grace_s=0.05)
    )

    first_task = asyncio.create_task(
        bridge.bridge(
            client_id="client-a-sentinel",
            client_ep=first_bridge_ep,
            runtime_ep=runtime_bridge_ep,
            user_id="user-sentinel",
            session_id="session-sentinel",
        )
    )
    await asyncio.sleep(0)
    await runtime_session_ep.send(
        frame(
            type="run.started",
            user_id="user-sentinel",
            session_id="session-sentinel",
            run_id="run-sentinel",
            turn_id="turn-sentinel",
        )
    )
    started = await _receive(first_client_ep)
    assert started["type"] == "run.started"
    assert started["session_id"] == "session-sentinel"
    assert started["run_id"] == "run-sentinel"

    second_task = asyncio.create_task(
        bridge.bridge(
            client_id="client-b-sentinel",
            client_ep=second_bridge_ep,
            runtime_ep=runtime_bridge_ep,
            user_id="user-sentinel",
            session_id="session-sentinel",
        )
    )
    await asyncio.wait_for(first_task, timeout=1.0)

    try:
        unexpected = await _receive(runtime_session_ep, timeout=0.05)
    except asyncio.TimeoutError:
        unexpected = None
    assert unexpected is None

    await runtime_session_ep.send(
        frame(
            type="stream.chunk",
            user_id="user-sentinel",
            session_id="session-sentinel",
            run_id="run-sentinel",
            turn_id="turn-sentinel",
            payload={"text": "value-sentinel"},
        )
    )
    delivered = await _receive(second_client_ep)
    assert delivered["type"] == "stream.chunk"
    assert delivered["session_id"] == "session-sentinel"
    assert delivered["run_id"] == "run-sentinel"

    try:
        stale_delivery = await _receive(first_client_ep, timeout=0.05)
    except asyncio.TimeoutError:
        stale_delivery = None
    assert stale_delivery is None

    await second_client_ep.close()
    await asyncio.wait_for(second_task, timeout=1.0)
    try:
        early_cancel = await _receive(runtime_session_ep, timeout=0.02)
    except asyncio.TimeoutError:
        early_cancel = None
    assert early_cancel is None

    cancel = await _receive(runtime_session_ep)
    assert cancel["type"] == "run.cancel"
    assert cancel["session_id"] == "session-sentinel"
    assert cancel["run_id"] == "run-sentinel"
    await runtime_session_ep.close()


@pytest.mark.core_invariant("GATE-001")
def test_detached_connection_replays_outbox_after_cursor() -> None:
    asyncio.run(_assert_detached_connection_replays_outbox_after_cursor())


async def _assert_detached_connection_replays_outbox_after_cursor() -> None:
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
            client_id="client-a-sentinel",
            client_ep=first_bridge_ep,
            runtime_ep=runtime_bridge_ep,
            user_id="user-sentinel",
            session_id="session-sentinel",
        )
    )
    await asyncio.sleep(0)
    await runtime_session_ep.send(
        frame(
            type="run.started",
            user_id="user-sentinel",
            session_id="session-sentinel",
            run_id="run-sentinel",
            turn_id="turn-sentinel",
        )
    )
    started = await _receive(first_client_ep)
    assert started["type"] == "run.started"
    assert started["delivery_cursor"] == 1

    await first_client_ep.close()
    await asyncio.wait_for(first_task, timeout=1.0)
    await runtime_session_ep.send(
        frame(
            type="stream.chunk",
            session_id="session-sentinel",
            run_id="run-sentinel",
            turn_id="turn-sentinel",
            payload={"text": "value-sentinel"},
        )
    )

    second_bridge_ep, second_client_ep = InMemoryDuplex.create_pair()
    second_task = asyncio.create_task(
        bridge.bridge(
            client_id="client-b-sentinel",
            client_ep=second_bridge_ep,
            runtime_ep=runtime_bridge_ep,
            user_id="user-sentinel",
            session_id="session-sentinel",
        )
    )
    await asyncio.sleep(0)
    await second_client_ep.send(
        frame(
            type="session.resume",
            user_id="user-sentinel",
            session_id="session-sentinel",
            payload={"cursor": 1},
        )
    )

    replayed = await _receive(second_client_ep)
    attached = await _receive(second_client_ep)
    assert replayed["type"] == "stream.chunk"
    assert replayed["session_id"] == "session-sentinel"
    assert replayed["run_id"] == "run-sentinel"
    assert replayed["delivery_cursor"] == 2
    assert attached["type"] == "session.attached"
    assert attached["session_id"] == "session-sentinel"

    try:
        unexpected_cancel = await _receive(runtime_session_ep, timeout=0.22)
    except asyncio.TimeoutError:
        unexpected_cancel = None
    assert unexpected_cancel is None

    await second_client_ep.close()
    await asyncio.wait_for(second_task, timeout=1.0)
    await runtime_session_ep.close()


@pytest.mark.core_invariant("GATE-001")
def test_hangup_destroys_logical_session() -> None:
    asyncio.run(_assert_hangup_destroys_logical_session())


async def _assert_hangup_destroys_logical_session() -> None:
    backend = HangupBackend()
    manager = GatewaySessionManager(
        backend_factory=lambda: backend,
        start_reaper=False,
    )
    bridge = GatewayBridge(session_manager=manager)
    bridge_ep, client_ep = InMemoryDuplex.create_pair()
    bridge_task = asyncio.create_task(
        bridge.bridge(
            client_id="client-sentinel",
            client_ep=bridge_ep,
            user_id="user-sentinel",
        )
    )

    await client_ep.send(
        frame(
            type=CALL_INCOMING,
            user_id="user-sentinel",
            session_id="session-a-sentinel",
            payload={"session_id": "session-a-sentinel"},
        )
    )
    ready = await _receive(client_ep)
    assert ready["type"] == "call.ready"
    assert ready["session_id"] == "session-a-sentinel"
    assert manager.active_count() == 1

    await client_ep.send(
        frame(
            type="message.user",
            user_id="user-sentinel",
            session_id="session-a-sentinel",
            payload={
                "text": "first-sentinel",
                "run_id": "run-sentinel",
            },
        )
    )
    await asyncio.wait_for(backend.started.wait(), timeout=1.0)
    started = await _receive(client_ep)
    assert started["type"] == "run.started"
    assert started["run_id"] == "run-sentinel"

    await client_ep.send(
        frame(
            type=CALL_HANGUP,
            user_id="user-sentinel",
            session_id="session-a-sentinel",
        )
    )
    ack = await _receive(client_ep)
    assert ack["type"] == "call.hangup_ack"
    assert ack["session_id"] == "session-a-sentinel"
    await asyncio.wait_for(backend.cancelled.wait(), timeout=1.0)
    for _ in range(20):
        if manager.active_count() == 0:
            break
        await asyncio.sleep(0.01)
    assert manager.active_count() == 0

    await client_ep.send(
        frame(
            type=CALL_INCOMING,
            user_id="user-sentinel",
            session_id="session-b-sentinel",
            payload={"session_id": "session-b-sentinel"},
        )
    )
    next_ready = await _receive(client_ep)
    assert next_ready["type"] == "call.ready"
    assert next_ready["session_id"] == "session-b-sentinel"
    assert manager.active_count() == 1

    await client_ep.close()
    await asyncio.wait_for(bridge_task, timeout=1.0)
    await manager.close()
