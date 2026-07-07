# Observability Trace Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local metrics summary derived from redacted trace JSONL events so developers can inspect run health, tool behavior, LLM usage, context budget, Gateway cancellation, and memory counters.

**Architecture:** Add a reusable `assistant_agent.services.trace_metrics` module that loads and aggregates redacted `TraceEvent` records without changing runtime execution. Expose it through `scripts/trace_metrics.py` as a read-only local CLI with human and JSON output.

**Tech Stack:** Python standard library, existing `TraceEvent` Pydantic model, pytest subprocess tests, local JSONL trace files.

## Global Constraints

- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for pytest.
- Derive metrics from redacted trace/events; do not expose raw prompts, memory content, provider payloads, secrets, media bodies, or hidden reasoning.
- Keep metric labels low-cardinality: run status, tool name, provider, model, canonical event, error code, and cancellation source.
- Do not add external APM dependencies or enable real providers.
- Do not touch unrelated memory-media work currently dirty in the worktree.

---

### Task 1: Trace Metrics Service And CLI

**Files:**
- Create: `src/assistant_agent/services/trace_metrics.py`
- Create: `scripts/trace_metrics.py`
- Create: `tests/test_trace_metrics.py`
- Modify: `docs/observability-harness.md`

**Interfaces:**
- Consumes: `TraceEvent` from `assistant_agent.services.trace_store`.
- Produces: `load_trace_events(path: Path | str) -> list[TraceEvent]`.
- Produces: `filter_trace_events(events: list[TraceEvent], *, user_id: str | None = None, session_id: str | None = None) -> list[TraceEvent]`.
- Produces: `build_trace_metrics(events: list[TraceEvent]) -> dict[str, Any]`.
- Produces command:
  - `/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_metrics.py`
  - `/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_metrics.py --trace-path .data/graph_trace.jsonl`
  - `/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_metrics.py --json`

- [ ] **Step 1: Write failing service and subprocess tests**

Create `tests/test_trace_metrics.py` with tests that build sample trace events, assert aggregate metrics, and call `scripts/trace_metrics.py` through `subprocess.run()`.

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_trace_metrics.py -q
```

Expected: fail because `assistant_agent.services.trace_metrics` and `scripts/trace_metrics.py` do not exist yet.

- [ ] **Step 3: Implement the metrics service**

Create `src/assistant_agent/services/trace_metrics.py` with JSONL loading, optional user/session filtering, run status rates, duration p50/p95, error code counts, tool counters, LLM counters, context budget counters, Gateway cancel counters, and memory counters.

- [ ] **Step 4: Implement the metrics CLI**

Create `scripts/trace_metrics.py` with `argparse`, `--trace-path`, `--user-id`, `--session-id`, and `--json`, then render a compact human summary.

- [ ] **Step 5: Update observability docs**

Update `docs/observability-harness.md` to list the metrics command and clarify that Phase 3 currently ships a script, with API endpoint exposure left for a later phase.

- [ ] **Step 6: Run targeted and fast tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_trace_metrics.py tests/test_trace_view_script.py tests/test_observability_harness.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
git diff --check -- src/assistant_agent/services/trace_metrics.py scripts/trace_metrics.py tests/test_trace_metrics.py docs/observability-harness.md docs/superpowers/plans
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit**

```bash
git add src/assistant_agent/services/trace_metrics.py scripts/trace_metrics.py tests/test_trace_metrics.py docs/observability-harness.md docs/superpowers/plans/2026-07-07-observability-trace-metrics.md
git commit -m "feat: add trace metrics summary"
```
