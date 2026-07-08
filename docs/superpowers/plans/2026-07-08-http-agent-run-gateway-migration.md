# HTTP Agent Run Gateway Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route HTTP `/agent/run` through Gateway while preserving the existing `AgentRunResponse` schema.

**Architecture:** The HTTP route uses `GatewayTurnFacade` for Gateway lifecycle and an in-process capture id to retrieve the full `AgentRunResponse` produced behind `GatewayAgentAdapter`. Gateway wire frames remain compact; the HTTP schema stays an entry-adapter concern.

**Tech Stack:** FastAPI, Python asyncio, existing Gateway session manager/facade, `GatewayAgentAdapter`, Pydantic API schemas, pytest.

## Global Constraints

- Keep Gateway as the lifecycle boundary for HTTP `/agent/run`.
- Do not expose the full `AgentRunResponse` through Gateway WebSocket `run.end.payload`.
- Do not change `/agents/run`, CLI, or legacy `/ws/agent/{session_id}` in this phase.
- Do not add observer wiring in this phase.
- Do not enable real providers.
- Use TDD: write failing tests before production code.

---

### Task 1: Preserve Execution Strategy Through Realtime Metadata

**Files:**
- Modify: `tests/test_realtime_agent_backend.py`
- Modify: `src/assistant_agent/realtime/agent_graph_backend.py`

**Interfaces:**
- Consumes: `RealtimeAgentRequest.metadata["execution_strategy"]`.
- Produces: `realtime_request_to_user_request()` returns a `UserRequest` with `execution_strategy` set to `"react"` or `"plan_and_solve"`.

- [ ] **Step 1: Write the failing test**

Add a test to `tests/test_realtime_agent_backend.py` that builds a
`RealtimeAgentRequest(metadata={"execution_strategy": "plan_and_solve"})`,
runs `AgentGraphRealtimeBackend`, and asserts the captured `UserRequest` has
`execution_strategy == "plan_and_solve"`.

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_agent_backend.py::test_agent_graph_realtime_backend_preserves_execution_strategy_metadata -q
```

Expected: FAIL because the request falls back to `"react"`.

- [ ] **Step 3: Write minimal implementation**

Add a helper in `agent_graph_backend.py` that maps any value other than
`"plan_and_solve"` to `"react"`, and pass it into `UserRequest(...)`.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_agent_backend.py::test_agent_graph_realtime_backend_preserves_execution_strategy_metadata -q
```

Expected: PASS.

### Task 2: Add HTTP Response Capture to Gateway Runtime

**Files:**
- Modify: `tests/test_gateway_api.py`
- Modify: `src/assistant_agent/api/gateway_runtime.py`

**Interfaces:**
- Produces:
  - `GATEWAY_HTTP_RESPONSE_CAPTURE_ID = "http_response_capture_id"`
  - `new_gateway_http_response_capture_id() -> str`
  - `gateway_http_capture_metadata(capture_id: str) -> dict[str, Any]`
  - `pop_gateway_http_response(capture_id: str) -> AgentRunResponse | None`

- [ ] **Step 1: Write the failing test**

Add a unit test to `tests/test_gateway_api.py` that calls
`gateway_http_capture_metadata()`, runs `_run_assistant_request_with_http_runtime()`
with a `UserRequest` containing that metadata, then asserts
`pop_gateway_http_response(capture_id)` returns an `AgentRunResponse`.

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_api.py::test_gateway_http_runtime_captures_agent_run_response -q
```

Expected: FAIL because capture helpers are not defined.

- [ ] **Step 3: Write minimal implementation**

Add a thread-safe dict in `gateway_runtime.py`. In
`_run_assistant_request_with_http_runtime()`, call `artifacts.api_response()`
and store it when the request metadata contains the capture id.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_api.py::test_gateway_http_runtime_captures_agent_run_response -q
```

Expected: PASS.

### Task 3: Migrate `/agent/run` to Gateway

**Files:**
- Modify: `tests/test_api_agent_graph_runtime.py`
- Modify: `tests/test_phase7c_web_productization.py`
- Modify: `src/assistant_agent/api/routes_agent.py`
- Modify: `docs/gateway-architecture.md`

**Interfaces:**
- Consumes: `gateway_runtime.get_gateway_turn_facade()`, capture helpers, and `GatewayTurnRequest`.
- Produces: HTTP `/agent/run` returns the captured `AgentRunResponse` after the Gateway turn reaches `run.end`.

- [ ] **Step 1: Write the failing test**

Extend `test_api_agent_run_defaults_to_graph_runtime` to assert the captured
runtime request includes `metadata["runtime"]["history"] == ["你好"]`. This
proves the request passed through `GatewaySessionService`.

Add or keep an API test asserting a request with
`execution_strategy="plan_and_solve"` returns `"plan_and_solve"`.

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_api_agent_graph_runtime.py::test_api_agent_run_defaults_to_graph_runtime tests/test_phase7c_web_productization.py::test_agent_run_accepts_explicit_plan_and_solve_strategy -q
```

Expected: FAIL on missing Gateway runtime metadata before migration.

- [ ] **Step 3: Write minimal implementation**

Change `run_agent()` to `async def`, create a capture id, merge HTTP capture
metadata into request metadata, copy `request.execution_strategy`, call
`GatewayTurnFacade.run_turn()`, then return the popped captured response.

- [ ] **Step 4: Run focused verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_api_agent_graph_runtime.py tests/test_phase6b_api_demo_contract.py tests/test_phase7c_web_productization.py::test_agent_run_accepts_explicit_plan_and_solve_strategy tests/test_gateway_api.py tests/test_gateway_turn_facade.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Update architecture doc**

Update `docs/gateway-architecture.md` to say HTTP `/agent/run` now uses
`GatewayTurnFacade` plus HTTP response capture.

### Task 4: Final Verification and Commit

**Files:**
- All files changed in Tasks 1-3.

- [ ] **Step 1: Run Gateway and API verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway.py tests/test_gateway_turn_facade.py tests/test_gateway_session.py tests/test_gateway_api.py tests/test_realtime_agent_backend.py tests/test_realtime_event_mapping.py tests/test_realtime_backend_types.py tests/test_api_agent_graph_runtime.py tests/test_phase6b_api_demo_contract.py tests/test_session_api.py tests/test_trace_query_api.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run fast suite**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

Expected: all fast tests pass.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-07-08-http-agent-run-gateway-migration-design.md docs/superpowers/plans/2026-07-08-http-agent-run-gateway-migration.md docs/gateway-architecture.md src/assistant_agent/api/gateway_runtime.py src/assistant_agent/api/routes_agent.py src/assistant_agent/realtime/agent_graph_backend.py tests/test_gateway_api.py tests/test_api_agent_graph_runtime.py tests/test_realtime_agent_backend.py
git commit -m "feat: route agent run through gateway"
```

## Self-Review

- Spec coverage: Tasks preserve execution strategy, add capture, migrate HTTP, and verify Gateway/API behavior.
- Placeholder scan: no TBD/TODO placeholders.
- Scope check: CLI, legacy WebSocket, `/agents/run`, and observers remain explicitly out of scope.
