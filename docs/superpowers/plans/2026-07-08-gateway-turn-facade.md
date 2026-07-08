# Gateway Turn Facade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a synchronous Gateway turn facade so non-WebSocket entries can route one request/response turn through Gateway lifecycle semantics.

**Architecture:** `GatewayTurnFacade` depends on `GatewaySessionManager`, sends one `message.user` frame, and collects frames until `run.end`. The FastAPI-owned `gateway_runtime` module exposes a process-local facade factory beside the existing manager and bridge.

**Tech Stack:** Python dataclasses, asyncio, existing `assistant_agent.gateway` endpoint/session primitives, pytest.

## Global Constraints

- Keep Gateway as the lifecycle boundary for future Web / CLI / HTTP / WebSocket routing.
- Keep `AssistantRuntimeApp` behind `GatewayAgentAdapter`; do not add a second agent loop.
- Do not migrate `/agent/run`, `/ws/agent`, or CLI in this phase.
- Do not enable real providers.
- Use TDD: write failing tests before production code.

---

### Task 1: Add Gateway Turn Facade

**Files:**
- Create: `tests/test_gateway_turn_facade.py`
- Create: `src/assistant_agent/services/gateway_turn_facade.py`

**Interfaces:**
- Consumes: `GatewaySessionManager.acquire(user_id, config)`, Gateway `frame()`, and Gateway `Endpoint`.
- Produces:
  - `GatewayTurnRequest(user_id: str, session_id: str, text: str, image_ids: list[str] = ..., video_ids: list[str] = ..., audio_id: str | None = ..., metadata: dict[str, Any] = ..., config: dict[str, Any] = ..., timeout_s: float = 30.0)`
  - `GatewayTurnResult(frames: list[Frame], terminal_frame: Frame, response_text: str, status: str, reason: str, run_id: str | None, turn_id: str | None, trace_id: str | None, payload: dict[str, Any])`
  - `GatewayTurnFacade(manager: GatewaySessionManager).run_turn(request: GatewayTurnRequest) -> GatewayTurnResult`
  - `GatewayTurnTimeout` and `GatewayTurnError`.

- [ ] **Step 1: Write the failing test**

Add `tests/test_gateway_turn_facade.py`:

```python
from __future__ import annotations

import unittest

from assistant_agent.gateway import GatewaySessionManager
from assistant_agent.realtime import RealtimeAgentEvent, RealtimeAgentResult
from assistant_agent.services.gateway_turn_facade import GatewayTurnFacade, GatewayTurnRequest


class RecordingRealtimeBackend:
    def __init__(self) -> None:
        self.requests = []

    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        self.requests.append(request)
        assert event_sink is not None
        await event_sink(RealtimeAgentEvent(type="response.chunk", text="hello via gateway"))
        return RealtimeAgentResult(
            status="completed",
            run_id=request.run_id,
            trace_id="trace-turn-1",
            response_text="hello via gateway",
            expects_reply=True,
        )


class GatewayTurnFacadeTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_turn_collects_gateway_frames_and_backend_request(self) -> None:
        backend = RecordingRealtimeBackend()
        manager = GatewaySessionManager(backend_factory=lambda: backend, start_reaper=False)
        facade = GatewayTurnFacade(manager=manager)

        try:
            result = await facade.run_turn(
                GatewayTurnRequest(
                    user_id="user-1",
                    session_id="session-1",
                    text="hello",
                    metadata={"source": "http_gateway_turn"},
                    config={"tone": "concise"},
                    timeout_s=1,
                )
            )
        finally:
            await manager.close()

        assert [frame["type"] for frame in result.frames] == [
            "run.started",
            "stream.chunk",
            "run.end",
        ]
        assert result.status == "completed"
        assert result.reason == "completed"
        assert result.response_text == "hello via gateway"
        assert result.trace_id == "trace-turn-1"
        assert backend.requests[0].text == "hello"
        assert backend.requests[0].metadata["gateway"]["history"] == ["hello"]
        assert backend.requests[0].metadata["gateway"]["session_config"] == {"tone": "concise"}

    async def test_run_turn_returns_gateway_error_terminal_result(self) -> None:
        class ErrorBackend:
            async def run_turn(self, request, *, event_sink=None, cancel_token=None):
                return RealtimeAgentResult(
                    status="error",
                    run_id=request.run_id,
                    metadata={"error_message": "backend failed", "error_type": "RuntimeError"},
                )

        manager = GatewaySessionManager(backend_factory=ErrorBackend, start_reaper=False)
        facade = GatewayTurnFacade(manager=manager)

        try:
            result = await facade.run_turn(
                GatewayTurnRequest(user_id="user-1", session_id="session-err", text="fail", timeout_s=1)
            )
        finally:
            await manager.close()

        assert result.status == "error"
        assert result.reason == "error"
        assert result.terminal_frame["error"]["message"] == "backend failed"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_turn_facade.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'assistant_agent.services.gateway_turn_facade'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/assistant_agent/services/gateway_turn_facade.py` with dataclasses, error classes, frame construction, frame collection, stream chunk text assembly, and timeout handling.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_turn_facade.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-07-08-gateway-first-entry-unification-design.md docs/superpowers/plans/2026-07-08-gateway-turn-facade.md tests/test_gateway_turn_facade.py src/assistant_agent/services/gateway_turn_facade.py
git commit -m "feat: add gateway turn facade"
```

### Task 2: Expose Process-Local Facade and Update Gateway Docs

**Files:**
- Modify: `src/assistant_agent/api/gateway_runtime.py`
- Modify: `docs/gateway-architecture.md`
- Test: `tests/test_gateway_api.py`

**Interfaces:**
- Consumes: `GatewayTurnFacade`.
- Produces: `get_gateway_turn_facade()` and `create_gateway_turn_facade()`.

- [ ] **Step 1: Write the failing test**

Add a focused assertion to `tests/test_gateway_api.py` that imports
`gateway_runtime.get_gateway_turn_facade()`, installs a test manager with
`set_gateway_runtime_for_tests()`, and verifies the returned facade uses the
installed manager by running a turn through the fake backend.

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_api.py::test_gateway_turn_facade_uses_process_local_manager -q
```

Expected: FAIL because `get_gateway_turn_facade` is not defined.

- [ ] **Step 3: Write minimal implementation**

Add `_GATEWAY_TURN_FACADE`, `get_gateway_turn_facade()`, and
`create_gateway_turn_facade()` to `gateway_runtime.py`. Reset the facade in
`set_gateway_runtime_for_tests()`, `shutdown_gateway_runtime()`, and
`reset_gateway_runtime_for_tests()`.

- [ ] **Step 4: Update architecture doc**

Update `docs/gateway-architecture.md` so the target non-realtime path points to
Gateway first and `AssistantRuntimeApp` is documented as the backend runtime
boundary behind `GatewayAgentAdapter`.

- [ ] **Step 5: Run focused verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_turn_facade.py tests/test_gateway_api.py tests/test_gateway_session.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/assistant_agent/api/gateway_runtime.py docs/gateway-architecture.md tests/test_gateway_api.py
git commit -m "feat: expose gateway turn facade"
```

## Self-Review

- Spec coverage: Task 1 implements the sync turn facade; Task 2 exposes it through process-local Gateway runtime ownership and corrects Gateway docs.
- Placeholder scan: no TBD/TODO placeholders.
- Scope check: endpoint migration is explicitly deferred to the next phase.
