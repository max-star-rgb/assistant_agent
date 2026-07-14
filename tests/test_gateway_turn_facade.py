from __future__ import annotations

import asyncio
import unittest

import pytest

from assistant_agent.gateway import GatewayQueuePolicy, GatewaySessionManager
from assistant_agent.realtime import RealtimeAgentEvent, RealtimeAgentResult
from assistant_agent.services.gateway_turn_facade import (
    GatewayTurnFacade,
    GatewayTurnError,
    GatewayTurnRequest,
    GatewayTurnTimeout,
)


class RecordingRealtimeBackend:
    def __init__(self) -> None:
        self.requests = []

    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        self.requests.append(request)
        assert event_sink is not None
        await event_sink(RealtimeAgentEvent(type="response.chunk", text="hello via gateway"))
        return RealtimeAgentResult(
            status="completed",
            run_id=request.run_id,
            trace_id="trace-turn-1",
            response_text="hello via gateway",
            expects_reply=True,
        )


async def _append_async(items: list[str], value: str) -> None:
    items.append(value)


class GatewayTurnFacadeTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_stops_dispatcher_reader(self) -> None:
        backend = RecordingRealtimeBackend()
        manager = GatewaySessionManager(
            backend_factory=lambda: backend,
            start_reaper=False,
        )
        facade = GatewayTurnFacade(manager=manager)

        try:
            await facade.run_turn(
                GatewayTurnRequest(
                    user_id="user-1",
                    session_id="session-1",
                    text="hello",
                    timeout_s=1,
                )
            )
            readers = [dispatcher._reader for dispatcher in facade._dispatchers.values()]

            await facade.close()

            assert readers
            assert all(reader.done() for reader in readers)
        finally:
            await manager.close()

    async def test_queue_rejection_raises_gateway_error_without_timeout(self) -> None:
        class BlockingBackend:
            def __init__(self) -> None:
                self.first_started = asyncio.Event()
                self.release_first = asyncio.Event()

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                if request.text == "first":
                    self.first_started.set()
                    await self.release_first.wait()
                return RealtimeAgentResult(status="completed", run_id=request.run_id)

        backend = BlockingBackend()
        manager = GatewaySessionManager(
            backend_factory=lambda: backend,
            queue_policy=GatewayQueuePolicy(max_pending_per_session=1),
            start_reaper=False,
        )
        facade = GatewayTurnFacade(manager=manager)

        try:
            first = asyncio.create_task(
                facade.run_turn(
                    GatewayTurnRequest(user_id="u1", session_id="s1", text="first")
                )
            )
            await asyncio.wait_for(backend.first_started.wait(), timeout=1.0)
            second = asyncio.create_task(
                facade.run_turn(
                    GatewayTurnRequest(user_id="u1", session_id="s1", text="second")
                )
            )

            async def _wait_for_pending() -> None:
                while (await manager.admission_controller.snapshot()).queued_turns == 0:
                    await asyncio.sleep(0)

            await asyncio.wait_for(_wait_for_pending(), timeout=1.0)
            with pytest.raises(GatewayTurnError, match="queue_overflow") as raised:
                await facade.run_turn(
                    GatewayTurnRequest(
                        user_id="u1",
                        session_id="s1",
                        text="third",
                        timeout_s=0.2,
                    )
                )
            assert not isinstance(raised.value, GatewayTurnTimeout)

            backend.release_first.set()
            await asyncio.gather(first, second)
        finally:
            backend.release_first.set()
            await manager.close()

    async def test_timeout_sends_run_cancel_and_releases_backend(self) -> None:
        class CancellableBackend:
            def __init__(self) -> None:
                self.cancelled = asyncio.Event()

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                await cancel_token.cancelled()
                self.cancelled.set()
                return RealtimeAgentResult(status="cancelled", run_id=request.run_id)

        backend = CancellableBackend()
        manager = GatewaySessionManager(
            backend_factory=lambda: backend,
            start_reaper=False,
        )
        facade = GatewayTurnFacade(manager=manager)

        try:
            with pytest.raises(GatewayTurnTimeout):
                await facade.run_turn(
                    GatewayTurnRequest(
                        user_id="u1",
                        session_id="s1",
                        text="wait",
                        timeout_s=0.02,
                    )
                )
            await asyncio.wait_for(backend.cancelled.wait(), timeout=1.0)

            async def _wait_for_release() -> None:
                while (await manager.admission_controller.snapshot()).active_runs:
                    await asyncio.sleep(0)

            await asyncio.wait_for(_wait_for_release(), timeout=1.0)
        finally:
            await manager.close()

    async def test_concurrent_same_user_turns_receive_their_own_frames(self) -> None:
        class OrderedBackend:
            def __init__(self) -> None:
                self.first_started = asyncio.Event()
                self.release_first = asyncio.Event()

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                assert event_sink is not None
                if request.text == "first":
                    self.first_started.set()
                    await self.release_first.wait()
                await event_sink(
                    RealtimeAgentEvent(
                        type="response.chunk",
                        text=f"done:{request.text}",
                    )
                )
                return RealtimeAgentResult(status="completed", run_id=request.run_id)

        backend = OrderedBackend()
        manager = GatewaySessionManager(
            backend_factory=lambda: backend,
            start_reaper=False,
        )
        facade = GatewayTurnFacade(manager=manager)

        try:
            first = asyncio.create_task(
                facade.run_turn(
                    GatewayTurnRequest(
                        user_id="u1",
                        session_id="s1",
                        text="first",
                        timeout_s=1.0,
                    )
                )
            )
            await asyncio.wait_for(backend.first_started.wait(), timeout=1.0)
            second = asyncio.create_task(
                facade.run_turn(
                    GatewayTurnRequest(
                        user_id="u1",
                        session_id="s1",
                        text="second",
                        timeout_s=1.0,
                    )
                )
            )

            async def _wait_for_queued_reservation() -> None:
                while (await manager.admission_controller.snapshot()).queued_turns == 0:
                    await asyncio.sleep(0)

            await asyncio.wait_for(_wait_for_queued_reservation(), timeout=1.0)
            backend.release_first.set()
            first_result, second_result = await asyncio.gather(first, second)

            assert first_result.response_text == "done:first"
            assert second_result.response_text == "done:second"
            assert "run.queued" in [item["type"] for item in second_result.frames]
            assert first_result.run_id != second_result.run_id
        finally:
            backend.release_first.set()
            await manager.close()

    async def test_run_turn_collects_gateway_frames_and_backend_request(self) -> None:
        backend = RecordingRealtimeBackend()
        manager = GatewaySessionManager(backend_factory=lambda: backend, start_reaper=False)
        facade = GatewayTurnFacade(manager=manager)

        try:
            result = await facade.run_turn(
                GatewayTurnRequest(
                    user_id="user-1",
                    session_id="session-1",
                    text="hello",
                    metadata={"source": "http_gateway_turn"},
                    config={"tone": "concise"},
                    timeout_s=1,
                )
            )
        finally:
            await manager.close()

        assert [frame["type"] for frame in result.frames] == [
            "run.started",
            "stream.chunk",
            "run.end",
        ]
        assert result.status == "completed"
        assert result.reason == "completed"
        assert result.response_text == "hello via gateway"
        assert result.trace_id == "trace-turn-1"
        assert backend.requests[0].text == "hello"
        assert backend.requests[0].metadata["gateway"]["history"] == ["hello"]
        assert backend.requests[0].metadata["gateway"]["session_config"] == {"tone": "concise"}

    async def test_run_turn_calls_stream_chunk_callback_before_returning(self) -> None:
        class StreamingBackend:
            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                assert event_sink is not None
                await event_sink(RealtimeAgentEvent(type="response.chunk", text="你"))
                await event_sink(RealtimeAgentEvent(type="response.chunk", text="好"))
                return RealtimeAgentResult(
                    status="completed",
                    run_id=request.run_id,
                    response_text="你好",
                )

        manager = GatewaySessionManager(backend_factory=StreamingBackend, start_reaper=False)
        facade = GatewayTurnFacade(manager=manager)
        seen: list[str] = []

        try:
            result = await facade.run_turn(
                GatewayTurnRequest(
                    user_id="user-1",
                    session_id="session-stream",
                    text="hello",
                    timeout_s=1,
                ),
                on_stream_chunk=lambda text, _frame: _append_async(seen, text),
            )
        finally:
            await manager.close()

        assert seen == ["你", "好"]
        assert result.response_text == "你好"

    async def test_run_turn_returns_gateway_error_terminal_result(self) -> None:
        class ErrorBackend:
            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                return RealtimeAgentResult(
                    status="error",
                    run_id=request.run_id,
                    metadata={"error_message": "backend failed", "error_type": "RuntimeError"},
                )

        manager = GatewaySessionManager(backend_factory=ErrorBackend, start_reaper=False)
        facade = GatewayTurnFacade(manager=manager)

        try:
            result = await facade.run_turn(
                GatewayTurnRequest(user_id="user-1", session_id="session-err", text="fail", timeout_s=1)
            )
        finally:
            await manager.close()

        assert result.status == "error"
        assert result.reason == "error"
        assert result.terminal_frame["error"]["message"] == "backend failed"
