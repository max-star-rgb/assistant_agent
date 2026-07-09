# Realtime Assistant Loop Deepening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the Phase 1 deepening from `docs/superpowers/specs/2026-07-09-realtime-assistant-loop-design.md`: a text-only realtime assistant loop with stable turn state, cancel/interrupt/hangup behavior, stale-output suppression, simulator coverage, and trace visibility.

**Architecture:** Keep Gateway as the lifecycle boundary and `GatewayAgentAdapter` as the thin bridge to `AgentGraphRuntime`. Add a small, testable Turn Manager only if the current `GatewaySessionService` state rules need a clear boundary. Do not add a new realtime runtime, new Agent loop, ASR/TTS, multi-agent behavior, or real provider calls.

**Tech Stack:** Python 3, asyncio, FastAPI TestClient, Pydantic models already in the repo, pytest/unittest, existing Gateway/realtime modules.

## Global Constraints

- Text-only realtime orchestration; ASR/TTS/media service stays outside this repo.
- Default mock/local/offline behavior only.
- No new dependencies.
- Do not call real providers.
- Do not introduce a second Agent loop.
- Keep all tool execution behind `ActionValidator -> ToolExecutor -> ToolRegistry`.
- Preserve existing Gateway frame names and public backend contracts.
- Do not stage or commit unrelated `.gitignore` changes.

---

### Task 1: Turn State Contract

**Files:**
- Create: `src/assistant_agent/gateway/turn_manager.py` if the current private logic is too hard to test directly.
- Modify: `src/assistant_agent/gateway/session.py`
- Test: `tests/test_phase1_realtime_loop_deep_gate.py`

**Interfaces:**
- Produces, if needed: `TurnState`, `TurnEvent`, `TurnAction`, and `decide_turn_action(...)`.
- Gateway-facing behavior remains through `GatewaySessionService`.

- [x] **Step 1: Write failing tests for queued and interrupt turns**

Add tests that run `GatewaySessionService` with a deterministic slow backend:

- normal second `message.user` while a run is active queues behind the first run.
- `interrupt=true` second `message.user` cancels the active run and starts a new run.
- queued turn receives same session and ordered history.
- interrupted turn has different run id and metadata marks interrupt.

- [x] **Step 2: Run tests to verify red**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase1_realtime_loop_deep_gate.py -q
```

Expected: at least one assertion fails because the new deep gate is not fully implemented or not yet present.

- [x] **Step 3: Implement minimal turn-state support**

If direct `GatewaySessionService` changes are enough, keep it there. If state rules become unclear, create `turn_manager.py` with a pure decision helper and call it from `_handle_user_message` / `_handle_cancel`.

- [x] **Step 4: Run focused tests to green**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase1_realtime_loop_deep_gate.py -q
```

---

### Task 2: Explicit Cancel And Stale Output Gate

**Files:**
- Modify: `src/assistant_agent/gateway/session.py`
- Test: `tests/test_phase1_realtime_loop_deep_gate.py`

**Interfaces:**
- Consumes: `CancelToken`, `RealtimeAgentEvent`, `RealtimeAgentResult`
- Produces: stale-output suppression invariant after cancel/interruption.

- [x] **Step 1: Write failing stale-output test**

Add a backend that emits a chunk after cancel is requested. Assert the cancelled run emits only `run.end(reason=cancelled)` after cancel, not the late chunk.

- [x] **Step 2: Verify red**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase1_realtime_loop_deep_gate.py::test_cancel_suppresses_late_stream_chunks -q
```

- [x] **Step 3: Implement minimal output gate**

Ensure queued frames are discarded and backend late events are ignored once cancel is set.

- [x] **Step 4: Run focused tests**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase1_realtime_loop_deep_gate.py -q
```

---

### Task 3: Tool-Running Interrupt

**Files:**
- Modify: `src/assistant_agent/gateway/session.py`
- Modify: `src/assistant_agent/realtime/agent_graph_backend.py` only if backend metadata needs a prompt-safe cancel marker.
- Test: `tests/test_phase1_realtime_loop_deep_gate.py`

**Interfaces:**
- Consumes: realtime backend progress/tool events and cancel token.
- Produces: best-effort cancel metadata and no stale tool output to the user.

- [x] **Step 1: Write failing tool-running interrupt test**

Add a fake backend that emits `tool.started`, waits for cancel, then attempts to emit `tool.finished` or `response.chunk`. Assert the old run gets cancelled, no stale finished/chunk frame is sent, and the new turn completes.

- [x] **Step 2: Verify red**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase1_realtime_loop_deep_gate.py::test_tool_running_interrupt_suppresses_stale_tool_output_and_completes_new_turn -q
```

- [x] **Step 3: Implement minimal behavior**

Reuse the Gateway output gate and cancel metadata. Do not make Gateway understand tool semantics beyond suppressing stale output.

- [x] **Step 4: Run focused tests**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase1_realtime_loop_deep_gate.py -q
```

---

### Task 4: Simulator Deepening

**Files:**
- Modify: `scripts/run_realtime_call_simulator.py`
- Modify: `tests/test_realtime_call_simulator.py`

**Interfaces:**
- Produces new simulator scenarios: `cancel`, `tool_interrupt`; `all` includes `basic`, `interrupt`, `hangup`, `cancel`, `tool_interrupt`.

- [x] **Step 1: Write failing simulator tests**

Update `tests/test_realtime_call_simulator.py` to expect `all` includes the new scenarios and asserts their terminal reasons.

- [x] **Step 2: Verify red**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_call_simulator.py -q
```

- [x] **Step 3: Implement scenarios**

Add deterministic simulator paths for explicit `run.cancel` and tool-running interrupt using the existing `TextSimulatorBackend`.

- [x] **Step 4: Run simulator tests and CLI**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_call_simulator.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_realtime_call_simulator.py --scenario all --quiet
```

---

### Task 5: Trace Gate

**Files:**
- Modify: `src/assistant_agent/gateway/session.py`
- Test: `tests/test_phase1_realtime_loop_deep_gate.py`
- Modify docs if trace events are added: `docs/gateway-architecture.md`

**Interfaces:**
- Produces prompt-safe trace/run metadata for terminal realtime turns.

- [x] **Step 1: Write failing trace assertions**

Assert terminal runs include reason and trace id when backend supplies one; cancel/interrupt/hangup include cancel source in prompt-safe metadata; no raw text or media bodies appear in trace-style metadata.

- [x] **Step 2: Verify red if implementation is missing**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase1_realtime_loop_deep_gate.py -q
```

- [x] **Step 3: Implement minimal trace/run metadata**

Prefer Gateway frame payload metadata and existing trace events. Only add new observability events if current surfaces cannot prove the gate.

- [x] **Step 4: Run focused tests**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase1_realtime_loop_deep_gate.py -q
```

---

### Task 6: Verification

**Files:**
- All modified implementation, tests, docs, and plan files.

- [x] **Step 1: Run Gateway and realtime tests**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway.py tests/test_gateway_session.py tests/test_gateway_api.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_agent_backend.py tests/test_realtime_event_mapping.py tests/test_realtime_backend_types.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_call_simulator.py tests/test_phase1_realtime_loop_deep_gate.py -q
```

- [x] **Step 2: Run simulator command**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_realtime_call_simulator.py --scenario all --quiet
```

- [x] **Step 3: Run fast suite**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

- [x] **Step 4: Run diff check**

```bash
git diff --check -- AGENTS.md docs src tests scripts skills
```
