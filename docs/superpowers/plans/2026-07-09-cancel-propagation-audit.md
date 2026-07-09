# Cancel Propagation Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tighten cooperative cancellation propagation without rewriting Gateway, Runtime, Tool, Memory, or Provider internals.

**Architecture:** Keep the existing `CancelToken`/`raise_if_cancelled()` cooperative model. Extend token detection to raw event-like objects with `is_set()` so `asyncio.Event`-style tokens are understood. Replace tool retry backoff's single blocking `time.sleep()` with a small cancel-aware sleep loop.

**Tech Stack:** Python 3.12, asyncio, pytest, existing `AgentRunCancelled`, existing `ToolExecutor`, existing Gateway `CancelToken`.

## Global Constraints

- Do not forcefully kill blocking SDK calls, subprocesses, or provider requests in this phase.
- Do not rewrite Tool, Memory, Provider, Gateway, or Runtime loops.
- Preserve current cancellation metadata and `AgentRunCancelled` error shape.
- Keep cancellation cooperative and testable.
- Use TDD: write failing tests before production code.

---

### Task 1: Raw Event Token Support

**Files:**
- Modify: `tests/test_agent_runtime_cancellation.py`
- Modify: `src/assistant_agent/agent/cancellation.py`

**Interfaces:**
- Consumes: raw event-like cancel token with `is_set()`.
- Produces: `is_cancelled(cancel_token)` recognizes set event-like tokens.

- [x] **Step 1: Write failing test**

Add:

```python
def test_runtime_accepts_raw_asyncio_event_cancel_token() -> None:
    cancel_event = asyncio.Event()
    cancel_event.set()

    state = AgentGraphRuntime().run_state(
        UserRequest(user_id="u1", session_id="s1", text="hello"),
        cancel_token=cancel_event,
    )

    assert state.status == "cancelled"
    assert state.errors[-1].details["cancel_phase"] == "pre_graph"
```

- [x] **Step 2: Verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_runtime_cancellation.py::test_runtime_accepts_raw_asyncio_event_cancel_token -q
```

Expected: failure because `is_cancelled()` ignores raw `is_set()` tokens.

- [x] **Step 3: Implement support**

Update `is_cancelled()`:

```python
is_set = getattr(cancel_token, "is_set", None)
if callable(is_set):
    return bool(is_set())
```

- [x] **Step 4: Verify GREEN**

Run the same single test and expect PASS.

### Task 2: Interruptible Tool Retry Backoff

**Files:**
- Modify: `tests/test_tool_executor.py`
- Modify: `src/assistant_agent/agent/tool_executor.py`

**Interfaces:**
- Consumes: existing `ProviderExecutionPolicy.retry.backoff_seconds`.
- Produces: backoff sleep that wakes promptly when `cancel_token` is set.

- [x] **Step 1: Write failing test**

Add a retrying tool test that starts a background timer setting the token during retry backoff and asserts the executor raises before the full backoff duration elapses.

- [x] **Step 2: Verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_tool_executor.py::test_tool_executor_retry_backoff_wakes_when_cancelled -q
```

Expected: failure because current `time.sleep(backoff_seconds)` blocks until the full backoff expires.

- [x] **Step 3: Implement cancel-aware sleep**

Replace the single retry `sleep(backoff_seconds)` with a helper that sleeps in short chunks and calls `raise_if_cancelled()` between chunks.

- [x] **Step 4: Verify GREEN**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_tool_executor.py::test_tool_executor_retry_backoff_wakes_when_cancelled -q
```

Expected: PASS.

### Task 3: Docs And Verification

**Files:**
- Modify: `docs/runtime-event-stream-architecture.md`

**Interfaces:**
- Produces: Phase 4 status and remaining limitations.

- [x] **Step 1: Document Phase 4**

Document that Phase 4 extends cooperative cancellation but does not forcefully terminate blocking external calls.

- [x] **Step 2: Run verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_runtime_cancellation.py tests/test_tool_executor.py tests/test_tool_call_boundaries.py tests/test_tool_risk_gate.py tests/test_realtime_agent_backend.py tests/test_gateway_session.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
git diff --check
```

Expected: all commands exit 0.
