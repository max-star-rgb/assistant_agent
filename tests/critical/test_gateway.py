from __future__ import annotations

import asyncio
import unittest

from assistant_agent.gateway import (
    CALL_HANGUP,
    CALL_HANGUP_ACK,
    CALL_INCOMING,
    CALL_READY,
    GatewayBridge,
    GatewayQueuePolicy,
    GatewaySessionManager,
    GatewaySessionService,
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
    async def test_client_disconnect_uses_disconnect_cancel_metadata(self) -> None:
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
            bridge.bridge(client_id="disconnect-client", client_ep=bridge_ep)
        )

        try:
            await client_ep.send(
                frame(type=CALL_INCOMING, user_id="disconnect-user", session_id="disconnect-s")
            )
            await _read_until(client_ep, CALL_READY)
            await client_ep.send(
                frame(
                    type="message.user",
                    user_id="disconnect-user",
                    session_id="disconnect-s",
                    payload={"text": "wait"},
                )
            )
            await _read_until(client_ep, "run.started")

            await client_ep.close()
            await asyncio.wait_for(bridge_task, timeout=1.0)
            await asyncio.wait_for(backend.cancel_seen.wait(), timeout=1.0)

            assert backend.cancel_metadata["cancel_source"] == "gateway_disconnect"
            assert backend.cancel_metadata["cancel_reason"] == "client_disconnected"
        finally:
            await bridge_ep.close()
            if not bridge_task.done():
                bridge_task.cancel()
                await asyncio.gather(bridge_task, return_exceptions=True)
            await manager.close()

    async def test_reconnect_after_disconnect_cancel_can_start_new_run(self) -> None:
        class ReconnectBackend:
            def __init__(self) -> None:
                self.requests = []
                self.first_started = asyncio.Event()
                self.cancel_seen = asyncio.Event()

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                self.requests.append(request)
                if request.text == "first waits":
                    self.first_started.set()
                    await cancel_token.cancelled()
                    self.cancel_seen.set()
                    return RealtimeAgentResult(status="cancelled", run_id=request.run_id)
                return RealtimeAgentResult(
                    status="completed",
                    run_id=request.run_id,
                    response_text="second completed",
                )

        backend = ReconnectBackend()
        manager = GatewaySessionManager(
            backend_factory=lambda: backend,
            start_reaper=False,
        )
        bridge = GatewayBridge(session_manager=manager)
        first_client_ep, first_bridge_ep = InMemoryDuplex.create_pair()
        first_task = asyncio.create_task(
            bridge.bridge(client_id="reconnect-first", client_ep=first_bridge_ep)
        )
        second_client_ep, second_bridge_ep = InMemoryDuplex.create_pair()
        second_task: asyncio.Task | None = None

        try:
            await first_client_ep.send(
                frame(type=CALL_INCOMING, user_id="reconnect-user", session_id="reconnect-session")
            )
            await _read_until(first_client_ep, CALL_READY)
            await first_client_ep.send(
                frame(
                    type="message.user",
                    user_id="reconnect-user",
                    session_id="reconnect-session",
                    payload={"text": "first waits", "run_id": "run-before-disconnect"},
                )
            )
            first_started = await _read_until(first_client_ep, "run.started")
            await asyncio.wait_for(backend.first_started.wait(), timeout=1.0)

            await first_client_ep.close()
            await asyncio.wait_for(first_task, timeout=1.0)
            await asyncio.wait_for(backend.cancel_seen.wait(), timeout=1.0)

            second_task = asyncio.create_task(
                bridge.bridge(client_id="reconnect-second", client_ep=second_bridge_ep)
            )
            await second_client_ep.send(
                frame(type=CALL_INCOMING, user_id="reconnect-user", session_id="reconnect-session")
            )
            ready = await _read_until(second_client_ep, CALL_READY)
            await second_client_ep.send(
                frame(
                    type="message.user",
                    user_id="reconnect-user",
                    session_id="reconnect-session",
                    payload={"text": "second starts", "run_id": "run-after-reconnect"},
                )
            )
            second_started = await _read_until(second_client_ep, "run.started")
            second_end = await _read_until(second_client_ep, "run.end")

            assert ready["session_id"] == "reconnect-session"
            assert ready["payload"]["session_managed"] is True
            assert first_started["run_id"] == "run-before-disconnect"
            assert second_started["run_id"] == "run-after-reconnect"
            assert second_started["run_id"] != first_started["run_id"]
            assert second_end["run_id"] == "run-after-reconnect"
            assert second_end["reason"] == "completed"
            assert [request.text for request in backend.requests] == [
                "first waits",
                "second starts",
            ]
        finally:
            await first_bridge_ep.close()
            if not first_task.done():
                first_task.cancel()
                await asyncio.gather(first_task, return_exceptions=True)
            if second_task is not None:
                await _close_bridge(second_client_ep, second_bridge_ep, second_task)
            else:
                await second_client_ep.close()
                await second_bridge_ep.close()
            await manager.close()

    async def test_new_same_user_bridge_evicts_idle_stale_connection(self) -> None:
        manager = GatewaySessionManager(start_reaper=False)
        bridge = GatewayBridge(session_manager=manager)
        stale_client_ep, stale_bridge_ep = InMemoryDuplex.create_pair()
        current_client_ep, current_bridge_ep = InMemoryDuplex.create_pair()
        stale_task = asyncio.create_task(
            bridge.bridge(
                client_id="same-user-stale",
                client_ep=stale_bridge_ep,
                user_id="same-user",
                session_id="same-session",
            )
        )
        current_task = asyncio.create_task(
            bridge.bridge(
                client_id="same-user-current",
                client_ep=current_bridge_ep,
                user_id="same-user",
                session_id="same-session",
            )
        )

        try:
            await asyncio.wait_for(stale_task, timeout=1.0)

            await stale_client_ep.send(
                frame(type=CALL_INCOMING, user_id="same-user", session_id="same-session")
            )
            await current_client_ep.send(
                frame(type=CALL_INCOMING, user_id="same-user", session_id="same-session")
            )

            ready = await _read_until(current_client_ep, CALL_READY)
            assert ready["user_id"] == "same-user"
            assert ready["session_id"] == "same-session"

            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(_read_until(stale_client_ep, CALL_READY), timeout=0.05)
        finally:
            await _close_bridge(current_client_ep, current_bridge_ep, current_task)
            await stale_client_ep.close()
            await stale_bridge_ep.close()
            if not stale_task.done():
                stale_task.cancel()
                await asyncio.gather(stale_task, return_exceptions=True)
            await manager.close()

    async def test_same_user_bridge_handoff_preserves_active_run_for_new_owner(self) -> None:
        class HandoffBackend:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.cancel_seen = asyncio.Event()

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                self.started.set()
                await self.release.wait()
                if cancel_token.is_cancelled():
                    self.cancel_seen.set()
                    return RealtimeAgentResult(status="cancelled", run_id=request.run_id)
                assert event_sink is not None
                await event_sink(RealtimeAgentEvent(type="response.chunk", text="handoff response"))
                return RealtimeAgentResult(
                    status="completed",
                    run_id=request.run_id,
                    response_text="handoff response",
                )

        backend = HandoffBackend()
        manager = GatewaySessionManager(
            backend_factory=lambda: backend,
            start_reaper=False,
        )
        bridge = GatewayBridge(session_manager=manager)
        stale_client_ep, stale_bridge_ep = InMemoryDuplex.create_pair()
        current_client_ep, current_bridge_ep = InMemoryDuplex.create_pair()
        stale_task = asyncio.create_task(
            bridge.bridge(
                client_id="active-stale",
                client_ep=stale_bridge_ep,
                user_id="handoff-user",
                session_id="handoff-session",
            )
        )
        current_task: asyncio.Task | None = None

        try:
            await stale_client_ep.send(
                frame(type=CALL_INCOMING, user_id="handoff-user", session_id="handoff-session")
            )
            await _read_until(stale_client_ep, CALL_READY)
            await stale_client_ep.send(
                frame(
                    type="message.user",
                    user_id="handoff-user",
                    session_id="handoff-session",
                    payload={"text": "wait for handoff"},
                )
            )
            await _read_until(stale_client_ep, "run.started")
            await asyncio.wait_for(backend.started.wait(), timeout=1.0)

            current_task = asyncio.create_task(
                bridge.bridge(
                    client_id="active-current",
                    client_ep=current_bridge_ep,
                    user_id="handoff-user",
                    session_id="handoff-session",
                )
            )
            await asyncio.wait_for(stale_task, timeout=1.0)
            await current_client_ep.send(
                frame(type=CALL_INCOMING, user_id="handoff-user", session_id="handoff-session")
            )
            await _read_until(current_client_ep, CALL_READY)

            backend.release.set()
            chunk = await _read_until(current_client_ep, "stream.chunk")
            run_end = await _read_until(current_client_ep, "run.end")

            assert chunk["payload"]["text"] == "handoff response"
            assert run_end["reason"] == "completed"
            assert not backend.cancel_seen.is_set()
        finally:
            backend.release.set()
            if current_task is not None:
                await _close_bridge(current_client_ep, current_bridge_ep, current_task)
            else:
                await current_client_ep.close()
                await current_bridge_ep.close()
            await stale_client_ep.close()
            await stale_bridge_ep.close()
            if not stale_task.done():
                stale_task.cancel()
                await asyncio.gather(stale_task, return_exceptions=True)
            await manager.close()

    async def test_destroy_cancels_active_and_releases_all_queue_capacity(self) -> None:
        class CancellableBackend:
            def __init__(self) -> None:
                self.cancel_seen = asyncio.Event()
                self.force_release = asyncio.Event()

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                while not cancel_token.is_cancelled() and not self.force_release.is_set():
                    await asyncio.sleep(0)
                if cancel_token.is_cancelled():
                    self.cancel_seen.set()
                    return RealtimeAgentResult(status="cancelled", run_id=request.run_id)
                return RealtimeAgentResult(status="completed", run_id=request.run_id)

        backend = CancellableBackend()
        manager = GatewaySessionManager(
            backend_factory=lambda: backend,
            queue_policy=GatewayQueuePolicy(
                max_active_runs=1,
                max_queued_turns_global=4,
            ),
            start_reaper=False,
        )
        handle = await manager.acquire(user_id="destroy-user")

        try:
            await handle.endpoint.send(
                frame(
                    type="message.user",
                    session_id="destroy-session",
                    payload={"text": "first", "turn_id": "t1", "run_id": "r1"},
                )
            )
            await _read_until(handle.endpoint, "run.started")
            await handle.endpoint.send(
                frame(
                    type="message.user",
                    session_id="destroy-session",
                    payload={"text": "second", "turn_id": "t2", "run_id": "r2"},
                )
            )
            await _read_until(handle.endpoint, "run.queued")

            before = await manager.admission_controller.snapshot()
            assert before.active_runs == 1
            assert before.queued_turns == 1

            assert await manager.destroy("destroy-user") is True

            await asyncio.wait_for(backend.cancel_seen.wait(), timeout=1.0)
            after = await manager.admission_controller.snapshot()
            assert after.active_runs == 0
            assert after.queued_turns == 0
        finally:
            backend.force_release.set()
            await manager.close()

    async def test_hangup_cancels_global_waiter_without_active_run_id(self) -> None:
        class HoldingBackend:
            def __init__(self) -> None:
                self.release = asyncio.Event()

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                await self.release.wait()
                return RealtimeAgentResult(status="completed", run_id=request.run_id)

        backend = HoldingBackend()
        manager = GatewaySessionManager(
            backend_factory=lambda: backend,
            queue_policy=GatewayQueuePolicy(
                max_active_runs=1,
                max_queued_turns_global=4,
            ),
            start_reaper=False,
        )
        holder = await manager.acquire(user_id="holder")
        await holder.endpoint.send(
            frame(type="message.user", session_id="holder-s", payload={"text": "hold"})
        )
        await _read_until(holder.endpoint, "run.started")

        bridge = GatewayBridge(session_manager=manager)
        client_ep, bridge_ep = InMemoryDuplex.create_pair()
        bridge_task = asyncio.create_task(
            bridge.bridge(client_id="waiting-client", client_ep=bridge_ep)
        )

        try:
            await client_ep.send(
                frame(type=CALL_INCOMING, user_id="waiting-user", session_id="waiting-s")
            )
            await _read_until(client_ep, CALL_READY)
            await client_ep.send(
                frame(
                    type="message.user",
                    user_id="waiting-user",
                    session_id="waiting-s",
                    payload={"text": "queued", "turn_id": "t2", "run_id": "r2"},
                )
            )
            queued = await _read_until(client_ep, "run.queued")
            assert queued["payload"]["reason"] == "global_capacity"

            await client_ep.send(
                frame(type=CALL_HANGUP, user_id="waiting-user", session_id="waiting-s")
            )

            async def _read_hangup_and_terminal():
                ack = None
                terminal = None
                async for received in client_ep:
                    if received["type"] == CALL_HANGUP_ACK:
                        ack = received
                    elif received["type"] == "run.end" and received.get("run_id") == "r2":
                        terminal = received
                    if ack is not None and terminal is not None:
                        return ack, terminal
                raise AssertionError("endpoint closed before hangup cleanup completed")

            ack, terminal = await asyncio.wait_for(
                _read_hangup_and_terminal(),
                timeout=1.0,
            )
            assert ack["payload"]["cancelled_active_run"] is False
            assert terminal["reason"] == "cancelled"
            assert terminal["payload"]["cancel"]["phase"] == "before_llm"
        finally:
            backend.release.set()
            await _close_bridge(client_ep, bridge_ep, bridge_task)
            await manager.close()

    async def test_custom_service_factory_uses_manager_admission_controller(self) -> None:
        class BlockingBackend:
            def __init__(self) -> None:
                self.release = asyncio.Event()
                self.active = 0
                self.max_seen = 0

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                self.active += 1
                self.max_seen = max(self.max_seen, self.active)
                try:
                    await self.release.wait()
                    return RealtimeAgentResult(status="completed", run_id=request.run_id)
                finally:
                    self.active -= 1

        backend = BlockingBackend()

        def service_factory(user_id, config):
            return GatewaySessionService(
                user_id=user_id,
                backend=backend,
                config=config,
            )

        manager = GatewaySessionManager(
            service_factory=service_factory,
            queue_policy=GatewayQueuePolicy(
                max_active_runs=1,
                max_queued_turns_global=4,
            ),
            start_reaper=False,
        )
        first = await manager.acquire(user_id="custom-u1")
        second = await manager.acquire(user_id="custom-u2")

        try:
            await first.endpoint.send(
                frame(type="message.user", session_id="custom-s1", payload={"text": "one"})
            )
            await _read_until(first.endpoint, "run.started")
            await second.endpoint.send(
                frame(type="message.user", session_id="custom-s2", payload={"text": "two"})
            )
            queued = await _read_until(second.endpoint, "run.queued", timeout_s=0.2)

            assert queued["payload"]["reason"] == "global_capacity"
            assert backend.max_seen == 1
        finally:
            backend.release.set()
            await manager.close()

    async def test_manager_shares_one_admission_controller_across_users(self) -> None:
        class BlockingBackend:
            def __init__(self) -> None:
                self.release = asyncio.Event()
                self.active = 0
                self.max_seen = 0

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                self.active += 1
                self.max_seen = max(self.max_seen, self.active)
                try:
                    await self.release.wait()
                    return RealtimeAgentResult(status="completed", run_id=request.run_id)
                finally:
                    self.active -= 1

        backend = BlockingBackend()
        manager = GatewaySessionManager(
            backend_factory=lambda: backend,
            queue_policy=GatewayQueuePolicy(
                max_active_runs=1,
                max_queued_turns_global=4,
            ),
            start_reaper=False,
        )
        first = await manager.acquire(user_id="u1")
        second = await manager.acquire(user_id="u2")

        try:
            await first.endpoint.send(
                frame(type="message.user", session_id="s1", payload={"text": "one"})
            )
            first_started = await _read_until(first.endpoint, "run.started")
            await second.endpoint.send(
                frame(type="message.user", session_id="s2", payload={"text": "two"})
            )
            second_queued = await _read_until(second.endpoint, "run.queued")

            assert first_started["session_id"] == "s1"
            assert second_queued["payload"]["reason"] == "global_capacity"
            assert backend.max_seen == 1

            backend.release.set()
            second_started = await _read_until(second.endpoint, "run.started")
            second_end = await _read_until(second.endpoint, "run.end")
            assert second_started["session_id"] == "s2"
            assert second_end["reason"] == "completed"
            assert backend.max_seen == 1
        finally:
            backend.release.set()
            await manager.close()

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
        assert backend.cancel_metadata["cancel_source"] == "gateway_hangup"
        assert backend.cancel_metadata["cancel_reason"] == "call_hangup"
        assert backend.cancel_metadata["realtime_turn_cancellation"] == {
            "cancelled_by": "hangup",
            "phase": "final_streaming",
            "stale_outputs": True,
            "can_reuse_tool_result": False,
            "speakable": False,
        }
        assert run_end["payload"]["cancel"]["cancelled_by"] == "hangup"
        assert run_end["payload"]["cancel"]["speakable"] is False


if __name__ == "__main__":
    unittest.main()
