"""Explicit Gateway followup/replace turn lifecycle contract."""

from __future__ import annotations

import asyncio

from assistant_agent.gateway import GatewaySessionManager, frame
from assistant_agent.realtime import RealtimeAgentResult


class _ControllableBackend:
    def __init__(self) -> None:
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.first_cancelled = asyncio.Event()
        self.requests = []

    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        self.requests.append(request)
        if request.text == "first":
            self.first_started.set()
            while not self.release_first.is_set() and not cancel_token.is_cancelled():
                await asyncio.sleep(0.001)
            if cancel_token.is_cancelled():
                self.first_cancelled.set()
                return RealtimeAgentResult(
                    status="cancelled",
                    run_id=request.run_id,
                )
        return RealtimeAgentResult(
            status="completed",
            run_id=request.run_id,
            response_text=request.text,
        )


async def _receive_until(endpoint, frame_type: str, run_id: str) -> dict:
    async for received in endpoint:
        if received.get("type") == frame_type and received.get("run_id") == run_id:
            return received
    raise AssertionError("Gateway endpoint closed before expected frame")


def test_explicit_followup_does_not_interrupt_active_turn() -> None:
    asyncio.run(_assert_explicit_followup_does_not_interrupt_active_turn())


async def _assert_explicit_followup_does_not_interrupt_active_turn() -> None:
    backend = _ControllableBackend()
    manager = GatewaySessionManager(
        backend_factory=lambda: backend,
        start_reaper=False,
    )
    handle = await manager.acquire(
        user_id="user-1",
        config={"interrupt_policy": "interrupt"},
    )
    await handle.endpoint.send(
        frame(
            type="message.user",
            user_id="user-1",
            session_id="session-1",
            payload={"text": "first", "run_id": "run-1", "mode": "followup"},
        )
    )
    await asyncio.wait_for(backend.first_started.wait(), timeout=1.0)
    await handle.endpoint.send(
        frame(
            type="message.user",
            user_id="user-1",
            session_id="session-1",
            payload={"text": "second", "run_id": "run-2", "mode": "followup"},
        )
    )
    queued = await asyncio.wait_for(
        _receive_until(handle.endpoint, "run.queued", "run-2"),
        timeout=1.0,
    )
    assert queued["payload"]["reason"] == "session_busy"
    assert not backend.first_cancelled.is_set()

    backend.release_first.set()
    await asyncio.wait_for(
        _receive_until(handle.endpoint, "run.end", "run-2"),
        timeout=1.0,
    )
    assert [request.text for request in backend.requests] == ["first", "second"]
    assert backend.requests[1].metadata["turn_mode"] == "followup"
    await manager.close()


def test_explicit_replace_cancels_active_turn_before_running_replacement() -> None:
    asyncio.run(_assert_explicit_replace_cancels_active_turn())


async def _assert_explicit_replace_cancels_active_turn() -> None:
    backend = _ControllableBackend()
    manager = GatewaySessionManager(
        backend_factory=lambda: backend,
        start_reaper=False,
    )
    handle = await manager.acquire(user_id="user-1")
    await handle.endpoint.send(
        frame(
            type="message.user",
            user_id="user-1",
            session_id="session-1",
            payload={"text": "first", "run_id": "run-1", "mode": "followup"},
        )
    )
    await asyncio.wait_for(backend.first_started.wait(), timeout=1.0)
    await handle.endpoint.send(
        frame(
            type="message.user",
            user_id="user-1",
            session_id="session-1",
            payload={"text": "replacement", "run_id": "run-2", "mode": "replace"},
        )
    )

    await asyncio.wait_for(backend.first_cancelled.wait(), timeout=1.0)
    await asyncio.wait_for(
        _receive_until(handle.endpoint, "run.end", "run-2"),
        timeout=1.0,
    )
    assert [request.text for request in backend.requests] == ["first", "replacement"]
    assert backend.requests[1].metadata["turn_mode"] == "replace"
    await manager.close()


def test_invalid_explicit_turn_mode_is_rejected_before_runtime() -> None:
    asyncio.run(_assert_invalid_explicit_turn_mode_is_rejected())


async def _assert_invalid_explicit_turn_mode_is_rejected() -> None:
    backend = _ControllableBackend()
    manager = GatewaySessionManager(
        backend_factory=lambda: backend,
        start_reaper=False,
    )
    handle = await manager.acquire(user_id="user-1")
    await handle.endpoint.send(
        frame(
            type="message.user",
            user_id="user-1",
            session_id="session-1",
            payload={"text": "invalid", "mode": "guess"},
        )
    )
    rejected = await asyncio.wait_for(
        _receive_until(handle.endpoint, "error", None),
        timeout=1.0,
    )
    assert rejected["error"]["code"] == "invalid_turn_mode"
    assert backend.requests == []
    await manager.close()
