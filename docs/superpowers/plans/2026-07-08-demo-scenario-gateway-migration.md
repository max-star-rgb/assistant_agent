# Demo Scenario Gateway Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route offline demo scenarios and CLI `--scenario` through Gateway without changing their output contract.

**Architecture:** `scripts/run_demo_flows.py` will become a Gateway-backed entry adapter. Each scenario uses a local `GatewaySessionManager`, `GatewayTurnFacade`, `GatewayAgentAdapter`, and `AssistantRuntimeApp` callback that captures `AssistantRunArtifacts` for the existing summary payload.

**Tech Stack:** Python asyncio, existing Gateway session manager/facade, `GatewayAgentAdapter`, `AssistantRuntimeApp`, pytest.

## Global Constraints

- Preserve the existing `run_demo_flows()` and CLI JSON output contract.
- Do not change demo scenario data files.
- Do not migrate `scripts/run_evals.py` through Gateway in this phase.
- Do not enable real providers.
- Use TDD: write failing tests before production code.

---

### Task 1: Prove Demo Scenarios Enter Gateway

**Files:**
- Modify: `tests/test_e2e_demo_runner.py`
- Modify: `scripts/run_demo_flows.py`

**Interfaces:**
- Consumes: `GatewayTurnFacade.run_turn(GatewayTurnRequest(...))`.
- Produces: `run_scenario(scenario: dict[str, Any]) -> dict[str, Any]` backed by Gateway.

- [ ] **Step 1: Write the failing test**

Add a recording-runtime test to `tests/test_e2e_demo_runner.py`:

```python
from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.requests import AgentResponse
import scripts.run_demo_flows as runner


class RecordingRuntime:
    def __init__(self) -> None:
        self.requests = []

    def run_state(self, request):
        self.requests.append(request)
        state = AgentState.from_request(request, run_id="run_demo_gateway_test")
        state.set_response(AgentResponse(message="demo gateway response"))
        return state


def test_demo_runner_runs_scenarios_through_gateway(monkeypatch) -> None:
    runtime = RecordingRuntime()
    monkeypatch.setattr(runner, "create_runtime", lambda **kwargs: runtime, raising=False)

    summary = runner.run_demo_flows("product_search_compare")

    result = summary["results"][0]
    assert result["run_id"] == "run_demo_gateway_test"
    assert result["response_text"] == "demo gateway response"
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.metadata["runtime"]["history"] == [request.text]
    assert request.metadata["offline"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_e2e_demo_runner.py::test_demo_runner_runs_scenarios_through_gateway -q
```

Expected: FAIL because the current demo runner does not call
`scripts.run_demo_flows.create_runtime`.

- [ ] **Step 3: Implement minimal Gateway-backed demo run**

In `scripts/run_demo_flows.py`:

- remove the direct `AgentGraphRuntime` import;
- import `asyncio`, `GatewaySessionManager`, `GatewayAgentAdapter`,
  `AssistantRuntimeApp`, `create_runtime`, `GatewayTurnFacade`, and
  `GatewayTurnRequest`;
- make `run_scenario()` call `asyncio.run(_run_scenario_through_gateway(scenario))`;
- create a local Gateway manager with `start_reaper=False`;
- capture `AssistantRunArtifacts` in the backend callback;
- send scenario text/media through `GatewayTurnRequest`;
- build the existing result payload from captured artifacts;
- close the manager in `finally`.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_e2e_demo_runner.py::test_demo_runner_runs_scenarios_through_gateway -q
```

Expected: PASS.

### Task 2: Preserve Demo and CLI Contracts

**Files:**
- Modify: `scripts/run_demo_flows.py`
- Test: `tests/test_e2e_demo_runner.py`, `tests/test_memory_demo_runner.py`, `tests/test_video_demo_runner.py`, `tests/test_assistant_cli.py`

**Interfaces:**
- Existing result keys stay: `scenario_id`, `status`, `tool_sequence`,
  `response_text`, `errors`, `run_id`, `trace_id`, `checks`.
- CLI `--scenario` keeps returning one scenario result plus `offline=true`.

- [ ] **Step 1: Run focused demo/CLI tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_e2e_demo_runner.py tests/test_memory_demo_runner.py tests/test_video_demo_runner.py tests/test_assistant_cli.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Fix only compatibility regressions**

If tests fail, preserve the public JSON keys and existing response checks. Do
not change scenario data or CLI output fields in this phase.

### Task 3: Update Boundary Docs and Guards

**Files:**
- Modify: `docs/gateway-architecture.md`
- Modify: `tests/test_architecture_boundaries.py`

**Interfaces:**
- `scripts/run_demo_flows.py` must not import or instantiate `AgentGraphRuntime`.
- Gateway architecture docs must state that demo/scenario paths are migrated and
  eval remains a direct offline harness exception.

- [ ] **Step 1: Add architecture boundary guard**

Add `scripts/run_demo_flows.py` to
`test_product_entry_layers_do_not_import_agent_graph_runtime_directly()`.

- [ ] **Step 2: Update Gateway docs**

In `docs/gateway-architecture.md`, update the Quick Handoff and request/response
entry sections so they say demo scenarios and CLI `--scenario` enter Gateway
through the demo runner, while eval harnesses may call lower layers directly for
offline regression measurement.

- [ ] **Step 3: Run Gateway/demo verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_architecture_boundaries.py tests/test_gateway.py tests/test_gateway_turn_facade.py tests/test_gateway_session.py tests/test_gateway_api.py tests/test_realtime_agent_backend.py tests/test_realtime_event_mapping.py tests/test_realtime_backend_types.py tests/test_e2e_demo_runner.py tests/test_memory_demo_runner.py tests/test_video_demo_runner.py tests/test_assistant_cli.py -q
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
git add docs/superpowers/specs/2026-07-08-demo-scenario-gateway-migration-design.md docs/superpowers/plans/2026-07-08-demo-scenario-gateway-migration.md docs/gateway-architecture.md scripts/run_demo_flows.py tests/test_e2e_demo_runner.py tests/test_architecture_boundaries.py
git commit -m "feat: route demo scenarios through gateway"
```

## Self-Review

- Spec coverage: demo/scenario paths migrate through Gateway; eval exception is
  documented.
- Placeholder scan: no unresolved placeholders.
- Scope check: no observer wiring, no real providers, no scenario data changes.
