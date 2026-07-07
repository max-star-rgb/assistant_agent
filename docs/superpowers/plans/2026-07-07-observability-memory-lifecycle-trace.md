# Observability Memory Lifecycle Trace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Emit canonical `memory.load.started` / `memory.load.finished` and `memory.save.started` / `memory.save.finished` events for assistant memory lifecycle boundaries.

**Architecture:** Add a small memory observability helper in the service layer. Runtime and graph nodes call the helper around existing `MemoryManager.load_into_state(...)` and `MemoryManager.save_from_run(...)` calls, preserving memory retrieval, write policy, profile merge, and store behavior.

**Tech Stack:** Python standard library, existing `TraceStore` / `append_observability_event`, `MemoryManager`, pytest.

## Global Constraints

- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for pytest.
- Keep memory tools thin; do not move retrieval, ranking, write policy, TTL, audit, or profile behavior out of `MemoryManager`.
- Do not expose memory content, rendered memory context, raw provider payloads, prompts, secrets, or media bodies in trace events.
- Do not stage unrelated system-prompt, memory-media, Memory Server, risk-gate, API, or Gateway work currently dirty in the worktree.

---

### Task 1: Memory Lifecycle Trace Events

**Files:**
- Create: `src/assistant_agent/services/memory_observability.py`
- Modify: `src/assistant_agent/agent/runtime.py`
- Modify: `src/assistant_agent/agent/graph_nodes.py`
- Modify: `tests/test_observability_harness.py`
- Modify: `docs/observability-harness.md`

**Interfaces:**
- Produces: `load_memory_with_trace(...) -> MemoryContext`
- Produces: `save_memory_with_trace(...) -> MemoryItem | None`
- Emits canonical events:
  - `memory.load.started`
  - `memory.load.finished`
  - `memory.save.started`
  - `memory.save.finished`

- [ ] **Step 1: Write failing tests**

Add tests in `tests/test_observability_harness.py` that run mock/offline and native runtime paths, then assert memory load/save canonical events include prompt-safe counters and no raw memory content.

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_observability_harness.py::test_mock_runtime_emits_memory_lifecycle_trace_without_memory_content tests/test_observability_harness.py::test_native_runtime_emits_memory_load_trace_without_memory_content -q
```

Expected: fail because standalone canonical memory lifecycle events are not emitted yet.

- [ ] **Step 3: Implement helper**

Create `src/assistant_agent/services/memory_observability.py` with wrappers around existing manager calls. Summaries must include only counts, token/budget fields, retrieval version, injected memory IDs, promotion counters, skip reason, written ID, and safe error code/message.

- [ ] **Step 4: Replace call sites**

Use the helper in `AgentGraphRuntime._run_native_runtime()`, `load_memory_node()`, and `save_memory_node()`.

- [ ] **Step 5: Update docs**

Update `docs/observability-harness.md` to state that memory lifecycle events are emitted at runtime/graph memory boundaries and are redacted summaries only.

- [ ] **Step 6: Run verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_observability_harness.py tests/test_trace_metrics.py tests/test_memory_manager.py tests/test_memory_runtime_integration.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
git diff --check -- src/assistant_agent/services/memory_observability.py src/assistant_agent/agent/runtime.py src/assistant_agent/agent/graph_nodes.py tests/test_observability_harness.py docs/observability-harness.md docs/superpowers/plans
```

- [ ] **Step 7: Commit**

```bash
git add -p src/assistant_agent/agent/runtime.py src/assistant_agent/agent/graph_nodes.py
git add src/assistant_agent/services/memory_observability.py tests/test_observability_harness.py docs/observability-harness.md docs/superpowers/plans/2026-07-07-observability-memory-lifecycle-trace.md
git commit -m "feat: trace memory lifecycle events"
```
