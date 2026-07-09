from __future__ import annotations

import asyncio
import unittest

from assistant_agent.gateway import GatewaySessionService, InMemoryDuplex, frame
from assistant_agent.realtime import RealtimeAgentEvent, RealtimeAgentResult


async def _close_session(client_ep, session_ep, session_task) -> None:
    await client_ep.close()
    await session_ep.close()
    session_task.cancel()
    await asyncio.gather(session_task, return_exceptions=True)


async def _assert_no_frame(client_ep, *, timeout_s: float = 0.08) -> None:
    async def _read_one():
        async for received in client_ep:
            return received
        return None

    try:
        received = await asyncio.wait_for(_read_one(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return
    raise AssertionError(f"unexpected frame after terminal realtime gate: {received}")


class Phase1RealtimeLoopDeepGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_turn_waits_for_active_run_and_preserves_ordered_history(self) -> None:
        class QueueBackend:
            def __init__(self) -> None:
                self.release_first = asyncio.Event()
                self.requests = []

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                self.requests.append(request)
                if request.text == "first":
                    await self.release_first.wait()
                    return RealtimeAgentResult(status="completed", run_id=request.run_id, trace_id="trace_first")
                assert event_sink is not None
                await event_sink(RealtimeAgentEvent(type="response.chunk", text="second reply"))
                return RealtimeAgentResult(status="completed", run_id=request.run_id, trace_id="trace_second")

        backend = QueueBackend()
        session = GatewaySessionService(backend=backend)
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        frames: list[dict] = []
        first_run_id = None
        second_run_id = None

        async def _read_flow() -> None:
            nonlocal first_run_id, second_run_id
            async for received in client_ep:
                frames.append(received)
                if received["type"] == "run.started" and first_run_id is None:
                    first_run_id = received["run_id"]
                    await client_ep.send(
                        frame(type="message.user", session_id="phase1-queue", payload={"text": "second"})
                    )
                    await _assert_no_frame(client_ep)
                    backend.release_first.set()
                elif received["type"] == "run.started":
                    second_run_id = received["run_id"]
                elif received["type"] == "run.end" and second_run_id is not None:
                    return

        try:
            await client_ep.send(
                frame(type="message.user", session_id="phase1-queue", payload={"text": "first"})
            )
            await asyncio.wait_for(_read_flow(), timeout=3.0)
        finally:
            backend.release_first.set()
            await _close_session(client_ep, session_ep, session_task)

        assert [item["type"] for item in frames] == [
            "run.started",
            "run.end",
            "run.started",
            "stream.chunk",
            "run.end",
        ]
        assert first_run_id is not None
        assert second_run_id is not None
        assert first_run_id != second_run_id
        assert [request.text for request in backend.requests] == ["first", "second"]
        assert backend.requests[1].metadata["gateway"]["history"] == ["first", "second"]

    async def test_explicit_cancel_run_end_payload_includes_prompt_safe_cancel_source(self) -> None:
        class CancellableBackend:
            def __init__(self) -> None:
                self.cancel_seen = asyncio.Event()

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                await cancel_token.cancelled()
                self.cancel_seen.set()
                return RealtimeAgentResult(
                    status="cancelled",
                    run_id=request.run_id,
                    trace_id="trace_cancel",
                    metadata={
                        **cancel_token.cancel_metadata,
                        "cancel_phase": "agent_run",
                        "best_effort": True,
                    },
                )

        backend = CancellableBackend()
        session = GatewaySessionService(backend=backend)
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        frames: list[dict] = []

        async def _read_flow() -> None:
            async for received in client_ep:
                frames.append(received)
                if received["type"] == "run.started":
                    await client_ep.send(
                        frame(
                            type="run.cancel",
                            session_id="phase1-cancel",
                            run_id=received["run_id"],
                            payload={"reason": "user_requested_stop"},
                        )
                    )
                if received["type"] == "run.end":
                    return

        try:
            await client_ep.send(
                frame(type="message.user", session_id="phase1-cancel", payload={"text": "cancel me"})
            )
            await asyncio.wait_for(_read_flow(), timeout=3.0)
            await asyncio.wait_for(backend.cancel_seen.wait(), timeout=1.0)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        run_end = frames[-1]
        assert run_end["reason"] == "cancelled"
        assert "trace_id" not in run_end["payload"]
        assert run_end["payload"]["trace"] == {
            "status": "not_available",
            "reason": "cancelled_before_backend_result",
        }
        assert run_end["payload"]["cancel"] == {
            "source": "gateway_cancel",
            "reason": "user_requested_stop",
            "phase": "gateway_output_gate",
            "best_effort": True,
        }

    async def test_cancel_suppresses_late_stream_chunks(self) -> None:
        class LateChunkBackend:
            def __init__(self) -> None:
                self.late_chunk_attempted = asyncio.Event()

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                await cancel_token.cancelled()
                assert event_sink is not None
                await event_sink(RealtimeAgentEvent(type="response.chunk", text="late old answer"))
                self.late_chunk_attempted.set()
                return RealtimeAgentResult(
                    status="completed",
                    run_id=request.run_id,
                    trace_id="trace_late_chunk",
                    response_text="late old answer",
                )

        backend = LateChunkBackend()
        session = GatewaySessionService(backend=backend)
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        frames: list[dict] = []

        async def _read_flow() -> None:
            async for received in client_ep:
                frames.append(received)
                if received["type"] == "run.started":
                    await client_ep.send(
                        frame(
                            type="run.cancel",
                            session_id="phase1-late-chunk",
                            run_id=received["run_id"],
                            payload={"reason": "late_chunk_test"},
                        )
                    )
                if received["type"] == "run.end":
                    return

        try:
            await client_ep.send(
                frame(type="message.user", session_id="phase1-late-chunk", payload={"text": "cancel late"})
            )
            await asyncio.wait_for(_read_flow(), timeout=3.0)
            await asyncio.wait_for(backend.late_chunk_attempted.wait(), timeout=1.0)
            await _assert_no_frame(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert [item["type"] for item in frames] == ["run.started", "run.end"]
        assert frames[-1]["reason"] == "cancelled"
        assert frames[-1]["payload"]["cancel"]["source"] == "gateway_cancel"
        assert "late old answer" not in str(frames)

    async def test_tool_running_interrupt_suppresses_stale_tool_output_and_completes_new_turn(self) -> None:
        class ToolRunningBackend:
            def __init__(self) -> None:
                self.first_finished = asyncio.Event()
                self.requests = []

            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                self.requests.append(request)
                assert event_sink is not None
                if request.text == "first tool turn":
                    await event_sink(
                        RealtimeAgentEvent(
                            type="tool.started",
                            text="starting slow tool",
                            payload={"tool_name": "slow_tool"},
                        )
                    )
                    await cancel_token.cancelled()
                    await event_sink(
                        RealtimeAgentEvent(
                            type="tool.finished",
                            text="stale slow tool result",
                            payload={"tool_name": "slow_tool"},
                        )
                    )
                    await event_sink(RealtimeAgentEvent(type="response.chunk", text="stale old answer"))
                    self.first_finished.set()
                    return RealtimeAgentResult(
                        status="completed",
                        run_id=request.run_id,
                        trace_id="trace_first_tool",
                        response_text="stale old answer",
                    )

                await event_sink(RealtimeAgentEvent(type="response.chunk", text="new answer"))
                return RealtimeAgentResult(
                    status="completed",
                    run_id=request.run_id,
                    trace_id="trace_second_tool",
                    response_text="new answer",
                )

        backend = ToolRunningBackend()
        session = GatewaySessionService(backend=backend)
        client_ep, session_ep = InMemoryDuplex.create_pair()
        session_task = asyncio.create_task(session.serve(session_ep))
        first_run_id = None
        second_run_id = None
        ended: dict[str, dict] = {}
        frames_by_run: dict[str, list[dict]] = {}

        async def _read_flow() -> None:
            nonlocal first_run_id, second_run_id
            async for received in client_ep:
                if received["type"] == "run.started" and first_run_id is None:
                    first_run_id = received["run_id"]
                elif received["type"] == "event.tool" and received["run_id"] == first_run_id:
                    frames_by_run.setdefault(received["run_id"], []).append(received)
                    await client_ep.send(
                        frame(
                            type="message.user",
                            session_id="phase1-tool-interrupt",
                            payload={"text": "second turn", "interrupt": True},
                        )
                    )
                elif received["type"] == "run.started":
                    second_run_id = received["run_id"]
                elif received["type"] in {"event.tool", "stream.chunk"}:
                    frames_by_run.setdefault(received["run_id"], []).append(received)
                elif received["type"] == "run.end":
                    ended[received["run_id"]] = received

                if first_run_id and second_run_id:
                    if ended.get(first_run_id, {}).get("reason") == "cancelled" and ended.get(
                        second_run_id, {}
                    ).get("reason") == "completed":
                        return

        try:
            await client_ep.send(
                frame(
                    type="message.user",
                    session_id="phase1-tool-interrupt",
                    payload={"text": "first tool turn"},
                )
            )
            await asyncio.wait_for(_read_flow(), timeout=3.0)
            await asyncio.wait_for(backend.first_finished.wait(), timeout=2.0)
            await _assert_no_frame(client_ep)
        finally:
            await _close_session(client_ep, session_ep, session_task)

        assert first_run_id is not None
        assert second_run_id is not None
        assert first_run_id != second_run_id
        first_frames = frames_by_run.get(first_run_id, [])
        assert [item["type"] for item in first_frames] == ["event.tool"]
        assert first_frames[0]["payload"]["phase"] == "start"
        assert "stale old answer" not in str(frames_by_run)
        assert [item["payload"].get("text") for item in frames_by_run[second_run_id]] == ["new answer"]
        assert backend.requests[1].metadata["control"] == "interrupt"
        assert backend.requests[1].metadata["gateway"]["control"] == "interrupt"
        assert backend.requests[1].metadata["gateway"]["history"] == ["first tool turn", "second turn"]
        assert ended[first_run_id]["payload"]["cancel"]["source"] == "gateway_interrupt"
        assert ended[second_run_id]["payload"]["trace_id"] == "trace_second_tool"


if __name__ == "__main__":
    unittest.main()
