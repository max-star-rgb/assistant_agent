# Observability Invariant Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the observability harness development phase with regression tests for core trace ordering, lifecycle pairing, and redaction invariants.

**Architecture:** Add invariant tests over existing mock/offline and scripted native runtime traces. Fill only the missing canonical lifecycle events needed by those tests: prompt-safe `response.final` and native-runtime skipped `memory.save.*` events.

**Tech Stack:** Python standard library, pytest, existing `TraceStore`, existing mock/scripted native runtime tests.

## Global Constraints

- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for pytest.
- Do not add dashboard, APM, OpenTelemetry export, API debug endpoints, or new trace storage backends.
- Do not expose response text, memory content, rendered prompts, raw provider payloads, secrets, media bodies, or hidden reasoning in trace events.
- Do not stage unrelated system-prompt, memory-media, Memory Server, risk-gate, API, Gateway, or realtime plan work currently dirty in the worktree.

---

### Task 1: Core Trace Invariants

**Files:**
- Create: `src/assistant_agent/services/response_observability.py`
- Modify: `src/assistant_agent/agent/runtime.py`
- Modify: `src/assistant_agent/agent/graph_nodes.py`
- Modify: `tests/test_observability_harness.py`
- Modify: `docs/observability-harness.md`

**Interfaces:**
- Produces: `append_response_final_event(...) -> None`
- Emits canonical event:
  - `response.final`
- Extends native runtime to emit skipped `memory.save.started` / `memory.save.finished`.

- [ ] **Step 1: Write failing tests**

Add invariant tests for mock/offline and scripted native successful runs. The tests assert lifecycle order, tool start/finish pairing where a tool is used, terminal uniqueness, `response.final`, native skipped memory-save trace, and redaction of response/memory text.

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_observability_harness.py::test_mock_runtime_trace_satisfies_success_timeline_invariants tests/test_observability_harness.py::test_native_runtime_trace_satisfies_success_timeline_invariants -q
```

Expected: fail because `response.final` and native skipped memory-save trace are not emitted yet.

- [ ] **Step 3: Implement prompt-safe response final trace**

Create `src/assistant_agent/services/response_observability.py` and call it from graph compose response and native final response code. Record only message presence/length, output ref count, response data keys, status, and error count.

- [ ] **Step 4: Emit native skipped memory-save trace**

After native runtime completes and before terminal run trace, call `save_memory_with_trace(..., skipped_reason="native_runtime_memory_writes_are_llm_tool_calls")`.

- [ ] **Step 5: Update docs**

Update `docs/observability-harness.md` invariants to include `response.final`, native skipped memory save, and phase stop guidance.

- [ ] **Step 6: Run verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_observability_harness.py tests/test_trace_metrics.py tests/test_trace_view_script.py tests/test_trace_query_api.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
git diff --check -- src/assistant_agent/services/response_observability.py src/assistant_agent/agent/runtime.py src/assistant_agent/agent/graph_nodes.py tests/test_observability_harness.py docs/observability-harness.md docs/superpowers/plans/2026-07-07-observability-invariant-closure.md
```

- [ ] **Step 7: Commit**

```bash
git add src/assistant_agent/services/response_observability.py tests/test_observability_harness.py docs/observability-harness.md docs/superpowers/plans/2026-07-07-observability-invariant-closure.md
git add -p src/assistant_agent/agent/runtime.py src/assistant_agent/agent/graph_nodes.py
git commit -m "test: close observability trace invariants"
```
