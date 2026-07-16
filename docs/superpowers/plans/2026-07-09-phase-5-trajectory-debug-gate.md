# Phase 5 Trajectory Debug Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the Phase 5 gate with a redacted trajectory replay/eval contract that supports debugging and regression review without automatic learning or production policy changes.

**Architecture:** Build on existing `TraceStore`, `TraceQueryService`, `trace_view.py`, memory evals, and Skill v1 gates. Add a small `trajectory_debug` service that converts redacted `TraceEvent` records into replay-safe diagnostic cases and evaluates whether memory/skill improvement suggestions can enter manual review. Do not create an RL pipeline, scheduler, model training flow, autonomous policy updater, or production learning loop.

**Tech Stack:** Python 3, Pydantic v2, pytest, existing `TraceEvent`, existing provider redaction helpers.

## Global Constraints

- Default mock/local/offline behavior only.
- No new dependencies.
- No real provider calls.
- No automatic modification of production memory, skill, prompts, routing, tool policy, or provider policy.
- Replay payloads must not include raw user text, prompts, rendered context, raw memory, raw provider payloads, secrets, or inline media bodies.
- Memory and skill improvement suggestions must require explicit regression evidence before manual review.

---

### Task 1: Redacted Trajectory Replay Case

**Files:**
- Create: `src/assistant_agent/services/trajectory_debug.py`
- Test: `tests/test_phase5_trajectory_debug_gate.py`

**Interfaces:**
- Consumes: `Iterable[TraceEvent]`
- Produces: `build_redacted_trajectory_replay(events: Iterable[TraceEvent]) -> TrajectoryReplayCase`

- [x] **Step 1: Write the failing replay redaction test**

Create `tests/test_phase5_trajectory_debug_gate.py` with a test that builds a short run trace containing raw user text, `memory_context_text`, `raw_provider_response`, `Authorization`, `sk-secret`, and inline-media-like data inside `attributes`, `input_summary`, and `output_summary`. The test should call `build_redacted_trajectory_replay(events)` and assert:

- `replay.replay_mode == "debug_replay_eval_only"`.
- `replay.raw_data_included is False`.
- timeline entries include canonical event names, status, tool/provider, error code, and span IDs.
- serialized replay JSON does not contain the raw user text, raw memory text, provider payload, Authorization value, `sk-secret`, or inline media body.
- replay redaction flags say raw payloads, memory content, conversation history, and media bodies are not included.

- [x] **Step 2: Verify the test fails**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase5_trajectory_debug_gate.py -q
```

Expected: FAIL because `trajectory_debug.py` does not exist yet.

- [x] **Step 3: Implement the minimal replay builder**

Create `src/assistant_agent/services/trajectory_debug.py` with:

- `TrajectoryTimelineEvent`
- `TrajectoryReplayCase`
- `build_redacted_trajectory_replay(events)`

Implementation rules:

- Sort by insertion order supplied by caller; do not reorder.
- Keep only prompt-safe fields:
  - `canonical_event`, `event_type`, `node_name`, `status`
  - `tool_name`, `provider`, `model`, `error_code`
  - `latency_ms`, `span_id`, `parent_span_id`
  - allowlisted attributes and summaries such as budget ratio, retry count, recovery action, output refs, item/result counts.
- Convert error payloads to `{code, recoverable}` plus sanitized message only when needed.
- Never include raw `input_summary` or `output_summary` wholesale.
- Set `production_mutation_allowed=False`.

- [x] **Step 4: Run the gate test to green**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase5_trajectory_debug_gate.py -q
```

Expected: PASS.

---

### Task 2: Learning Gate Is Manual Review Only

**Files:**
- Modify: `src/assistant_agent/services/trajectory_debug.py`
- Test: `tests/test_phase5_trajectory_debug_gate.py`

**Interfaces:**
- Consumes: `TrajectoryReplayCase`
- Produces: `evaluate_trajectory_improvement_gate(...) -> TrajectoryImprovementGateReport`

- [x] **Step 1: Write the failing learning gate tests**

Add tests that assert:

- For `target="memory"` and `memory_regression_passed=False`, the report blocks manual review with `memory_regression_required`.
- For `target="skill"` and `skill_regression_passed=False`, the report blocks manual review with `skill_regression_required`.
- When both replay safety and the target regression pass, the report allows manual review but still has `production_mutation_allowed is False`, `auto_apply_allowed is False`, and `learning_loop_mode == "debug_replay_eval_only"`.

- [x] **Step 2: Verify failure**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase5_trajectory_debug_gate.py -q
```

Expected: FAIL because the learning gate API does not exist.

- [x] **Step 3: Implement the gate report**

Add:

- `TrajectoryImprovementTarget = Literal["memory", "skill"]`
- `TrajectoryImprovementGateReport`
- `evaluate_trajectory_improvement_gate(replay, target, memory_regression_passed=False, skill_regression_passed=False)`

Rules:

- `production_mutation_allowed` is always `False`.
- `auto_apply_allowed` is always `False`.
- `manual_review_allowed` is true only when replay is safe and the target regression is passed.
- `required_regression_suites` includes `memory` for memory target and `skill` for skill target.
- `blocked_reasons` include precise missing requirements.

- [x] **Step 4: Run focused tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase5_trajectory_debug_gate.py -q
```

Expected: PASS.

---

### Task 3: Documentation and Gate Commands

**Files:**
- Modify: `docs/observability-harness.md`
- Modify: `docs/roadmaps/personal-realtime-ai-assistant-roadmap.md`

**Interfaces:**
- Consumes: Phase 5 roadmap gate and observability authority.
- Produces: clear Phase 5 wording that trajectory debug is local/redacted/manual-review only.

- [x] **Step 1: Update observability authority**

Document:

- `trajectory_debug` creates redacted replay cases from `TraceEvent`.
- Replay cases are diagnostic/eval artifacts only.
- Replay does not contain raw prompts, user text, memory content, provider payloads, or media bodies.
- Improvement gates never auto-apply changes and require memory/skill regression evidence.

- [x] **Step 2: Update roadmap Phase 5 gate**

Add Phase 5 commands:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase5_trajectory_debug_gate.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase2_memory_intelligence_gate.py tests/test_phase3_skill_system_gate.py -q
```

Keep RL pipeline, production self-modification, and private-data training explicitly out of scope.

- [x] **Step 3: Run doc diff check**

Run:

```bash
git diff --check -- docs/observability-harness.md docs/roadmaps/personal-realtime-ai-assistant-roadmap.md
```

Expected: no output and exit code 0.

---

### Task 4: Verification and Commit

**Files:**
- All modified Phase 5 files.

**Interfaces:**
- Consumes: previous tasks.
- Produces: committed Phase 5 debug/eval gate patch.

- [x] **Step 1: Run Phase 5 gate**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase5_trajectory_debug_gate.py -q
```

- [x] **Step 2: Run memory and skill regression gates**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase2_memory_intelligence_gate.py tests/test_phase3_skill_system_gate.py -q
```

- [x] **Step 3: Run observability regression tests**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase0_trace_invariant_gate.py tests/test_trace_view_script.py tests/test_trace_redaction.py tests/test_observability_harness.py -q
```

- [x] **Step 4: Run fast suite**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

- [x] **Step 5: Run diff check**

```bash
git diff --check -- AGENTS.md docs src tests scripts skills
```

- [x] **Step 6: Commit**

```bash
git add docs/observability-harness.md docs/roadmaps/personal-realtime-ai-assistant-roadmap.md docs/superpowers/plans/2026-07-09-phase-5-trajectory-debug-gate.md src/assistant_agent/services/trajectory_debug.py tests/test_phase5_trajectory_debug_gate.py
git commit -m "参考hermes的长期个人助手:phase5"
```
