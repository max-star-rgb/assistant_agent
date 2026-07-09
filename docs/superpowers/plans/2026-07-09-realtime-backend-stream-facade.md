# Realtime Backend Stream Facade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate `AgentGraphRealtimeBackend` to consume the service-level assistant event stream while preserving existing Gateway-facing realtime behavior.

**Architecture:** Default realtime backend execution should call `run_assistant_request_stream()` and consume `AgentEvent` records with `async for`. Existing sync `run_request=` injection remains supported through a small compatibility stream wrapper so tests and entry adapters do not break. The backend keeps its current mapping, progress policy, cancellation result mapping, final response handling, and `RealtimeAgentResult` contract.

**Tech Stack:** Python 3.12, asyncio, pytest, existing `AgentRunStream`, existing `run_assistant_request_stream`, existing `RealtimeAgentEvent` mapping helpers.

## Global Constraints

- Do not change Gateway frame names or realtime public types.
- Do not rewrite `AgentGraphRuntime`, Tool, Memory, Provider, or Gateway internals.
- Do not introduce a new event schema.
- Preserve `run_request=` sync injection compatibility.
- Default backend execution should consume `run_assistant_request_stream()` rather than passing `_RealtimeForwardingEventSink` as a callback sink to `run_assistant_request()`.
- Keep final results explicit: stream yields `AgentEvent`; backend result comes from `await stream.result()`.

---

### Task 1: Stream Provider Test

**Files:**
- Modify: `tests/test_realtime_agent_backend.py`

**Interfaces:**
- Consumes: `AgentGraphRealtimeBackend(run_request_stream=...)`
- Produces: a failing test proving the backend can consume an async `AgentRunStream` provider directly.

- [x] **Step 1: Write the failing test**

Add imports:

```python
from assistant_agent.agent.event_stream import AgentRunStream
```

Add a test that builds a stream provider:

```python
def test_agent_graph_realtime_backend_consumes_run_request_stream_provider() -> None:
    captured: dict[str, object] = {}

    def fake_run_assistant_request_stream(request: UserRequest, **kwargs) -> AgentRunStream[SimpleNamespace]:
        captured["request"] = request
        captured["kwargs"] = kwargs
        loop = asyncio.get_running_loop()
        stream: AgentRunStream[SimpleNamespace] = AgentRunStream(loop=loop)

        async def publish() -> None:
            stream.emit(
                AgentEvent(
                    type="response_delta",
                    session_id=request.session_id,
                    run_id="assistant-run-1",
                    text="Alpha ",
                    payload={"token_streaming": True, "source": "stream_provider"},
                )
            )
            stream.set_result(_completed_artifacts(request, run_id="assistant-run-1", message="Alpha beta."))

        asyncio.create_task(publish())
        return stream

    backend = AgentGraphRealtimeBackend(run_request_stream=fake_run_assistant_request_stream)
    events: list[RealtimeAgentEvent] = []

    async def collect(event: RealtimeAgentEvent) -> None:
        events.append(event)

    result = asyncio.run(
        backend.run_turn(
            RealtimeAgentRequest(user_id="user-1", session_id="session-1", text="hello"),
            event_sink=collect,
        )
    )

    assert result.status == "completed"
    assert captured["kwargs"]["load_env"] is True
    assert captured["kwargs"]["enable_conversation_history"] is True
    assert "event_sink" not in captured["kwargs"]
    assert [event.type for event in events] == ["response.chunk", "response.final"]
    assert [event.text for event in events] == ["Alpha ", "Alpha beta."]
```

- [x] **Step 2: Verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_agent_backend.py::test_agent_graph_realtime_backend_consumes_run_request_stream_provider -q
```

Expected: failure because `AgentGraphRealtimeBackend.__init__()` does not accept `run_request_stream`.

### Task 2: Backend Stream Migration

**Files:**
- Modify: `src/assistant_agent/realtime/agent_graph_backend.py`

**Interfaces:**
- Produces: `RunAssistantRequestStream` injection type.
- Preserves: existing `run_request=` sync injection.

- [x] **Step 1: Import stream primitives**

Use:

```python
from assistant_agent.agent.event_stream import AgentRunStream, AsyncQueueEventSink
from assistant_agent.services.assistant_run_service import run_assistant_request, run_assistant_request_stream
```

- [x] **Step 2: Add constructor injection**

Add `run_request_stream: RunAssistantRequestStream | None = None` to `AgentGraphRealtimeBackend.__init__()` and store it.

- [x] **Step 3: Add sync compatibility stream wrapper**

Implement a helper that runs a legacy sync `run_request` in `asyncio.to_thread()` with `AsyncQueueEventSink` and returns `AgentRunStream[Any]`.

- [x] **Step 4: Consume stream in `run_turn()`**

Replace the direct `asyncio.to_thread(run_request, ..., event_sink=forwarder)` call with:

```python
stream = self._assistant_request_stream(user_request, cancel_token=cancel_token)
async for agent_event in stream:
    await forwarder.forward_agent_event(agent_event)
artifacts = await stream.result()
```

Do not pass `_RealtimeForwardingEventSink` as the service event sink.

- [x] **Step 5: Convert forwarder to async mapping**

Change `_RealtimeForwardingEventSink` so event forwarding happens in the running loop with `await forward_agent_event(event)` rather than `asyncio.run_coroutine_threadsafe()`.

- [x] **Step 6: Verify focused tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_agent_backend.py -q
```

Expected: all realtime backend tests pass.

### Task 3: Docs And Verification

**Files:**
- Modify: `docs/runtime-event-stream-architecture.md`

**Interfaces:**
- Produces: documentation that Phase 3 moved the realtime backend consumer boundary.

- [x] **Step 1: Update architecture doc**

State that `AgentGraphRealtimeBackend` now consumes `run_assistant_request_stream()` by default, while sync `run_request=` remains a compatibility injection path.

- [x] **Step 2: Run verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_agent_backend.py tests/test_realtime_event_mapping.py tests/test_realtime_backend_types.py tests/test_gateway.py tests/test_gateway_session.py tests/test_gateway_api.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
git diff --check
```

Expected: all commands exit 0.
