# Legacy WebSocket Gateway Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route legacy `/ws/agent/{session_id}` through Gateway while preserving its current `AgentEvent` WebSocket stream.

**Architecture:** The WebSocket route creates a local `GatewaySessionManager` and `GatewayTurnFacade` per connection. A Gateway backend callback wraps the Gateway realtime event sink with a mirror sink that also forwards original `AgentEvent` objects to the legacy WebSocket queue.

**Tech Stack:** FastAPI WebSocket, Python asyncio/thread-safe queue handoff, existing Gateway session manager/facade, `GatewayAgentAdapter`, `AssistantRuntimeApp`, pytest.

## Global Constraints

- Preserve the external `/ws/agent/{session_id}` JSON event contract.
- Do not change `/ws/gateway`, `/ws/realtime/media`, or `/agent-service/v1`.
- Do not change remote `scripts/run_client.py` protocol.
- Do not add observer wiring.
- Do not enable real providers.
- Use TDD: write failing tests before production code.

---

### Task 1: Prove Legacy WebSocket Enters Gateway

**Files:**
- Modify: `tests/test_websocket_graph_runtime.py`
- Modify: `src/assistant_agent/api/websocket.py`

**Interfaces:**
- Runtime `UserRequest.metadata["runtime"]["history"]` is present for `/ws/agent` runs.
- Existing final `agent_response` event remains unchanged.

- [ ] **Step 1: Write the failing test**

Extend `test_websocket_ignores_auth_headers_when_disabled` or add a new
recording-runtime WebSocket test asserting
`runtime.requests[0].metadata["runtime"]["history"] == ["你好"]`.

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_websocket_graph_runtime.py::test_websocket_ignores_auth_headers_when_disabled -q
```

Expected: FAIL with `KeyError: 'runtime'`.

- [ ] **Step 3: Implement minimal Gateway-backed WebSocket path**

In `src/assistant_agent/api/websocket.py`:

- import `GatewaySessionManager`, `GatewayAgentAdapter`,
  `GatewayTurnFacade`, and `GatewayTurnRequest`;
- replace the worker-thread direct `AssistantRuntimeApp.run_request()` call with
  an async Gateway turn task;
- add a mirror sink that forwards each `AgentEvent` to both the Gateway sink and
  the legacy WebSocket queue;
- capture assistant artifacts from the Gateway backend callback;
- after `GatewayTurnFacade.run_turn()`, send the legacy final
  `agent_response` event;
- close the local Gateway manager in `finally`.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_websocket_graph_runtime.py::test_websocket_ignores_auth_headers_when_disabled -q
```

Expected: PASS.

### Task 2: Preserve Existing WebSocket Stream Contract

**Files:**
- Modify: `src/assistant_agent/api/websocket.py`
- Test: `tests/test_websocket_graph_runtime.py`, `tests/test_websocket_error_events.py`, `tests/integration/test_websocket_events.py`

**Interfaces:**
- Existing raw `AgentEvent` stream remains available.
- Final `agent_response.payload.response` remains the full `AgentRunResponse`.

- [ ] **Step 1: Run WebSocket tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_websocket_graph_runtime.py tests/test_websocket_error_events.py tests/integration/test_websocket_events.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Fix only compatibility regressions**

If tests fail, preserve the old event names and payload shape. Do not change
client protocol in this phase.

### Task 3: Update Docs and Verify

**Files:**
- Modify: `docs/gateway-architecture.md`

- [ ] **Step 1: Update docs**

Document that legacy `/ws/agent/{session_id}` now uses Gateway internally while
retaining legacy `AgentEvent` JSON externally.

- [ ] **Step 2: Run Gateway/WebSocket verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway.py tests/test_gateway_turn_facade.py tests/test_gateway_session.py tests/test_gateway_api.py tests/test_realtime_agent_backend.py tests/test_realtime_event_mapping.py tests/test_realtime_backend_types.py tests/test_websocket_graph_runtime.py tests/test_websocket_error_events.py tests/integration/test_websocket_events.py -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Run fast suite**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

Expected: all fast tests pass.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-07-08-legacy-websocket-gateway-migration-design.md docs/superpowers/plans/2026-07-08-legacy-websocket-gateway-migration.md docs/gateway-architecture.md src/assistant_agent/api/websocket.py tests/test_websocket_graph_runtime.py
git commit -m "feat: route legacy websocket through gateway"
```

## Self-Review

- Spec coverage: legacy WebSocket is Gateway-backed while keeping old wire events.
- Placeholder scan: no TBD/TODO placeholders.
- Scope check: normalized Gateway WebSocket, media WebSocket, vendor route, remote CLI protocol, and observers remain out of scope.
