# Observability Context Build Trace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Emit canonical `context.build.started` and `context.build.finished` trace events whenever the assistant builds a context pack for native or mock/offline ReAct decisions.

**Architecture:** Add a small wrapper in the context service layer that calls the existing `build_assistant_context_pack()` and emits redacted observability events around it. Replace runtime context-build call sites with the wrapper, leaving the existing budget, compaction, rendering, and memory policy behavior unchanged.

**Tech Stack:** Python standard library, existing `TraceStore`/`TraceEvent`, `AssistantContextPack`, pytest, existing mock and scripted native runtime tests.

## Global Constraints

- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for pytest.
- Do not change context compaction, memory retrieval/write policy, or provider-native tool calling semantics.
- Do not expose raw prompts, rendered context bodies, raw provider payloads, memory content, media bodies, secrets, or hidden reasoning in trace events.
- Keep mock/local/offline defaults.
- Do not stage unrelated system-prompt, memory-media, remote-memory, or risk-gate work currently dirty in the worktree.

---

### Task 1: Context Build Trace Events

**Files:**
- Create: `src/assistant_agent/services/context/observability.py`
- Modify: `src/assistant_agent/agent/runtime.py`
- Modify: `src/assistant_agent/agent/assistant_loop_nodes.py`
- Modify: `tests/test_observability_harness.py`
- Modify: `docs/observability-harness.md`

**Interfaces:**
- Produces: `build_traced_assistant_context_pack(..., trace_store: TraceStore | None, trace_id: str | None, node_name: str) -> AssistantContextPack`.
- Produces: `context_trace_summary(pack: AssistantContextPack) -> dict[str, Any]`.
- Emits canonical events:
  - `context.build.started`
  - `context.build.finished`

- [ ] **Step 1: Write failing tests**

Add tests in `tests/test_observability_harness.py` that run mock/offline and scripted native runtime paths, then assert `context.build.finished` includes `output_summary.context.budget` and prompt-safe attributes.

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_observability_harness.py::test_mock_runtime_emits_context_build_trace_with_budget_summary tests/test_observability_harness.py::test_native_runtime_emits_context_build_trace_with_budget_summary -q
```

Expected: fail because no standalone canonical context build events exist yet.

- [ ] **Step 3: Implement context observability wrapper**

Create `src/assistant_agent/services/context/observability.py` with wrapper logic, duration measurement, redacted output summary, and failed-build trace behavior.

- [ ] **Step 4: Replace context build call sites**

Use the wrapper in `AgentGraphRuntime._native_runtime_chat_request()`, `_build_decision_context()`, and `_rebuild_context_after_provider_overflow()`.

- [ ] **Step 5: Update docs**

Update `docs/observability-harness.md` to state that context build events are emitted by the context observability wrapper around `AssistantContextPack` construction.

- [ ] **Step 6: Run targeted and fast tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_observability_harness.py tests/test_trace_metrics.py tests/test_trace_view_script.py tests/test_assistant_context_renderer.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
git diff --check -- src/assistant_agent/services/context/observability.py src/assistant_agent/agent/runtime.py src/assistant_agent/agent/assistant_loop_nodes.py tests/test_observability_harness.py docs/observability-harness.md docs/superpowers/plans
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/assistant_agent/services/context/observability.py src/assistant_agent/agent/runtime.py src/assistant_agent/agent/assistant_loop_nodes.py tests/test_observability_harness.py docs/observability-harness.md docs/superpowers/plans/2026-07-07-observability-context-build-trace.md
git commit -m "feat: trace context build events"
```
