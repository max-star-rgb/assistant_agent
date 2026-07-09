# Service Async Stream Facade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a service-level async event stream facade that exposes existing `run_assistant_request()` events as an async iterator while preserving `AssistantRunArtifacts` as the final result.

**Architecture:** Keep `run_assistant_request()` as the synchronous source of truth for environment resolution, runtime creation, conversation history, realtime task state, cancellation, and final artifacts. Add `run_assistant_request_stream()` as a thin async facade that runs the existing service function in a worker thread, forwards emitted `AgentEvent` records into `AgentRunStream[AssistantRunArtifacts]`, and keeps compatibility event sinks working.

**Tech Stack:** Python 3.11, asyncio, pytest, existing `AgentRunStream`, existing `EventSink`, existing `AssistantRunArtifacts`.

## Global Constraints

- Do not rewrite `AgentGraphRuntime`, Tool, Memory, Provider, or Gateway internals in this phase.
- Do not add a new event schema or rename existing event types.
- Preserve `run_assistant_request()` and `run_assistant_query()` behavior.
- Use thread-safe event handoff from worker thread to asyncio loop.
- Keep final result separate from event stream: stream yields `AgentEvent`; `await stream.result()` returns `AssistantRunArtifacts`.

---

### Task 1: Service Stream Tests

**Files:**
- Modify: `tests/test_shared_assistant_run_service.py`

**Interfaces:**
- Consumes: `run_assistant_request_stream(request, ...) -> AgentRunStream[AssistantRunArtifacts]`
- Produces: tests that fail until the facade exists.

- [x] **Step 1: Write failing tests**

Add async scenario tests that:

```python
def test_run_assistant_request_stream_yields_events_and_returns_artifacts() -> None:
    async def scenario() -> tuple[list[str], str, list[str]]:
        store = InMemoryConversationStore()
        stream = run_assistant_request_stream(
            UserRequest(user_id="u1", session_id="s1", text="你好"),
            load_env=False,
            conversation_store=store,
        )

        events = [event async for event in stream]
        artifacts = await stream.result()
        return [event.type for event in events], artifacts.state.status, [event.type for event in artifacts.events]

    streamed_types, status, artifact_types = asyncio.run(scenario())

    assert status == "completed"
    assert streamed_types[0] == "task_started"
    assert "response_delta" in streamed_types
    assert streamed_types[-1] == "final_response"
    assert streamed_types == artifact_types
```

Also add compatibility sink and cancellation tests.

- [x] **Step 2: Verify tests fail**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_shared_assistant_run_service.py::test_run_assistant_request_stream_yields_events_and_returns_artifacts -q
```

Expected: import/name failure because `run_assistant_request_stream` does not exist yet.

### Task 2: Service Facade Implementation

**Files:**
- Modify: `src/assistant_agent/services/assistant_run_service.py`
- Modify: `src/assistant_agent/agent/event_stream.py`

**Interfaces:**
- Produces: `run_assistant_request_stream(...) -> AgentRunStream[AssistantRunArtifacts]`
- Preserves: `AsyncQueueEventSink.emit(event)` continues forwarding to optional inner sink.

- [x] **Step 1: Record stream events for service artifacts**

Update `AsyncQueueEventSink` to expose an `events: list[AgentEvent]` attribute and append every emitted event before forwarding it to the stream and compatibility sink.

- [x] **Step 2: Add service-level stream facade**

Implement `run_assistant_request_stream()` with the same keyword parameters as `run_assistant_request()`. It should:

```python
loop = asyncio.get_running_loop()
stream: AgentRunStream[AssistantRunArtifacts] = AgentRunStream(loop=loop)
stream_sink = AsyncQueueEventSink(loop=loop, stream=stream, inner=event_sink)
```

Then run `run_assistant_request(..., event_sink=stream_sink, ...)` in `asyncio.to_thread()`, calling `stream.set_result(artifacts)` or `stream.set_exception(exc)`.

- [x] **Step 3: Verify focused tests pass**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_runtime_stream.py tests/test_shared_assistant_run_service.py -q
```

Expected: all selected tests pass.

### Task 3: Docs And Verification

**Files:**
- Modify: `docs/runtime-event-stream-architecture.md`

**Interfaces:**
- Produces: documentation that Phase 2 is a service facade, not native async runtime.

- [x] **Step 1: Document Phase 2 boundary**

Update the runtime event stream architecture doc to state:

```text
Phase 2 adds run_assistant_request_stream(), which preserves the existing synchronous assistant run service and exposes its events through AgentRunStream[AssistantRunArtifacts].
```

- [x] **Step 2: Run verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_runtime_stream.py tests/test_shared_assistant_run_service.py tests/test_realtime_task_state.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
git diff --check
```

Expected: all commands exit 0.
