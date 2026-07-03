from __future__ import annotations

import asyncio
import unittest

from assistant_agent.gateway import (
    CALL_HANGUP,
    CALL_HANGUP_ACK,
    CALL_INCOMING,
    CALL_READY,
    GatewayBridge,
    GatewaySessionManager,
    InMemoryDuplex,
    frame,
)
from assistant_agent.realtime import RealtimeAgentEvent, RealtimeAgentResult


async def _close_bridge(client_ep, bridge_ep, bridge_task) -> None:
    await client_ep.close()
    await bridge_ep.close()
    bridge_task.cancel()
    await asyncio.gather(bridge_task, return_exceptions=True)


async def _read_until(client_ep, frame_type: str, *, timeout_s: float = 3.0):
    async def _read():
        async for received in client_ep:
            if received["type"] == frame_type:
                return received
        raise AssertionError(f"endpoint closed before {frame_type}")

    return await asyncio.wait_for(_read(), timeout=timeout_s)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_incoming_creates_managed_session_and_ready(self) -> None:
        class RecordingBackend:
            def __init__(self) -> None:
                self.requests = []

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                self.requests.append(request)
                assert event_sink is not None
                await event_sink(RealtimeAgentEvent(type="response.chunk", text="managed response"))
                return RealtimeAgentResult(status="completed", response_text="managed response")

        backend = RecordingBackend()
        manager = GatewaySessionManager(
            backend_factory=lambda: backend,
            start_reaper=False,
        )
        bridge = GatewayBridge(session_manager=manager)
        client_ep, bridge_ep = InMemoryDuplex.create_pair()
        bridge_task = asyncio.create_task(
            bridge.bridge(client_id="client-1", client_ep=bridge_ep)
        )

        try:
            await client_ep.send(
                frame(
                    type=CALL_INCOMING,
                    user_id="u1",
                    session_id="s1",
                    payload={"config": {"tone": "concise"}},
                )
            )
            ready = await _read_until(client_ep, CALL_READY)
            assert ready["user_id"] == "u1"
            assert ready["session_id"] == "s1"
            assert ready["payload"]["session_managed"] is True

            await client_ep.send(
                frame(
                    type="message.user",
                    user_id="u1",
                    session_id="s1",
                    payload={"text": "hello gateway"},
                )
            )
            run_end = await _read_until(client_ep, "run.end")
        finally:
            await _close_bridge(client_ep, bridge_ep, bridge_task)
            await manager.destroy("u1")

        assert run_end["reason"] == "completed"
        assert len(backend.requests) == 1
        assert backend.requests[0].text == "hello gateway"
        assert backend.requests[0].metadata["gateway"]["history"] == ["hello gateway"]
        assert backend.requests[0].metadata["gateway"]["session_config"] == {"tone": "concise"}

    async def test_call_hangup_marks_session_for_grace_reuse(self) -> None:
        manager = GatewaySessionManager(start_reaper=False)
        first = await manager.acquire(user_id="u1")

        assert await manager.mark_hangup("u1") is True
        second = await manager.acquire(user_id="u1")

        await manager.destroy("u1")
        assert second.endpoint is first.endpoint
        assert second.created is False
        assert second.resumed is True
        assert manager.active_count() == 0

    async def test_idle_and_hangup_reap_destroy_sessions(self) -> None:
        manager = GatewaySessionManager(
            idle_timeout_s=0,
            hangup_grace_s=0,
            start_reaper=False,
        )
        await manager.acquire(user_id="idle-user")

        evicted = await manager.reap_once()

        assert evicted == ["idle-user"]
        assert manager.active_count() == 0

    async def test_config_update_online_and_deferred(self) -> None:
        manager = GatewaySessionManager(start_reaper=False)

        deferred = await manager.update_config("u1", {"language": "zh-CN"})
        acquired = await manager.acquire(user_id="u1", config={"tone": "direct"})
        online = await manager.update_config("u1", {"tone": "warm"})

        await manager.destroy("u1")
        assert deferred.online is False
        assert acquired.config == {"language": "zh-CN", "tone": "direct"}
        assert online.online is True
        assert online.config == {"language": "zh-CN", "tone": "warm"}

    async def test_bridge_call_hangup_sends_ack(self) -> None:
        manager = GatewaySessionManager(start_reaper=False)
        bridge = GatewayBridge(session_manager=manager)
        client_ep, bridge_ep = InMemoryDuplex.create_pair()
        bridge_task = asyncio.create_task(
            bridge.bridge(client_id="client-1", client_ep=bridge_ep)
        )

        try:
            await manager.acquire(user_id="u1")
            await client_ep.send(frame(type=CALL_HANGUP, user_id="u1"))
            ack = await _read_until(client_ep, CALL_HANGUP_ACK)
        finally:
            await _close_bridge(client_ep, bridge_ep, bridge_task)
            await manager.destroy("u1")

        assert ack["user_id"] == "u1"

    async def test_bridge_call_hangup_cancels_active_run(self) -> None:
        class CancellableBackend:
            def __init__(self) -> None:
                self.cancel_metadata = None
                self.cancel_seen = asyncio.Event()

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                await cancel_token.cancelled()
                self.cancel_metadata = cancel_token.cancel_metadata
                self.cancel_seen.set()
                return RealtimeAgentResult(status="cancelled", run_id=request.run_id)

        backend = CancellableBackend()
        manager = GatewaySessionManager(
            backend_factory=lambda: backend,
            start_reaper=False,
        )
        bridge = GatewayBridge(session_manager=manager)
        client_ep, bridge_ep = InMemoryDuplex.create_pair()
        bridge_task = asyncio.create_task(
            bridge.bridge(client_id="client-1", client_ep=bridge_ep)
        )

        try:
            await client_ep.send(
                frame(
                    type=CALL_INCOMING,
                    user_id="u1",
                    session_id="s1",
                    payload={"config": {"locale": "zh-CN"}},
                )
            )
            ready = await _read_until(client_ep, CALL_READY)
            await client_ep.send(
                frame(
                    type="message.user",
                    user_id="u1",
                    session_id="s1",
                    payload={"text": "long running"},
                )
            )
            started = await _read_until(client_ep, "run.started")

            await client_ep.send(frame(type=CALL_HANGUP, user_id="u1", session_id="s1"))
            ack = await _read_until(client_ep, CALL_HANGUP_ACK)
            run_end = await _read_until(client_ep, "run.end")
        finally:
            await _close_bridge(client_ep, bridge_ep, bridge_task)
            await manager.destroy("u1")

        assert ready["type"] == CALL_READY
        assert started["type"] == "run.started"
        assert ack["session_id"] == "s1"
        assert ack["payload"]["cancelled_active_run"] is True
        assert run_end["reason"] == "cancelled"
        await asyncio.wait_for(backend.cancel_seen.wait(), timeout=2.0)
        assert backend.cancel_metadata == {
            "cancel_source": "gateway_hangup",
            "cancel_reason": "call_hangup",
        }


if __name__ == "__main__":
    unittest.main()
