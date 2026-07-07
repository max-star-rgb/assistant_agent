# Observability Harness Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align native provider runtime and mock/offline ReAct runtime around a shared redacted canonical trace timeline.

**Architecture:** Add canonical observability fields to `TraceEvent` without breaking existing query responses, then write runtime events from the current boundaries that already own the facts: `AgentGraphRuntime`, `assistant_loop_nodes`, `ToolExecutor`, and `TraceQueryService`. Keep existing `node_path`, `react_steps`, and `decision_trace` stable while adding `canonical_event`, `span_id`, `parent_span_id`, and `attributes` to public trace summaries.

**Tech Stack:** Python 3.12/3.13-compatible code, Pydantic models, pytest, existing `InMemoryTraceStore`, existing mock/local providers only.

## Global Constraints

- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for pytest.
- Preserve mock/local/offline defaults; do not enable real providers.
- Do not expose raw provider payloads, full prompts, full memory content, base64/media bodies, secrets, or hidden reasoning.
- Do not bypass `ActionValidator -> ToolExecutor -> ToolRegistry`.
- Do not touch unrelated memory-media work currently dirty in the worktree.
- Keep public response fields backward-compatible.

---

### Task 1: Trace Schema Canonical Fields

**Files:**
- Modify: `src/assistant_agent/services/trace_store.py`
- Test: `tests/test_observability_harness.py`

**Interfaces:**
- Produces: `TraceEvent.canonical_event: str | None`, `TraceEvent.span_id: str | None`, `TraceEvent.parent_span_id: str | None`, `TraceEvent.attributes: dict[str, Any]`.
- Produces: `append_observability_event(trace_store, *, trace_id, run_id, user_id, session_id, canonical_event, node_name="runtime", status=None, tool_name=None, provider=None, model=None, latency_ms=None, attributes=None, error=None) -> None`.
- Consumes: existing `TraceStore.append()` and `trace_event_summary()`.

- [ ] **Step 1: Write failing schema/query test**

```python
def test_trace_event_summary_exposes_redacted_canonical_fields() -> None:
    store = InMemoryTraceStore()
    store.append(
        TraceEvent(
            trace_id="trace_1",
            run_id="run_1",
            user_id="u1",
            session_id="s1",
            node_name="native_runtime",
            event_type="assistant_decision",
            canonical_event="react.decision",
            span_id="span_decision_1",
            parent_span_id="span_run_1",
            attributes={
                "decision_type": "tool_call",
                "api_key": "secret",
                "raw_provider_payload": {"token": "hidden"},
            },
        )
    )

    summary = trace_debug_summary(store.list_by_trace("trace_1"))
    event = summary["events"][0]

    assert event["canonical_event"] == "react.decision"
    assert event["span_id"] == "span_decision_1"
    assert event["parent_span_id"] == "span_run_1"
    assert event["attributes"]["decision_type"] == "tool_call"
    dumped = json.dumps(event, ensure_ascii=False).lower()
    assert "secret" not in dumped
    assert "raw_provider_payload" not in dumped
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_observability_harness.py::test_trace_event_summary_exposes_redacted_canonical_fields -q
```

Expected: fail because `TraceEvent` does not accept or expose canonical fields.

- [ ] **Step 3: Implement trace schema fields and helper**

Add optional fields to `TraceEvent`, redact `attributes`, include them in `trace_event_summary()`, and add `append_observability_event()`.

- [ ] **Step 4: Run test to verify it passes**

Run the same pytest command. Expected: pass.

---

### Task 2: Mock/Offline ReAct Canonical Timeline

**Files:**
- Modify: `src/assistant_agent/agent/runtime.py`
- Modify: `src/assistant_agent/agent/assistant_loop_nodes.py`
- Test: `tests/test_observability_harness.py`

**Interfaces:**
- Consumes: `append_observability_event(...)`.
- Produces canonical events for mock/offline path: `run.started`, `react.decision`, `action.validation.finished`, `tool.observation`, and terminal run event.

- [ ] **Step 1: Write failing mock/offline invariant test**

```python
def test_mock_react_runtime_emits_canonical_run_decision_tool_observation_and_terminal_events() -> None:
    trace_store = InMemoryTraceStore()
    state = AgentGraphRuntime(trace_store=trace_store).run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我找相似款")
    )

    events = trace_debug_summary(trace_store.list_by_run(state.run_id))["events"]
    canonical = [event["canonical_event"] for event in events if event["canonical_event"]]

    assert "run.started" in canonical
    assert "react.decision" in canonical
    assert "action.validation.finished" in canonical
    assert "tool.observation" in canonical
    assert "run.completed" in canonical
    assert canonical.count("run.completed") == 1
    assert all("thought" not in json.dumps(event, ensure_ascii=False).lower() for event in events)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_observability_harness.py::test_mock_react_runtime_emits_canonical_run_decision_tool_observation_and_terminal_events -q
```

Expected: fail because canonical events are not populated.

- [ ] **Step 3: Implement mock/offline canonical event writing**

Add `run.started` and terminal events in `AgentGraphRuntime.run_state()`, add canonical fields in `_record_react_decision()`, validation completion in `execute_requested_tool_node()`, and canonical `tool.observation` in `_record_react_observation()`.

- [ ] **Step 4: Run test to verify it passes**

Run the same pytest command. Expected: pass.

---

### Task 3: Native Runtime Canonical Timeline

**Files:**
- Modify: `src/assistant_agent/agent/runtime.py`
- Test: `tests/test_observability_harness.py`

**Interfaces:**
- Consumes: existing scripted/fake native chat adapter patterns from `tests/test_native_tool_call_handoff.py`.
- Produces canonical events for native path: `run.started`, `llm.chat.finished`, `react.decision`, `action.validation.finished`, `tool.observation`, and terminal run event.

- [ ] **Step 1: Write failing native runtime invariant test**

```python
def test_native_runtime_emits_canonical_llm_decision_validation_observation_and_terminal_events() -> None:
    trace_store = InMemoryTraceStore()
    adapter = ScriptedNativeChatAdapter(
        [
            ChatResult(
                success=True,
                response_text="",
                provider="scripted",
                model="native-test",
                latency_ms=11,
                tool_calls=[
                    NativeToolCall(
                        id="call_native_1",
                        name="product_search",
                        arguments={"query": "白色运动鞋"},
                    )
                ],
                message_kind="tool_calls",
            ),
            ChatResult(
                success=True,
                response_text="找到了一些白色运动鞋。",
                provider="scripted",
                model="native-test",
                latency_ms=13,
                message_kind="content",
            ),
        ]
    )
    state = AgentGraphRuntime(trace_store=trace_store, chat_adapter=adapter).run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我找白色运动鞋")
    )

    events = trace_debug_summary(trace_store.list_by_run(state.run_id))["events"]
    canonical = [event["canonical_event"] for event in events if event["canonical_event"]]

    assert "run.started" in canonical
    assert canonical.count("llm.chat.finished") == 2
    assert "react.decision" in canonical
    assert "action.validation.finished" in canonical
    assert "tool.observation" in canonical
    assert "run.completed" in canonical
    assert canonical.index("react.decision") < canonical.index("tool.observation")
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_observability_harness.py::test_native_runtime_emits_canonical_llm_decision_validation_observation_and_terminal_events -q
```

Expected: fail because native runtime only records partial metadata/tool failure trace today.

- [ ] **Step 3: Implement native canonical event writing**

Record `llm.chat.finished` after each `ChatAdapter.chat()`, `react.decision` when native tool calls or final answers are normalized, validation completion after `ActionValidator.validate()`, `tool.observation` after observation construction, and terminal run event through the shared run finalizer.

- [ ] **Step 4: Run targeted observability tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_observability_harness.py -q
```

Expected: all observability harness tests pass.

---

### Task 4: Regression Sweep

**Files:**
- Test-only validation.

**Interfaces:**
- Consumes changed trace/runtime behavior.

- [ ] **Step 1: Run relevant regression tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_graph_execution_trace.py tests/test_trace_query_api.py tests/test_agent_events.py tests/test_native_tool_call_handoff.py -q
```

Expected: pass without exposing raw payloads or changing existing public fields.

- [ ] **Step 2: Run diff check**

Run:

```bash
git diff --check -- src/assistant_agent/agent src/assistant_agent/services tests docs/superpowers/plans
```

Expected: no whitespace errors.

- [ ] **Step 3: Commit**

Stage only observability files and commit:

```bash
git add src/assistant_agent/agent/runtime.py src/assistant_agent/agent/assistant_loop_nodes.py src/assistant_agent/services/trace_store.py tests/test_observability_harness.py docs/superpowers/plans/2026-07-07-observability-harness-phase1.md
git commit -m "feat: align observability trace timeline"
```
