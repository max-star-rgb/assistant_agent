# Agent Service Gateway Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route `/agent-service/v1` chat messages through Gateway while preserving the vendor compatibility WebSocket envelope.

**Architecture:** The route keeps vendor protocol parsing at the entry layer. Each accepted v1 WebSocket owns a local `GatewaySessionManager` and `GatewayTurnFacade`; `chat` maps the latest speech text to one `GatewayTurnRequest`, then wraps the Gateway result in the existing `chatResponse` body.

**Tech Stack:** FastAPI WebSocket, Python asyncio, existing Gateway session manager/facade, `GatewayAgentAdapter`, `AssistantRuntimeApp`, pytest.

## Global Constraints

- Preserve external vendor message names: `assistantControlStartAck`, `chatResponse`, and `error`.
- Preserve stringified `body` envelopes.
- Do not change `/ws/gateway`, `/ws/realtime/media`, `/ws/agent/{session_id}`, or HTTP `/agent/run`.
- Do not enable real providers.
- Use TDD: write failing tests before production code.

---

### Task 1: Prove Agent Service Chat Enters Gateway

**Files:**
- Modify: `tests/test_agent_service_websocket.py`
- Modify: `src/assistant_agent/api/agent_service_websocket.py`

**Interfaces:**
- Consumes: `GatewayTurnFacade.run_turn(GatewayTurnRequest(...))`.
- Produces: `ChatHandler.handle(...) -> chatResponse` backed by Gateway.

- [ ] **Step 1: Write the failing test**

Add a recording runtime and a Gateway assertion:

```python
from assistant_agent.agent.state import AgentState
from assistant_agent.api import routes_agent
from assistant_agent.schemas.requests import AgentResponse


class RecordingRuntime:
    def __init__(self) -> None:
        self.requests = []

    def run_state(self, request):
        self.requests.append(request)
        state = AgentState.from_request(request, run_id="run_agent_service_gateway_test")
        state.set_response(AgentResponse(message="agent service gateway response"))
        return state


def test_agent_service_chat_runs_through_gateway(monkeypatch) -> None:
    runtime = RecordingRuntime()
    monkeypatch.setattr(routes_agent, "get_agent_runtime", lambda: runtime)
    client = TestClient(create_app())

    with client.websocket_connect("/agent-service/v1?sessionId=s1") as websocket:
        websocket.send_json(
            _envelope(
                "chat",
                "s1",
                {
                    "chatIndex": 2,
                    "userNumber": "10086",
                    "contents": [
                        {
                            "speakerNumber": "10086",
                            "speechContent": "你好",
                            "time": "2026-07-06T10:00:00+08:00",
                        }
                    ],
                },
            )
        )
        response = websocket.receive_json()

    body = _body(response)
    assert response["message"] == "chatResponse"
    assert body["message"]["content"] == "agent service gateway response"
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.user_id == "10086"
    assert request.session_id == "s1"
    assert request.text == "你好"
    assert request.metadata["runtime"]["history"] == ["你好"]
    assert request.metadata["transport"] == "agent_service_websocket"
    assert request.metadata["agent_service"]["chat_index"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_service_websocket.py::test_agent_service_chat_runs_through_gateway -q
```

Expected: FAIL because the current route returns a mock response and does not
call the recording runtime.

- [ ] **Step 3: Implement minimal Gateway-backed chat**

In `src/assistant_agent/api/agent_service_websocket.py`:

- import `GatewaySessionManager`, `GatewayAgentAdapter`,
  `GatewayTurnFacade`, and `GatewayTurnRequest`;
- add optional `gateway_manager` and `gateway_facade` to
  `AgentServiceConnectionState`;
- after accepting supported v1 connections, create a local manager/facade;
- close the manager in `finally`;
- add a small backend callback that delegates to
  `routes_agent.get_assistant_runtime_app().run_request(...)`;
- in `ChatHandler.handle()`, after validation, call
  `state.gateway_facade.run_turn(...)`;
- use `turn.response_text` for `chatResponse.body.message.content`;
- on Gateway error, return `chatResponse` failure body.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_service_websocket.py::test_agent_service_chat_runs_through_gateway -q
```

Expected: PASS.

### Task 2: Preserve Vendor Compatibility Tests

**Files:**
- Modify: `tests/test_agent_service_websocket.py`
- Modify: `src/assistant_agent/api/agent_service_websocket.py`

**Interfaces:**
- `assistantControlStart` still returns `assistantControlStartAck`.
- Validation failures still return `code="FAIL"` inside the stringified body.
- `chatResponse` still contains `number`, `message.chatIndex`, and
  `message.content`.

- [ ] **Step 1: Run existing agent-service tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_service_websocket.py -q
```

Expected: selected tests pass after updating the old mock-response assertion to
expect a runtime-backed response contract.

- [ ] **Step 2: Fix only compatibility regressions**

If tests fail, preserve the vendor envelope and stringified body contract. Do
not expose Gateway frames on `/agent-service/v1`.

### Task 3: Update Docs and Boundary Guards

**Files:**
- Modify: `docs/gateway-architecture.md`
- Modify: `tests/test_architecture_boundaries.py`

**Interfaces:**
- `src/assistant_agent/api/agent_service_websocket.py` must not import or
  instantiate `AgentGraphRuntime`.
- Gateway architecture docs must say `/agent-service/v1` chat now enters Gateway
  while preserving vendor envelopes.

- [ ] **Step 1: Add architecture boundary guard**

Add `src/assistant_agent/api/agent_service_websocket.py` to
`test_product_entry_layers_do_not_import_agent_graph_runtime_directly()` and
`test_product_entry_layers_depend_on_runtime_app_boundary()`.

- [ ] **Step 2: Update Gateway docs**

In `docs/gateway-architecture.md`, replace the current statement that
`/agent-service/v1` returns mock envelopes without Gateway/runtime with a
statement that chat enters Gateway internally and handshake/validation remain
entry-layer compatibility behavior.

- [ ] **Step 3: Run Gateway/WebSocket verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_architecture_boundaries.py tests/test_agent_service_websocket.py tests/test_gateway.py tests/test_gateway_turn_facade.py tests/test_gateway_session.py tests/test_gateway_api.py tests/test_realtime_agent_backend.py tests/test_realtime_event_mapping.py tests/test_realtime_backend_types.py tests/test_websocket_graph_runtime.py tests/test_websocket_error_events.py tests/integration/test_websocket_events.py -q
```

Expected: all selected tests pass.

- [ ] **Step 4: Run fast suite**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

Expected: all fast tests pass.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-07-08-agent-service-gateway-migration-design.md docs/superpowers/plans/2026-07-08-agent-service-gateway-migration.md docs/gateway-architecture.md src/assistant_agent/api/agent_service_websocket.py tests/test_agent_service_websocket.py tests/test_architecture_boundaries.py
git commit -m "feat: route agent service websocket through gateway"
```

## Self-Review

- Spec coverage: `/agent-service/v1` chat migrates through Gateway; external
  vendor envelope remains stable.
- Placeholder scan: no unresolved placeholders.
- Scope check: no observer wiring, no real providers, no unrelated WebSocket or
  HTTP route changes.
