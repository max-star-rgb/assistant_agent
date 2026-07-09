import asyncio
import threading

import pytest

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.agent.event_stream import AgentRunStream, AsyncQueueEventSink
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.requests import UserRequest


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)


def _event(event_type: str = "task_started", text: str | None = None) -> AgentEvent:
    return AgentEvent(type=event_type, session_id="s1", run_id="run_1", text=text)


class MutableCancelToken:
    def __init__(self, cancelled: bool = False, metadata: dict[str, object] | None = None) -> None:
        self.cancelled = cancelled
        self._metadata = dict(metadata or {})

    def is_cancelled(self) -> bool:
        return self.cancelled

    @property
    def cancel_metadata(self) -> dict[str, object]:
        return dict(self._metadata)


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
        assert not thread.is_alive()

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


def test_runtime_run_stream_yields_existing_agent_events_and_result_state() -> None:
    async def scenario() -> tuple[list[str], str, str]:
        runtime = AgentGraphRuntime()
        request = UserRequest(user_id="u1", session_id="s1", text="你好")
        stream = runtime.run_stream(request)

        events = [event async for event in stream]
        state = await stream.result()
        response_text = state.response.message if state.response is not None else ""
        return [event.type for event in events], state.status, response_text

    event_types, status, response_text = asyncio.run(scenario())

    assert status == "completed"
    assert event_types[0] == "task_started"
    assert "response_delta" in event_types
    assert event_types[-1] == "final_response"
    assert response_text


def test_runtime_run_stream_preserves_compatibility_event_sink() -> None:
    async def scenario() -> tuple[list[str], list[str]]:
        runtime = AgentGraphRuntime()
        compatibility_sink = RecordingSink()
        request = UserRequest(user_id="u1", session_id="s1", text="你好")
        stream = runtime.run_stream(request, event_sink=compatibility_sink)

        streamed = [event async for event in stream]
        await stream.result()
        return [event.type for event in streamed], [event.type for event in compatibility_sink.events]

    streamed_types, compatibility_types = asyncio.run(scenario())

    assert streamed_types
    assert streamed_types == compatibility_types


def test_runtime_run_stream_forwards_constructor_event_sink() -> None:
    async def scenario() -> tuple[list[str], list[str]]:
        compatibility_sink = RecordingSink()
        runtime = AgentGraphRuntime(event_sink=compatibility_sink)
        request = UserRequest(user_id="u1", session_id="s1", text="你好")
        stream = runtime.run_stream(request)

        streamed = [event async for event in stream]
        await stream.result()
        return [event.type for event in streamed], [event.type for event in compatibility_sink.events]

    streamed_types, compatibility_types = asyncio.run(scenario())

    assert streamed_types
    assert streamed_types == compatibility_types


def test_runtime_run_stream_pre_graph_cancel_returns_cancelled_state_and_event() -> None:
    async def scenario() -> tuple[list[str], str, str]:
        token = MutableCancelToken(
            cancelled=True,
            metadata={"cancel_source": "deadline", "cancel_reason": "run_deadline_expired"},
        )
        runtime = AgentGraphRuntime()
        request = UserRequest(user_id="u1", session_id="s1", text="hello")
        stream = runtime.run_stream(request, cancel_token=token)

        events = [event async for event in stream]
        state = await stream.result()
        return [event.type for event in events], state.status, state.errors[-1].details["cancel_source"]

    event_types, status, cancel_source = asyncio.run(scenario())

    assert event_types == ["task_started", "task_cancelled"]
    assert status == "cancelled"
    assert cancel_source == "deadline"


def test_runtime_run_stream_failed_run_yields_task_failed_and_failed_state() -> None:
    async def scenario() -> tuple[list[str], str]:
        runtime = AgentGraphRuntime()
        request = UserRequest(user_id="u1", session_id="s1", text="哪个便宜")
        stream = runtime.run_stream(request)

        events = [event async for event in stream]
        state = await stream.result()
        return [event.type for event in events], state.status

    event_types, status = asyncio.run(scenario())

    assert status == "failed"
    assert event_types[-1] == "task_failed"
