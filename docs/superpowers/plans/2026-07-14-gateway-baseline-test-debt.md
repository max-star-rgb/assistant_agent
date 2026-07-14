# Gateway Baseline Test Debt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore a green repository baseline by aligning stale Phase 1 tests with the current Gateway wire contract and making agent-service disconnect cleanup survive ASGI cancel-scope cancellation.

**Architecture:** Current Gateway production behavior remains authoritative for `run.queued` and versioned cancellation metadata. The agent-service route will move its existing connection cleanup into one async finalizer executed inside an AnyIO shielded cancel scope, preserving cancellation propagation while allowing chat tasks, video resources, and the local Gateway manager to close.

**Tech Stack:** Python 3.12, asyncio, AnyIO through FastAPI, pytest, FastAPI/Starlette TestClient.

## Global Constraints

- Work only in `fix/gateway-baseline-tests` until its full suite is green.
- Do not change Gateway queue or cancellation production semantics to satisfy stale tests.
- Do not add sleeps, retries, or exception swallowing to hide the WebSocket race.
- Keep default tests mock/local/offline and do not invoke a real Provider.
- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for Python commands.
- Commit the test-contract correction and production cleanup correction separately.

---

### Task 1: Align Phase 1 deep-gate assertions with the current Gateway contract

**Files:**

- Modify: `tests/test_phase1_realtime_loop_deep_gate.py`

**Interfaces:**

- Consumes: current `run.queued` payload and `RealtimeTurnCancellationContract` projection.
- Produces: regression assertions matching the canonical `docs/gateway-architecture.md` queue and cancellation sections.

- [ ] **Step 1: Preserve the observed red baseline**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase1_realtime_loop_deep_gate.py -q
```

Expected: two failures—one because `run.queued` is emitted, one because the cancellation payload now uses `phase=final_streaming` plus versioned safety fields.

- [ ] **Step 2: Update the queue-flow test to consume the public queued frame**

Add a one-frame reader and replace the obsolete no-frame assertion:

```python
async def _read_one_frame(client_ep):
    async for received in client_ep:
        return received
    raise AssertionError("endpoint closed before the next Gateway frame")

queued = await asyncio.wait_for(_read_one_frame(client_ep), timeout=1.0)
frames.append(queued)
assert queued["type"] == "run.queued"
assert queued["payload"]["reason"] == "session_busy"
```

Add `run.queued` to the expected ordered frame sequence. Do not change production queue behavior.

- [ ] **Step 3: Update the cancellation assertion to the versioned contract**

Assert the current prompt-safe payload exactly:

```python
assert run_end["payload"]["cancel"] == {
    "source": "gateway_cancel",
    "reason": "user_requested_stop",
    "phase": "final_streaming",
    "best_effort": True,
    "cancelled_by": "run.cancel",
    "stale_outputs": True,
    "can_reuse_tool_result": False,
    "speakable": False,
}
```

- [ ] **Step 4: Verify and commit the contract correction**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase1_realtime_loop_deep_gate.py -q
```

Expected: `4 passed`.

Commit:

```bash
git add tests/test_phase1_realtime_loop_deep_gate.py
git commit -m "test: align phase1 gateway contract assertions"
```

---

### Task 2: Make agent-service connection cleanup cancellation-safe

**Files:**

- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Modify: `tests/test_agent_service_websocket.py`

**Interfaces:**

- Consumes: `AgentServiceConnectionState`, `GatewaySessionManager`, WebSocket disconnect close code/reason.
- Produces: cancellation-safe `_close_agent_service_connection(state, gateway_manager, close_code, close_reason) -> None`, invoked from the route `finally` block.

- [ ] **Step 1: Add a deterministic failing finalizer test**

Import `CancelScope` from `anyio`. Create a test whose outer AnyIO scope is already cancelled before calling the wished-for helper. Use fake observer, ingestion, manager, and one pending chat task. The test must assert that all cleanup stages still ran:

```python
def test_agent_service_connection_cleanup_is_shielded_from_outer_cancel() -> None:
    async def scenario() -> None:
        events: list[str] = []

        async def pending_chat() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                events.append("chat_cancelled")

        class Observer:
            async def close(self) -> None:
                await asyncio.sleep(0)
                events.append("observer_closed")

        class Ingestion:
            def cleanup(self, video_id: str) -> None:
                assert video_id == "video-1"
                events.append("video_cleaned")

        class Manager:
            async def close(self) -> None:
                await asyncio.sleep(0)
                events.append("gateway_closed")

        chat_task = asyncio.create_task(pending_chat())
        await asyncio.sleep(0)
        state = agent_service_ws.AgentServiceConnectionState(
            session_id="s1",
            query_params={},
            video_ids=["video-1"],
            video_ingestion=Ingestion(),
            video_observer=Observer(),
            chat_tasks={chat_task},
        )
        manager = Manager()
        with CancelScope() as outer:
            outer.cancel()
            await agent_service_ws._close_agent_service_connection(
                state=state,
                gateway_manager=manager,
                close_code=1000,
                close_reason=None,
            )
        assert events == ["chat_cancelled", "observer_closed", "video_cleaned", "gateway_closed"]
        assert state.closed is True

    asyncio.run(scenario())
```

- [ ] **Step 2: Run the focused test and verify red**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_service_websocket.py::test_agent_service_connection_cleanup_is_shielded_from_outer_cancel -q
```

Expected: fail because `_close_agent_service_connection` is absent or because outer cancellation prevents cleanup.

- [ ] **Step 3: Extract the existing cleanup without changing its order**

Import `CancelScope` from `anyio`, then add:

```python
async def _close_agent_service_connection(
    *,
    state: AgentServiceConnectionState,
    gateway_manager: GatewaySessionManager,
    close_code: int | None,
    close_reason: str | None,
) -> None:
    with CancelScope(shield=True):
        state.closed = True
        for delivery in state.delivery_registry.pending():
            state.delivery_registry.mark_disconnected(
                delivery.delivery_id,
                close_code=close_code,
                close_reason=close_reason,
            )
        for task in list(state.chat_tasks):
            task.cancel()
        if state.chat_tasks:
            await asyncio.gather(*state.chat_tasks, return_exceptions=True)
        if state.video_observer is not None:
            await state.video_observer.close()
        if state.video_ingestion is not None:
            for video_id in state.video_ids:
                await asyncio.to_thread(state.video_ingestion.cleanup, video_id)
        await gateway_manager.close()
```

In the route finalizer, delegate to the helper:

```python
await _close_agent_service_connection(
    state=state,
    gateway_manager=gateway_manager,
    close_code=locals().get("close_code"),
    close_reason=locals().get("close_reason"),
)
```

Do not catch or suppress `CancelledError`; external cancellation continues after shielded cleanup.

- [ ] **Step 4: Verify the deterministic test and previously flaky tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_service_websocket.py::test_agent_service_connection_cleanup_is_shielded_from_outer_cancel tests/test_agent_service_websocket.py::test_agent_service_video_context_reaches_following_chat tests/test_agent_service_websocket.py::test_agent_service_video_ack_continues_while_chat_is_running -q
```

Expected: all pass and video cleanup completes before the TestClient context returns.

- [ ] **Step 5: Run the full agent-service test file repeatedly**

Run the file once normally, then the two historical failures repeatedly to detect timing regressions. Expected: no `concurrent.futures.CancelledError` and no missed cleanup.

- [ ] **Step 6: Commit the lifecycle fix**

```bash
git add src/assistant_agent/api/agent_service_websocket.py tests/test_agent_service_websocket.py
git commit -m "fix: shield agent service disconnect cleanup"
```

---

### Task 3: Verify baseline and integrate in dependency order

**Files:**

- Create: `docs/superpowers/plans/2026-07-14-gateway-baseline-test-debt.md`
- No additional production files unless a failing test proves a scoped regression.

**Interfaces:**

- Produces: a clean `fix/gateway-baseline-tests` branch that can merge before `feature/realtime-semantic-interrupt`.

- [ ] **Step 1: Run Gateway and agent-service regressions**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway.py tests/test_gateway_session.py tests/test_gateway_api.py tests/test_phase1_realtime_loop_deep_gate.py tests/test_agent_service_websocket.py -q
```

- [ ] **Step 2: Run fast and full repository suites**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

Expected: both exit zero without real Provider calls.

- [ ] **Step 3: Run environment and diff checks**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
git diff --check
git status --short
```

- [ ] **Step 4: Commit this execution plan with the verified fixes**

```bash
git add docs/superpowers/plans/2026-07-14-gateway-baseline-test-debt.md
git commit -m "docs: record gateway baseline repair plan"
```

- [ ] **Step 5: Merge in dependency order**

From the main repository checkout, merge `fix/gateway-baseline-tests` into `cqy`, verify the full suite, then merge `feature/realtime-semantic-interrupt` into `cqy` and rerun targeted, fast, and full suites. Only remove owned worktrees and branches after both merged results pass.

## Execution record before integration

- Phase 1 deep-gate file: `4 passed`.
- Agent-service WebSocket file: `17 passed`.
- Historical video/chat disconnect cases: repeated command completed without failure.
- Gateway/agent-service combined regression: `83 passed`.
- Fast suite: `178 passed, 1755 deselected`.
- Full repository suite: `1927 passed, 6 skipped, 6 subtests passed`.
- Environment check: Python 3.12.13, no missing imports or paths, `ok=true`.
- No real Provider was invoked.
