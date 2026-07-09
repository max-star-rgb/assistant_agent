import asyncio
import threading

import pytest

from assistant_agent.agent.event_stream import AgentRunStream, AsyncQueueEventSink
from assistant_agent.schemas.events import AgentEvent


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)


def _event(event_type: str = "task_started", text: str | None = None) -> AgentEvent:
    return AgentEvent(type=event_type, session_id="s1", run_id="run_1", text=text)


def test_async_queue_event_sink_forwards_from_worker_thread_in_order() -> None:
    async def scenario() -> list[AgentEvent]:
        loop = asyncio.get_running_loop()
        stream = AgentRunStream(loop=loop)
        sink = AsyncQueueEventSink(loop=loop, stream=stream)
        events = [_event("task_started"), _event("response_delta", "hello")]

        def worker() -> None:
            for event in events:
                sink.emit(event)
            stream.set_result("done")

        thread = threading.Thread(target=worker)
        thread.start()

        seen: list[AgentEvent] = []
        async for event in stream:
            seen.append(event)
        thread.join(timeout=2)

        assert await stream.result() == "done"
        return seen

    seen = asyncio.run(scenario())
    assert [event.type for event in seen] == ["task_started", "response_delta"]


def test_async_queue_event_sink_also_forwards_to_compatibility_sink() -> None:
    async def scenario() -> tuple[list[AgentEvent], list[AgentEvent]]:
        loop = asyncio.get_running_loop()
        stream = AgentRunStream(loop=loop)
        compatibility_sink = RecordingSink()
        sink = AsyncQueueEventSink(loop=loop, stream=stream, inner=compatibility_sink)
        first = _event("task_started")
        second = _event("final_response", "done")

        sink.emit(first)
        sink.emit(second)
        stream.set_result("state")

        seen = [event async for event in stream]
        return seen, compatibility_sink.events

    seen, forwarded = asyncio.run(scenario())
    assert [event.type for event in seen] == ["task_started", "final_response"]
    assert [event.type for event in forwarded] == ["task_started", "final_response"]


def test_agent_run_stream_result_reraises_worker_exception_after_events_drain() -> None:
    async def scenario() -> list[AgentEvent]:
        loop = asyncio.get_running_loop()
        stream = AgentRunStream(loop=loop)
        sink = AsyncQueueEventSink(loop=loop, stream=stream)
        sink.emit(_event("task_started"))
        stream.set_exception(RuntimeError("worker failed"))

        seen: list[AgentEvent] = []
        with pytest.raises(RuntimeError, match="worker failed"):
            async for event in stream:
                seen.append(event)
        with pytest.raises(RuntimeError, match="worker failed"):
            await stream.result()
        return seen

    seen = asyncio.run(scenario())
    assert [event.type for event in seen] == ["task_started"]
