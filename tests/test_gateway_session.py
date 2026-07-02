from __future__ import annotations

import asyncio
import unittest

from assistant_agent.gateway import InMemoryDuplex, GatewaySessionService, dumps_frame, frame, loads_frame
from assistant_agent.realtime import RealtimeAgentEvent, RealtimeAgentResult


async def _close_session(client_ep, session_ep, session_task) -> None:
    await client_ep.close()
    await session_ep.close()
    session_task.cancel()
    await asyncio.gather(session_task, return_exceptions=True)


async def _collect_until_run_end(client_ep, *, timeout_s: float = 3.0):
    frames = []

    async def _read():
        async for received in client_ep:
            frames.append(received)
            if received["type"] == "run.end":
                return frames
        raise AssertionError("endpoint closed before run.end")

    return await asyncio.wait_for(_read(), timeout=timeout_s)


async def _assert_no_frame(client_ep, *, timeout_s: float = 0.08) -> None:
    async def _read_one():
        async for received in client_ep:
            return received
        return None

    try:
        received = await asyncio.wait_for(_read_one(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return
    raise AssertionError(f"unexpected frame after run end: {received}")


class GatewaySessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_user_streams_via_realtime_backend(self) -> None:
        class RecordingBackend:
            def __init__(self) -> None:
                self.requests = []

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                self.requests.append(request)
                assert event_sink is not None
                await event_sink(RealtimeAgentEvent(type="response.chunk", text="assistant smoke"))
                return RealtimeAgentResult(status="completed", response_text="assistant smoke", expects_reply=True)

        backend = RecordingBackend()
        session = GatewaySessionService(backend=backend)
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="smoke-session",
                    user_id="smoke-user",
                    payload={"text": "hello realtime", "turn_id": "turn-1"},
                )
            )
            frames = await _collect_until_run_end(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert [received["type"] for received in frames] == [
            "run.started",
            "stream.chunk",
            "run.end",
        ]
        assert frames[1]["payload"]["text"] == "assistant smoke"
        assert frames[-1]["reason"] == "completed"
        assert frames[-1]["payload"]["expects_reply"] is True
        assert len(backend.requests) == 1
        assert backend.requests[0].text == "hello realtime"
        assert backend.requests[0].user_id == "smoke-user"
        assert backend.requests[0].metadata["runtime"]["history"] == ["hello realtime"]

    async def test_cancel_preserves_cancelled_run_end(self) -> None:
        class CancellableBackend:
            def __init__(self) -> None:
                self.cancel_seen = asyncio.Event()
                self.release = asyncio.Event()
                self.requests = []

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                self.requests.append(request)
                while not cancel_token.is_cancelled():
                    await asyncio.sleep(0.01)
                self.cancel_seen.set()
                self.cancel_metadata = cancel_token.cancel_metadata
                await self.release.wait()
                return RealtimeAgentResult(status="cancelled", run_id=request.run_id)

        backend = CancellableBackend()
        session = GatewaySessionService(backend=backend)
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        frames = []

        async def _read_cancel_flow():
            async for received in client_ep:
                frames.append(received)
                if received["type"] == "run.started":
                    await client_ep.send(
                        frame(
                            type="run.cancel",
                            session_id="cancel-session",
                            run_id=received["run_id"],
                        )
                    )
                    await asyncio.wait_for(backend.cancel_seen.wait(), timeout=2.0)
                    backend.release.set()
                if received["type"] == "run.end":
                    return frames
            raise AssertionError("endpoint closed before run.end")

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="cancel-session",
                    payload={"text": "cancel realtime", "turn_id": "turn-cancel"},
                )
            )
            frames = await asyncio.wait_for(_read_cancel_flow(), timeout=3.0)
        finally:
            backend.release.set()
            await _close_session(client_ep, session_ep, session_task)

        assert frames[0]["type"] == "run.started"
        assert frames[-1]["type"] == "run.end"
        assert frames[-1]["reason"] == "cancelled"
        assert frames[-1]["payload"]["expects_reply"] is True
        assert len(backend.requests) == 1
        assert backend.requests[0].text == "cancel realtime"
        assert backend.cancel_metadata["cancel_source"] == "gateway_cancel"

    async def test_cancel_suppresses_backend_events_emitted_after_cancel(self) -> None:
        class StaleEventBackend:
            def __init__(self) -> None:
                self.finished = asyncio.Event()

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                await cancel_token.cancelled()
                assert event_sink is not None
                for event in [
                    RealtimeAgentEvent(type="response.chunk", text="stale chunk"),
                    RealtimeAgentEvent(type="response.final", text="stale final"),
                    RealtimeAgentEvent(
                        type="tool.started",
                        text="stale tool",
                        payload={"tool_name": "stale_tool"},
                    ),
                    RealtimeAgentEvent(
                        type="trace.decision",
                        text="stale trace",
                        payload={"decision_trace": {"action": "stale"}},
                    ),
                    RealtimeAgentEvent(type="error", text="stale error"),
                ]:
                    await event_sink(event)
                self.finished.set()
                return RealtimeAgentResult(
                    status="completed",
                    run_id=request.run_id,
                    response_text="stale final",
                    expects_reply=False,
                )

        backend = StaleEventBackend()
        session = GatewaySessionService(backend=backend)
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        frames = []

        async def _read_cancel_flow():
            async for received in client_ep:
                frames.append(received)
                if received["type"] == "run.started":
                    await client_ep.send(
                        frame(
                            type="run.cancel",
                            session_id="stale-cancel-session",
                            run_id=received["run_id"],
                        )
                    )
                if received["type"] == "run.end":
                    return frames
            raise AssertionError("endpoint closed before run.end")

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="stale-cancel-session",
                    payload={"text": "cancel stale events"},
                )
            )
            frames = await asyncio.wait_for(_read_cancel_flow(), timeout=3.0)
            await asyncio.wait_for(backend.finished.wait(), timeout=2.0)
            await _assert_no_frame(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert [received["type"] for received in frames] == ["run.started", "run.end"]
        assert frames[-1]["reason"] == "cancelled"
        assert frames[-1]["payload"]["expects_reply"] is True

    async def test_interrupt_cancels_previous_run_then_starts_new(self) -> None:
        class InterruptBackend:
            def __init__(self) -> None:
                self.first_cancel_metadata = None
                self.first_cancel_seen = asyncio.Event()

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                if request.text == "first":
                    while not cancel_token.is_cancelled():
                        await asyncio.sleep(0.01)
                    self.first_cancel_metadata = cancel_token.cancel_metadata
                    self.first_cancel_seen.set()
                    return RealtimeAgentResult(status="cancelled", run_id=request.run_id)
                assert event_sink is not None
                await event_sink(RealtimeAgentEvent(type="response.chunk", text="second done"))
                return RealtimeAgentResult(status="completed", run_id=request.run_id, expects_reply=True)

        backend = InterruptBackend()
        session = GatewaySessionService(backend=backend)
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        first_run = None
        second_run = None
        saw_first_cancelled = False
        saw_second_completed = False

        try:
            await client_ep.send(
                frame(type="message.user", session_id="interrupt-session", payload={"text": "first"})
            )

            async def _read_until_both_runs_end() -> None:
                nonlocal first_run, second_run, saw_first_cancelled, saw_second_completed
                async for received in client_ep:
                    if received["type"] == "run.started" and first_run is None:
                        first_run = received["run_id"]
                        await client_ep.send(
                            frame(
                                type="message.user",
                                session_id="interrupt-session",
                                payload={"text": "second"},
                            )
                        )
                    elif received["type"] == "run.end" and received.get("run_id") == first_run:
                        saw_first_cancelled = received.get("reason") == "cancelled"
                    elif received["type"] == "run.started" and first_run is not None:
                        second_run = received["run_id"]
                    elif received["type"] == "run.end" and second_run is not None:
                        assert received["run_id"] == second_run
                        saw_second_completed = received.get("reason") == "completed"
                    if saw_first_cancelled and saw_second_completed:
                        return

            await asyncio.wait_for(_read_until_both_runs_end(), timeout=3.0)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert first_run is not None
        assert second_run is not None
        assert first_run != second_run
        assert saw_first_cancelled is True
        assert saw_second_completed is True
        await asyncio.wait_for(backend.first_cancel_seen.wait(), timeout=2.0)
        assert backend.first_cancel_metadata["cancel_source"] == "gateway_interrupt"

    async def test_interrupt_suppresses_previous_run_events_after_new_message(self) -> None:
        class InterruptStaleEventBackend:
            def __init__(self) -> None:
                self.first_finished = asyncio.Event()

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                assert event_sink is not None
                if request.text == "first":
                    await cancel_token.cancelled()
                    await event_sink(RealtimeAgentEvent(type="response.chunk", text="first stale"))
                    await event_sink(
                        RealtimeAgentEvent(
                            type="tool.finished",
                            text="first tool stale",
                            payload={"tool_name": "first_tool"},
                        )
                    )
                    self.first_finished.set()
                    return RealtimeAgentResult(
                        status="completed",
                        run_id=request.run_id,
                        response_text="first stale final",
                    )
                await event_sink(RealtimeAgentEvent(type="response.chunk", text="second done"))
                return RealtimeAgentResult(
                    status="completed",
                    run_id=request.run_id,
                    response_text="second done",
                    expects_reply=True,
                )

        backend = InterruptStaleEventBackend()
        session = GatewaySessionService(backend=backend)
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        first_run = None
        second_run = None
        ended: dict[str, str] = {}
        chunks_by_run: dict[str, list[str]] = {}

        try:
            await client_ep.send(
                frame(type="message.user", session_id="interrupt-stale-session", payload={"text": "first"})
            )

            async def _read_until_both_runs_end() -> None:
                nonlocal first_run, second_run
                async for received in client_ep:
                    if received["type"] == "run.started" and first_run is None:
                        first_run = received["run_id"]
                        await client_ep.send(
                            frame(
                                type="message.user",
                                session_id="interrupt-stale-session",
                                payload={"text": "second"},
                            )
                        )
                    elif received["type"] == "run.started":
                        second_run = received["run_id"]
                    elif received["type"] == "stream.chunk":
                        chunks_by_run.setdefault(received["run_id"], []).append(
                            received["payload"]["text"]
                        )
                    elif received["type"] == "run.end":
                        ended[received["run_id"]] = received["reason"]
                    if first_run is not None and second_run is not None:
                        if ended.get(first_run) == "cancelled" and ended.get(second_run) == "completed":
                            return

            await asyncio.wait_for(_read_until_both_runs_end(), timeout=3.0)
            await asyncio.wait_for(backend.first_finished.wait(), timeout=2.0)
            await _assert_no_frame(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert first_run is not None
        assert second_run is not None
        assert first_run != second_run
        assert chunks_by_run.get(first_run, []) == []
        assert chunks_by_run.get(second_run) == ["second done"]

    async def test_run_deadline_from_session_config_cancels_backend(self) -> None:
        class DeadlineBackend:
            def __init__(self) -> None:
                self.cancel_seen = asyncio.Event()
                self.cancel_metadata = None
                self.requests = []

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                self.requests.append(request)
                await cancel_token.cancelled()
                self.cancel_metadata = cancel_token.cancel_metadata
                self.cancel_seen.set()
                return RealtimeAgentResult(status="cancelled", run_id=request.run_id)

        backend = DeadlineBackend()
        session = GatewaySessionService(backend=backend, config={"run_timeout_ms": 30})
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="deadline-session",
                    payload={"text": "deadline please"},
                )
            )
            frames = await _collect_until_run_end(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert frames[-1]["reason"] == "cancelled"
        assert frames[-1]["payload"]["expects_reply"] is True
        await asyncio.wait_for(backend.cancel_seen.wait(), timeout=2.0)
        assert backend.cancel_seen.is_set()
        assert backend.cancel_metadata == {
            "deadline_ms": 30,
            "cancel_source": "deadline",
            "cancel_reason": "run_deadline_expired",
        }
        assert backend.requests[0].metadata["runtime"]["session_config"]["run_timeout_ms"] == 30

    async def test_run_deadline_from_message_metadata_overrides_session_config(self) -> None:
        class DeadlineBackend:
            def __init__(self) -> None:
                self.cancel_metadata = None
                self.cancel_seen = asyncio.Event()

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                await cancel_token.cancelled()
                self.cancel_metadata = cancel_token.cancel_metadata
                self.cancel_seen.set()
                return RealtimeAgentResult(status="cancelled", run_id=request.run_id)

        backend = DeadlineBackend()
        session = GatewaySessionService(backend=backend, config={"run_timeout_ms": 5000})
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="deadline-override-session",
                    payload={
                        "text": "deadline override",
                        "metadata": {"gateway": {"run_timeout_ms": 25}},
                    },
                )
            )
            frames = await _collect_until_run_end(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert frames[-1]["reason"] == "cancelled"
        await asyncio.wait_for(backend.cancel_seen.wait(), timeout=2.0)
        assert backend.cancel_metadata["cancel_source"] == "deadline"
        assert backend.cancel_metadata["deadline_ms"] == 25

    async def test_deadline_suppresses_backend_events_emitted_after_timeout(self) -> None:
        class DeadlineStaleEventBackend:
            def __init__(self) -> None:
                self.finished = asyncio.Event()
                self.cancel_metadata = None

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                await cancel_token.cancelled()
                self.cancel_metadata = cancel_token.cancel_metadata
                assert event_sink is not None
                await event_sink(RealtimeAgentEvent(type="response.chunk", text="deadline stale"))
                await event_sink(RealtimeAgentEvent(type="error", text="deadline stale error"))
                self.finished.set()
                return RealtimeAgentResult(status="completed", run_id=request.run_id)

        backend = DeadlineStaleEventBackend()
        session = GatewaySessionService(backend=backend, config={"run_timeout_ms": 20})
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="deadline-stale-session",
                    payload={"text": "deadline stale events"},
                )
            )
            frames = await _collect_until_run_end(client_ep)
            await asyncio.wait_for(backend.finished.wait(), timeout=2.0)
            await _assert_no_frame(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert [received["type"] for received in frames] == ["run.started", "run.end"]
        assert frames[-1]["reason"] == "cancelled"
        assert frames[-1]["payload"]["expects_reply"] is True
        assert backend.cancel_metadata == {
            "deadline_ms": 20,
            "cancel_source": "deadline",
            "cancel_reason": "run_deadline_expired",
        }

    async def test_completed_run_cleans_deadline_monitor(self) -> None:
        class FastBackend:
            def __init__(self) -> None:
                self.cancel_token = None

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                self.cancel_token = cancel_token
                return RealtimeAgentResult(status="completed", run_id=request.run_id, expects_reply=True)

        backend = FastBackend()
        session = GatewaySessionService(backend=backend, config={"run_timeout_ms": 50})
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="deadline-cleanup-session",
                    payload={"text": "fast"},
                )
            )
            frames = await _collect_until_run_end(client_ep)
            await asyncio.sleep(0.08)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert frames[-1]["reason"] == "completed"
        assert backend.cancel_token is not None
        assert backend.cancel_token.is_cancelled() is False

    async def test_message_timeout_zero_disables_session_config_deadline(self) -> None:
        class SlowCompletedBackend:
            def __init__(self) -> None:
                self.cancel_token = None

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                self.cancel_token = cancel_token
                await asyncio.sleep(0.04)
                return RealtimeAgentResult(status="completed", run_id=request.run_id, expects_reply=True)

        backend = SlowCompletedBackend()
        session = GatewaySessionService(backend=backend, config={"run_timeout_ms": 10})
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="deadline-disabled-session",
                    payload={
                        "text": "disable deadline",
                        "metadata": {"gateway": {"run_timeout_ms": 0}},
                    },
                )
            )
            frames = await _collect_until_run_end(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert frames[-1]["reason"] == "completed"
        assert backend.cancel_token is not None
        assert backend.cancel_token.is_cancelled() is False

    async def test_multiturn_history_is_passed_to_backend_metadata(self) -> None:
        class HistoryBackend:
            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                assert event_sink is not None
                history = request.metadata["runtime"]["history"]
                await event_sink(
                    RealtimeAgentEvent(
                        type="response.chunk",
                        text=f"echo:{request.text};history:{'|'.join(history)}",
                    )
                )
                return RealtimeAgentResult(status="completed", run_id=request.run_id, expects_reply=True)

        session = GatewaySessionService(backend=HistoryBackend())
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))

        async def one_turn(text: str) -> str:
            await client_ep.send(frame(type="message.user", session_id="history-session", payload={"text": text}))
            chunks: list[str] = []
            async for received in client_ep:
                if received["type"] == "stream.chunk":
                    chunks.append(received["payload"]["text"])
                if received["type"] == "run.end":
                    break
            return "".join(chunks)

        try:
            out1 = await one_turn("one")
            out2 = await one_turn("two")
            out3 = await one_turn("three")
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert "history:one" in out1
        assert "history:one|two" in out2
        assert "history:one|two|three" in out3


def test_websocket_frame_json_roundtrip() -> None:
    source = frame(type="stream.chunk", session_id="s1", payload={"text": "hello"})

    assert loads_frame(dumps_frame(source)) == source


if __name__ == "__main__":
    unittest.main()
