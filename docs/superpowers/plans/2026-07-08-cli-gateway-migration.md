# CLI Gateway Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route local offline CLI `--text` runs through Gateway while preserving the current CLI payload.

**Architecture:** `run_text_prompt()` creates a local `GatewaySessionManager` and `GatewayTurnFacade`, uses `GatewayAgentAdapter` backed by `AssistantRuntimeApp.run_request()`, captures `AssistantRunArtifacts`, then formats the existing CLI payload from those artifacts.

**Tech Stack:** Python asyncio, existing Gateway session manager/facade, `GatewayAgentAdapter`, `AssistantRuntimeApp`, pytest.

## Global Constraints

- Only migrate `scripts/run_assistant_cli.py --text`.
- Do not migrate `--scenario`, remote `scripts/run_client.py`, or legacy `/ws/agent/{session_id}`.
- Do not add observer wiring.
- Do not enable real providers; keep CLI default mock/local/offline behavior.
- Use TDD: write failing tests before production code.

---

### Task 1: Add CLI Gateway Runtime Test

**Files:**
- Modify: `tests/test_assistant_cli.py`
- Modify: `scripts/run_assistant_cli.py`

**Interfaces:**
- Produces: `run_text_prompt()` continues returning `dict[str, Any]`.
- Internal behavior: final runtime `UserRequest.metadata["runtime"]["history"]` exists, proving the request entered `GatewaySessionService`.

- [ ] **Step 1: Write the failing test**

Add a test to `tests/test_assistant_cli.py` that monkeypatches
`scripts.run_assistant_cli.create_runtime` to return a recording runtime, calls
`run_text_prompt("你好")`, and asserts the captured request contains Gateway
runtime history.

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_assistant_cli.py::test_assistant_cli_text_prompt_runs_through_gateway -q
```

Expected: FAIL with `KeyError: 'runtime'`.

- [ ] **Step 3: Write minimal implementation**

In `scripts/run_assistant_cli.py`:

- import `asyncio`
- import `GatewaySessionManager`, `GatewayAgentAdapter`, `GatewayTurnFacade`,
  and `GatewayTurnRequest`
- change `run_text_prompt()` to call `asyncio.run(_run_text_prompt_through_gateway(...))`
- create local manager/backend/facade inside the async helper
- capture `AssistantRunArtifacts` from the backend callback
- format the existing CLI payload from captured artifacts
- close manager in `finally`

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_assistant_cli.py::test_assistant_cli_text_prompt_runs_through_gateway -q
```

Expected: PASS.

### Task 2: Preserve Existing CLI Contract

**Files:**
- Modify: `scripts/run_assistant_cli.py`
- Test: `tests/test_assistant_cli.py`

**Interfaces:**
- CLI JSON/text output remains unchanged.

- [ ] **Step 1: Run existing CLI tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_assistant_cli.py -q
```

Expected: all CLI tests pass.

- [ ] **Step 2: Refactor only if needed**

If payload formatting is duplicated, extract `_payload_from_artifacts(artifacts)`.
Do not change output keys.

### Task 3: Update Gateway Docs and Verify

**Files:**
- Modify: `docs/gateway-architecture.md`

**Interfaces:**
- Gateway architecture doc says HTTP `/agent/run` and local CLI `--text` use
  Gateway.

- [ ] **Step 1: Update docs**

Update Quick Handoff and request/response path sections to note local CLI
`--text` now uses `GatewayTurnFacade`.

- [ ] **Step 2: Run focused verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_assistant_cli.py tests/test_gateway_turn_facade.py tests/test_gateway_session.py tests/test_realtime_agent_backend.py -q
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
git add docs/superpowers/specs/2026-07-08-cli-gateway-migration-design.md docs/superpowers/plans/2026-07-08-cli-gateway-migration.md docs/gateway-architecture.md scripts/run_assistant_cli.py tests/test_assistant_cli.py
git commit -m "feat: route assistant cli through gateway"
```

## Self-Review

- Spec coverage: local CLI `--text` migrates through Gateway; scenario/demo and WebSocket remain out of scope.
- Placeholder scan: no TBD/TODO placeholders.
- Scope check: one entry path only, independently testable.
