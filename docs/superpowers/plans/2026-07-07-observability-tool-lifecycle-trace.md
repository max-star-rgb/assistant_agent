# Observability Tool Lifecycle Trace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `ToolExecutor` write canonical `tool.started`, `tool.finished`, and `tool.failed` trace events, then keep metrics from double-counting terminal tool events plus assistant-facing observations.

**Architecture:** Emit redacted canonical trace events inside `ToolExecutor.run_tool()` because it is the existing governance boundary for state, events, history, budget, retry, risk gate, and registry execution. Update `trace_metrics` so terminal tool lifecycle events are preferred, while old observation-only traces remain supported.

**Tech Stack:** Python standard library, existing `TraceStore`/`TraceEvent`, pytest, existing tool executor tests and trace metrics tests.

## Global Constraints

- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for pytest.
- Keep all tool execution behind `ActionValidator -> ToolExecutor -> ToolRegistry`.
- Do not write raw tool input, raw provider payloads, prompts, media bodies, secrets, or hidden reasoning to traces.
- Keep existing `AgentEvent` and `ToolHistoryStore` behavior stable.
- Do not touch unrelated memory-media, system prompt, runtime, or risk-gate work currently dirty in the worktree.

---

### Task 1: ToolExecutor Lifecycle Trace

**Files:**
- Modify: `src/assistant_agent/agent/tool_executor.py`
- Modify: `src/assistant_agent/services/trace_metrics.py`
- Modify: `tests/test_tool_executor.py`
- Modify: `tests/test_trace_metrics.py`
- Modify: `docs/observability-harness.md`

**Interfaces:**
- Consumes: `TraceStore.append(TraceEvent(...))`.
- Produces canonical events from `ToolExecutor.run_tool()`:
  - `tool.started`
  - `tool.finished`
  - `tool.failed`
- Produces metrics behavior: count terminal tool lifecycle events first and use `tool.observation` only for older traces with no terminal event for the same run/tool/call.

- [ ] **Step 1: Write failing tests**

Add tests proving `ToolExecutor` emits `tool.started` + terminal event and `trace_metrics` avoids double-counting terminal event plus observation.

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_tool_executor.py::test_tool_executor_emits_canonical_tool_lifecycle_trace_for_success tests/test_tool_executor.py::test_tool_executor_emits_canonical_tool_lifecycle_trace_for_failure tests/test_trace_metrics.py::test_trace_metrics_prefers_terminal_tool_events_over_observation_duplicates -q
```

Expected: fail because `ToolExecutor` does not emit canonical tool lifecycle events and metrics currently counts both terminal and observation events.

- [ ] **Step 3: Implement ToolExecutor trace events**

Emit `tool.started` after `state.add_tool_call()`, emit `tool.finished` for success, duplicate suppression, and pending confirmation, and emit `tool.failed` for budget block, cancellation, and tool failure.

- [ ] **Step 4: Implement trace metrics tool-call dedupe**

Group tool events by `(run_id, tool_call_id)` when present, otherwise by `(run_id, tool_name, event index)`. Prefer `tool.finished` / `tool.failed`; fall back to `tool.observation` only when no terminal lifecycle event exists.

- [ ] **Step 5: Run targeted and fast tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_tool_executor.py tests/test_trace_metrics.py tests/test_trace_view_script.py tests/test_observability_harness.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
git diff --check -- src/assistant_agent/agent/tool_executor.py src/assistant_agent/services/trace_metrics.py tests/test_tool_executor.py tests/test_trace_metrics.py docs/observability-harness.md docs/superpowers/plans
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/assistant_agent/agent/tool_executor.py src/assistant_agent/services/trace_metrics.py tests/test_tool_executor.py tests/test_trace_metrics.py docs/observability-harness.md docs/superpowers/plans/2026-07-07-observability-tool-lifecycle-trace.md
git commit -m "feat: trace tool lifecycle events"
```
