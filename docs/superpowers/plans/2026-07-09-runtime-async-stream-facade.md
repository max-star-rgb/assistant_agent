# Runtime Async Stream Facade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Phase 1 async stream facade that exposes existing `AgentEvent` records from `AgentGraphRuntime` through `async for` while preserving the existing final `AgentState` result path.

**Architecture:** Keep `AgentGraphRuntime.run_state()` and `run()` as the authoritative synchronous implementations. Add a small `agent.event_stream` module that runs the sync runtime function in a worker thread, bridges synchronous `EventSink.emit()` calls into an event-loop-owned queue with `loop.call_soon_threadsafe`, and exposes both async event iteration and `await result()`. Do not change Gateway, realtime event schemas, tools, memory, or providers in this phase.

**Status note:** Tasks 1-3 were executed and reviewed in the SDD ledger for this branch. Task 4 below records the documentation completion state for the plan file itself.

**Tech Stack:** Python 3, asyncio, Pydantic models already in the repo, existing `AgentEvent` and `EventSink`, pytest, conda environment `hello_agent`.

## Global Constraints

- Use existing `assistant_agent.schemas.events.AgentEvent`; do not introduce a third runtime event schema.
- Preserve `AgentGraphRuntime.run_state()` and `AgentGraphRuntime.run()` behavior.
- Preserve `run_assistant_request()` and `AssistantRunArtifacts` behavior.
- Preserve `RealtimeAgentRequest`, `RealtimeAgentEvent`, `RealtimeAgentResult`, `RealtimeAgentBackend`, and `RealtimeCancelToken`.
- Preserve Gateway wire frame names and lifecycle behavior.
- Do not change Tool, Memory, Provider, Gateway, or realtime backend business logic in Phase 1.
- Do not call real providers; use default mock/local/offline behavior.
- Do not add new dependencies.
- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for Python and pytest commands.
- Do not modify or stage unrelated untracked files such as `docs/superpowers/specs/2026-07-09-memory-intelligence-v1-design.md`.

---

### Task 1: Add Runtime Stream Primitive

**Files:**
- Create: `src/assistant_agent/agent/event_stream.py`
- Test: `tests/test_agent_runtime_stream.py`

**Interfaces:**
- Consumes: `assistant_agent.schemas.events.AgentEvent`, `assistant_agent.services.event_sink.EventSink`
- Produces: `AsyncQueueEventSink`, `AgentRunStream`

- [ ] **Step 1: Write the failing primitive tests**

Create `tests/test_agent_runtime_stream.py` with these tests:

```python
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
```

- [ ] **Step 2: Run the primitive tests to verify red**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_runtime_stream.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'assistant_agent.agent.event_stream'`.

- [ ] **Step 3: Implement the stream primitive**

Create `src/assistant_agent/agent/event_stream.py`:

```python
"""Async stream facade for synchronous agent runtime events."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from assistant_agent.schemas.events import AgentEvent
from assistant_agent.services.event_sink import EventSink


TResult = TypeVar("TResult")
_MISSING = object()


@dataclass(frozen=True)
class _EventItem:
    event: AgentEvent


@dataclass(frozen=True)
class _DoneItem:
    pass


class AgentRunStream(Generic[TResult]):
    """Async iterator over runtime events plus an explicit final result."""

    def __init__(self, *, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._queue: asyncio.Queue[_EventItem | _DoneItem] = asyncio.Queue()
        self._result_future: asyncio.Future[TResult] = loop.create_future()
        self._finished = False

    def __aiter__(self) -> "AgentRunStream[TResult]":
        return self

    async def __anext__(self) -> AgentEvent:
        item = await self._queue.get()
        if isinstance(item, _EventItem):
            return item.event
        if self._result_future.done():
            self._result_future.result()
        raise StopAsyncIteration

    async def result(self) -> TResult:
        return await self._result_future

    async def wait(self) -> TResult:
        return await self.result()

    def emit(self, event: AgentEvent) -> None:
        self._loop.call_soon_threadsafe(self._queue.put_nowait, _EventItem(event))

    def set_result(self, result: TResult) -> None:
        self._finish(result=result)

    def set_exception(self, exc: BaseException) -> None:
        self._finish(exc=exc)

    def _finish(
        self,
        *,
        result: TResult | object = _MISSING,
        exc: BaseException | None = None,
    ) -> None:
        def complete() -> None:
            if self._finished:
                return
            self._finished = True
            if exc is not None:
                self._result_future.set_exception(exc)
            else:
                self._result_future.set_result(result)  # type: ignore[arg-type]
            self._queue.put_nowait(_DoneItem())

        self._loop.call_soon_threadsafe(complete)


class AsyncQueueEventSink:
    """Thread-safe EventSink that forwards AgentEvent records to AgentRunStream."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        stream: AgentRunStream[Any],
        inner: EventSink | None = None,
    ) -> None:
        self._loop = loop
        self._stream = stream
        self._inner = inner

    def emit(self, event: AgentEvent) -> None:
        self._stream.emit(event)
        if self._inner is not None:
            self._inner.emit(event)
```

- [ ] **Step 4: Run the primitive tests to verify green**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_runtime_stream.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git add src/assistant_agent/agent/event_stream.py tests/test_agent_runtime_stream.py
git commit -m "feat: add async agent event stream primitive"
```

---

### Task 2: Add AgentGraphRuntime.run_stream

**Files:**
- Modify: `src/assistant_agent/agent/runtime.py`
- Test: `tests/test_agent_runtime_stream.py`

**Interfaces:**
- Consumes: `AgentRunStream[AgentState]`, `AsyncQueueEventSink`
- Produces: `AgentGraphRuntime.run_stream(request, *, event_sink=None, cancel_token=None) -> AgentRunStream[AgentState]`

- [ ] **Step 1: Add failing runtime stream tests**

Append these tests to `tests/test_agent_runtime_stream.py`:

```python
from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.schemas.requests import UserRequest


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
```

- [ ] **Step 2: Run runtime stream tests to verify red**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_runtime_stream.py::test_runtime_run_stream_yields_existing_agent_events_and_result_state tests/test_agent_runtime_stream.py::test_runtime_run_stream_preserves_compatibility_event_sink -q
```

Expected: fail with `AttributeError: 'AgentGraphRuntime' object has no attribute 'run_stream'`.

- [ ] **Step 3: Implement `AgentGraphRuntime.run_stream`**

Modify imports near the top of `src/assistant_agent/agent/runtime.py`:

```python
import asyncio
```

Add this import near the other agent imports:

```python
from assistant_agent.agent.event_stream import AgentRunStream, AsyncQueueEventSink
```

Add this method to `AgentGraphRuntime`, immediately before `def run(`:

```python
    def run_stream(
        self,
        request: UserRequest,
        *,
        event_sink: EventSink | None = None,
        cancel_token: Any | None = None,
    ) -> AgentRunStream[AgentState]:
        """Run the graph in a worker thread and expose AgentEvent records asynchronously."""

        loop = asyncio.get_running_loop()
        stream: AgentRunStream[AgentState] = AgentRunStream(loop=loop)
        stream_sink = AsyncQueueEventSink(loop=loop, stream=stream, inner=event_sink)

        async def _run() -> None:
            try:
                state = await asyncio.to_thread(
                    self.run_state,
                    request,
                    event_sink=stream_sink,
                    cancel_token=cancel_token,
                )
            except BaseException as exc:
                stream.set_exception(exc)
            else:
                stream.set_result(state)

        asyncio.create_task(_run())
        return stream
```

- [ ] **Step 4: Run runtime stream tests to verify green**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_runtime_stream.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Run existing event tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_events.py -q
```

Expected: all tests pass, proving the existing synchronous event sink behavior remains unchanged.

- [ ] **Step 6: Commit Task 2**

Run:

```bash
git add src/assistant_agent/agent/runtime.py tests/test_agent_runtime_stream.py
git commit -m "feat: expose runtime agent events as async stream"
```

---

### Task 3: Lock Cancellation And Failure Semantics

**Files:**
- Modify: `tests/test_agent_runtime_stream.py`
- Modify: `src/assistant_agent/agent/event_stream.py` only if tests reveal terminal ordering or result propagation defects.
- Modify: `src/assistant_agent/agent/runtime.py` only if cancellation is not passed through correctly.

**Interfaces:**
- Consumes: existing cancellation token shape with `is_cancelled()` and `cancel_metadata`
- Produces: tested stream behavior for cancelled and failed runs.

- [ ] **Step 1: Add failing cancellation and failure tests**

Append these tests to `tests/test_agent_runtime_stream.py`:

```python
class MutableCancelToken:
    def __init__(self, cancelled: bool = False, metadata: dict[str, object] | None = None) -> None:
        self.cancelled = cancelled
        self._metadata = dict(metadata or {})

    def is_cancelled(self) -> bool:
        return self.cancelled

    @property
    def cancel_metadata(self) -> dict[str, object]:
        return dict(self._metadata)


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
```

- [ ] **Step 2: Run cancellation and failure tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_runtime_stream.py::test_runtime_run_stream_pre_graph_cancel_returns_cancelled_state_and_event tests/test_agent_runtime_stream.py::test_runtime_run_stream_failed_run_yields_task_failed_and_failed_state -q
```

Expected: pass if Task 2 propagated `cancel_token` and terminal events correctly. If either test fails, inspect whether `run_stream()` omitted `cancel_token`, swallowed exceptions, or finished the stream before queued events were delivered.

- [ ] **Step 3: Fix terminal ordering only if tests fail**

If the iterator stops before the terminal event is observed, update `AgentRunStream._finish()` in `src/assistant_agent/agent/event_stream.py` so `_DoneItem()` is enqueued by the same event-loop callback after all prior event `call_soon_threadsafe` callbacks:

```python
    def _finish(
        self,
        *,
        result: TResult | object = _MISSING,
        exc: BaseException | None = None,
    ) -> None:
        def complete() -> None:
            if self._finished:
                return
            self._finished = True
            if exc is not None:
                self._result_future.set_exception(exc)
            else:
                self._result_future.set_result(result)  # type: ignore[arg-type]
            self._queue.put_nowait(_DoneItem())

        self._loop.call_soon_threadsafe(complete)
```

- [ ] **Step 4: Run focused stream and cancellation tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_runtime_stream.py tests/test_agent_runtime_cancellation.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 3**

Run:

```bash
git add src/assistant_agent/agent/event_stream.py src/assistant_agent/agent/runtime.py tests/test_agent_runtime_stream.py
git commit -m "test: lock runtime stream terminal semantics"
```

---

### Task 4: Document Phase 1 Completion Gates

**Files:**
- Modify: `docs/runtime-event-stream-architecture.md`
- Test: documentation and runtime regression commands.

**Interfaces:**
- Consumes: `AgentGraphRuntime.run_stream(...) -> AgentRunStream[AgentState]`
- Produces: updated architecture notes if implementation differs from the design.

- [x] **Step 1: Update the architecture document with exact implemented signatures**

After Tasks 1 through 3 pass, update `docs/runtime-event-stream-architecture.md` so the Phase 1 interface section matches the implemented method signatures. The expected text is:

```markdown
## Phase 1 Implemented Interfaces

`AgentGraphRuntime.run_stream(request, *, event_sink=None, cancel_token=None)`
returns `AgentRunStream[AgentState]`.

`AgentRunStream` supports async iteration over `AgentEvent` and
`await stream.result()` for the terminal `AgentState`.
```

- [x] **Step 2: Run markdown whitespace check**

Run:

```bash
git diff --check -- docs/runtime-event-stream-architecture.md docs/superpowers/plans/2026-07-09-runtime-async-stream-facade.md
```

Expected: no output.

- [x] **Step 3: Run full Phase 1 verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_runtime_stream.py tests/test_agent_events.py tests/test_agent_runtime_cancellation.py -q
```

Expected: all tests pass.

- [x] **Step 4: Run realtime mapping regression**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_event_mapping.py tests/test_realtime_backend_types.py -q
```

Expected: all tests pass, proving Phase 1 did not change realtime event schemas or mapping behavior.

- [x] **Step 5: Commit Task 4**

Run:

```bash
git add docs/runtime-event-stream-architecture.md docs/superpowers/plans/2026-07-09-runtime-async-stream-facade.md
git commit -m "docs: define runtime async stream facade plan"
```

---

## Self-Review

- Spec coverage: The plan covers existing `AgentEvent` reuse, stream facade creation, thread-safe queue bridging, final `AgentState` result access, cancellation preservation, and no changes to Gateway, Tool, Memory, Provider, or realtime schemas.
- Reserved-marker scan: The plan contains no reserved markers or unspecified implementation steps.
- Type consistency: `AgentRunStream[AgentState]`, `AsyncQueueEventSink`, `AgentGraphRuntime.run_stream`, and `MutableCancelToken` names are consistent across all tasks.
- Boundary check: `AgentGraphRealtimeBackend` migration is intentionally excluded from this Phase 1 plan and should be planned separately after a service-level stream facade exists.
