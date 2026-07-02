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

    async def test_interrupt_cancels_previous_run_then_starts_new(self) -> None:
        class InterruptBackend:
            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                if request.text == "first":
                    while not cancel_token.is_cancelled():
                        await asyncio.sleep(0.01)
                    return RealtimeAgentResult(status="cancelled", run_id=request.run_id)
                assert event_sink is not None
                await event_sink(RealtimeAgentEvent(type="response.chunk", text="second done"))
                return RealtimeAgentResult(status="completed", run_id=request.run_id, expects_reply=True)

        session = GatewaySessionService(backend=InterruptBackend())
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
