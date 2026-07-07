# Observability Trace Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local `scripts/trace_view.py` CLI that lets developers inspect one redacted run or trace timeline from the JSONL trace store.

**Architecture:** Reuse `JsonlTraceStore` and `trace_debug_summary()` so the viewer consumes the same redacted records exposed by trace APIs. Keep the script read-only, dependency-free, and focused on paste-friendly human output plus JSON output for follow-up tooling.

**Tech Stack:** Python standard library, existing Pydantic trace models, pytest subprocess tests, local JSONL trace files.

## Global Constraints

- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for pytest.
- Read only redacted trace records from `JsonlTraceStore`; do not expose raw provider payloads, prompts, memory content, secrets, media bodies, or hidden reasoning.
- Accept either `run_id` or `trace_id` as the lookup key.
- Keep output short enough to paste into issue comments or handoff notes.
- Do not touch unrelated memory-media work currently dirty in the worktree.

---

### Task 1: Trace Viewer CLI

**Files:**
- Create: `scripts/trace_view.py`
- Test: `tests/test_trace_view_script.py`

**Interfaces:**
- Consumes: `JsonlTraceStore(path).list_by_run(id)` and `JsonlTraceStore(path).list_by_trace(id)`.
- Consumes: `trace_debug_summary(events) -> dict[str, Any]`.
- Produces: `main(argv: Sequence[str] | None = None) -> int`.
- Produces command:
  - `/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_view.py <run_id-or-trace_id>`
  - `/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_view.py <run_id-or-trace_id> --errors`
  - `/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/trace_view.py <run_id-or-trace_id> --json`

- [ ] **Step 1: Write failing subprocess tests**

Create `tests/test_trace_view_script.py` with tests that write sample events to a temporary JSONL file and call `scripts/trace_view.py` through `subprocess.run()`.

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_trace_view_script.py -q
```

Expected: fail because `scripts/trace_view.py` does not exist yet.

- [ ] **Step 3: Implement minimal viewer**

Create `scripts/trace_view.py` with `argparse`, lookup by run first then trace, `--trace-path`, `--errors`, `--json`, human timeline rendering, and a nonzero exit for missing IDs.

- [ ] **Step 4: Run targeted viewer tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_trace_view_script.py -q
```

Expected: pass.

- [ ] **Step 5: Run observability regression tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_trace_view_script.py tests/test_observability_harness.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
git diff --check -- scripts tests docs/superpowers/plans
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add scripts/trace_view.py tests/test_trace_view_script.py docs/superpowers/plans/2026-07-07-observability-trace-viewer.md
git commit -m "feat: add trace viewer script"
```
