# Native Runtime Serial Multi-Tool Calls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute multiple provider-native tool calls returned in one model response serially, in provider order, without bypassing `ActionValidator -> ToolExecutor -> ToolRegistry` or the configured tool budget.

**Architecture:** Keep `AgentGraphRuntime._run_native_runtime()` as the provider-native loop owner. Replace the single `result.tool_calls[0]` path with a serial batch loop that reuses the existing validation, execution, observation, event, trace, and final-only handoff behavior per processed call. Use `len(observations)` as the processed tool-turn count so `max_tool_iterations` stays a real cap across multi-call batches.

**Tech Stack:** Python 3, pytest, existing `AgentGraphRuntime`, `NativeToolCall`, `ChatResult`, `ActionValidator`, `ToolExecutor`, `observation_from_tool_result`, `ProviderConfig`.

## Global Constraints

- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for Python and pytest commands.
- Do not add dependencies.
- Do not call real providers; use scripted adapters and mock/local/offline defaults.
- Do not change Gateway wire protocol, ToolSpec definitions, memory policy, or provider selection.
- Do not add parallel execution, tool batch safety whitelist/path conflict analysis, retry/backoff, or large-result persistence.
- Preserve `ActionValidator -> ToolExecutor -> ToolRegistry` for every executed tool.
- Preserve prompt-safe observations and native assistant/tool message pairing.
- Preserve the existing final-only handoff when the tool-call budget is exhausted.

---

## File Structure

- Modify `tests/test_native_tool_call_handoff.py`: add a helper for multi-call native responses and serial orchestration regression tests.
- Modify `src/assistant_agent/agent/runtime.py`: replace single-call handling with serial batch processing and budget skip metadata.
- No architecture docs are modified beyond the approved spec and this implementation plan.

### Task 1: Add Serial Multi-Tool Regression Tests

**Files:**
- Modify: `tests/test_native_tool_call_handoff.py`

**Interfaces:**
- Consumes: `NativeToolChatAdapter`, `native_result(...)`, `final_result(...)`, `AgentGraphRuntime.run_state(...)`.
- Produces: `native_multi_result(calls: list[tuple[str, dict[str, object]]]) -> ChatResult` test helper and four failing tests.

- [x] **Step 1: Add multi-call native result helper**

Add this helper below `native_result(...)`:

```python
def native_multi_result(calls: list[tuple[str, dict[str, object]]]) -> ChatResult:
    return ChatResult(
        response_text="",
        tool_calls=[
            NativeToolCall(
                id=f"call_{index}",
                name=name,
                arguments=arguments,
                raw={
                    "id": f"call_{index}",
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                },
            )
            for index, (name, arguments) in enumerate(calls, start=1)
        ],
        finish_reason="tool_calls",
        message_kind="tool_call",
        provider="scripted-native",
        model="native-test",
    )
```

- [x] **Step 2: Add successful serial multi-tool test**

Append this test near the native runtime handoff tests:

```python
def test_native_runtime_executes_multiple_tool_calls_serially_in_provider_order() -> None:
    adapter = NativeToolChatAdapter(
        [
            native_multi_result(
                [
                    ("product_search", {"query": "通勤耳机", "limit": 2}),
                    ("price_compare", {"query": "通勤耳机", "limit": 2}),
                ]
            ),
            final_result("已基于两个工具 observation 回答。"),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(max_tool_iterations=5),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机并比较价格"))

    assert adapter.calls == 2
    assert [call.tool_name for call in state.tool_calls] == ["product_search", "price_compare"]
    tool_messages = [message for message in adapter.requests[1].messages if message["role"] == "tool"]
    assert len(tool_messages) == 2
    assert tool_messages[0]["tool_call_id"] == "call_1"
    assert tool_messages[1]["tool_call_id"] == "call_2"
    assert "product_search" in tool_messages[0]["content"]
    assert "price_compare" in tool_messages[1]["content"]
    assert state.response is not None
    assert state.response.message == "已基于两个工具 observation 回答。"
    assert state.response.data["tool_observations"] == 2
```

- [x] **Step 3: Add validator rejection stops batch test**

Append this test:

```python
def test_native_runtime_stops_multi_tool_batch_when_first_call_is_rejected() -> None:
    adapter = NativeToolChatAdapter(
        [
            native_multi_result(
                [
                    ("unknown_tool", {}),
                    ("product_search", {"query": "通勤耳机", "limit": 2}),
                ]
            )
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(max_tool_iterations=5),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="use native unknown and then search"))

    assert adapter.calls == 1
    assert state.tool_calls == []
    assert state.response is not None
    assert state.response.data["validator_result"]["code"] == "unknown_tool"
    assert len(state.request.metadata["native_tool_calls"]) == 1
    assert state.request.metadata["native_tool_calls"][0]["name"] == "unknown_tool"
```

- [x] **Step 4: Add budget skip plus final-only handoff test**

Append this test:

```python
def test_native_runtime_multi_tool_batch_respects_single_remaining_tool_budget() -> None:
    adapter = NativeToolChatAdapter(
        [
            native_multi_result(
                [
                    ("product_search", {"query": "通勤耳机", "limit": 2}),
                    ("price_compare", {"query": "通勤耳机", "limit": 2}),
                ]
            ),
            final_result("基于预算允许的工具 observation 给出最终回答。"),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(max_tool_iterations=1),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机并比较价格"))

    assert adapter.calls == 2
    assert [call.tool_name for call in state.tool_calls] == ["product_search"]
    assert adapter.requests[1].tools == []
    assert adapter.requests[1].tool_choice == "none"
    assert state.request.metadata["native_runtime_tool_calls_skipped_for_budget"] == 1
    assert state.response is not None
    assert state.response.message == "基于预算允许的工具 observation 给出最终回答。"
    assert state.response.data["final_only_handoff"] is True
    assert state.response.data["tool_observations"] == 1
```

- [x] **Step 5: Add final call consumes budget after multi-tool batch test**

Append this test:

```python
def test_native_runtime_multi_tool_batch_triggers_final_only_when_last_call_consumes_budget() -> None:
    adapter = NativeToolChatAdapter(
        [
            native_multi_result(
                [
                    ("product_search", {"query": "通勤耳机", "limit": 2}),
                    ("price_compare", {"query": "通勤耳机", "limit": 2}),
                ]
            ),
            final_result("基于两个工具 observation 的最终回答。"),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(max_tool_iterations=2),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机并比较价格"))

    assert adapter.calls == 2
    assert [call.tool_name for call in state.tool_calls] == ["product_search", "price_compare"]
    assert adapter.requests[1].tools == []
    assert adapter.requests[1].tool_choice == "none"
    assert state.request.metadata.get("native_runtime_tool_calls_skipped_for_budget") is None
    assert state.response is not None
    assert state.response.message == "基于两个工具 observation 的最终回答。"
    assert state.response.data["final_only_handoff"] is True
    assert state.response.data["tool_observations"] == 2
```

- [x] **Step 6: Run tests and verify red**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_native_tool_call_handoff.py -q
```

Expected: the new serial multi-tool tests fail because current runtime only processes `result.tool_calls[0]`.

### Task 2: Implement Serial Native Tool Batch Processing

**Files:**
- Modify: `src/assistant_agent/agent/runtime.py`

**Interfaces:**
- Consumes: existing `result.tool_calls`, `native_tool_call_to_assistant_decision(...)`, `ActionValidator().validate(...)`, `tool_executor.run_tool(...)`, `observation_from_tool_result(...)`.
- Produces:
  - Serial processing loop over `result.tool_calls`.
  - `AgentGraphRuntime._record_native_tool_calls_skipped_for_budget(state: AgentState, skipped_count: int) -> None`.

- [x] **Step 1: Replace single-call extraction with serial loop**

In `AgentGraphRuntime._run_native_runtime()`, inside `if result.success and result.tool_calls:`, replace the current single-call block beginning at:

```python
                call = result.tool_calls[0]
```

and ending before:

```python
                continue
```

with this serial loop:

```python
                for call_index, call in enumerate(result.tool_calls):
                    if len(observations) >= max_iterations:
                        self._record_native_tool_calls_skipped_for_budget(
                            state,
                            skipped_count=len(result.tool_calls) - call_index,
                        )
                        if self._request_native_final_answer_after_tool_limit(
                            request,
                            state=state,
                            observations=observations,
                            native_calls=native_calls,
                            event_sink=event_sink,
                            iteration=iteration,
                            max_iterations=max_iterations,
                        ):
                            return state
                        self._set_native_runtime_max_iteration_response(
                            state,
                            observations=observations,
                            max_iterations=max_iterations,
                        )
                        return state

                    call_payload = call.model_dump(mode="json")
                    native_calls.append(call_payload)
                    state.request.metadata.setdefault("native_tool_calls", []).append(call_payload)
                    _record_native_tool_call_preamble(state, tool_name=call.name, content=result.response_text)
                    decision = native_tool_call_to_assistant_decision(call)
                    _record_native_decision_metadata(
                        state,
                        request=request,
                        decision=decision,
                        iteration=iteration,
                        max_iterations=max_iterations,
                        safety_notes=["native_tool_call"],
                    )
                    self._append_observability_event(
                        state,
                        canonical_event="react.decision",
                        node_name="native_runtime",
                        status=decision.type,
                        tool_name=decision.tool_name,
                        attributes={
                            "iteration": iteration + 1,
                            "batch_index": call_index + 1,
                            "batch_size": len(result.tool_calls),
                            "decision_type": decision.type,
                            "reason": decision.reason,
                            "safety_notes": decision.safety_notes,
                        },
                    )
                    validation = ActionValidator().validate(
                        decision=decision,
                        registry=self.registry,
                        request=request,
                        state=state,
                    )
                    state.request.metadata["last_action_validator"] = validation.model_dump(mode="json")
                    self._append_observability_event(
                        state,
                        canonical_event="action.validation.finished",
                        node_name="native_runtime",
                        status="accepted" if validation.accepted else "rejected",
                        tool_name=decision.tool_name,
                        attributes={
                            **validation.model_dump(mode="json"),
                            "batch_index": call_index + 1,
                            "batch_size": len(result.tool_calls),
                        },
                        error={"code": validation.code, "message": validation.message} if not validation.accepted else None,
                    )
                    if not validation.accepted:
                        observation = rejected_observation(
                            tool_name=decision.tool_name or "unknown",
                            error_code=validation.code,
                            error_message=validation.message,
                        ).model_dump(mode="json")
                        observations.append(observation)
                        state.request.metadata["native_runtime_observations"] = observations
                        _record_native_observation_metadata(state, observation)
                        _append_native_tool_observation_event(self, state, observation)
                        _set_native_validation_rejection_response(state, validation.model_dump(mode="json"))
                        return state

                    _emit_native_tool_progress_message(
                        state,
                        tool_name=decision.tool_name or call.name,
                        event_sink=event_sink,
                    )
                    tool_result = tool_executor.run_tool(
                        state,
                        decision.step_id or f"native_runtime_{len(observations) + 1}",
                        decision.tool_name or "",
                        decision.tool_input or {},
                        trace_store=self.trace_store,
                        trace_id=state.trace_id,
                        node_name="native_runtime",
                    )
                    observation = observation_from_tool_result(
                        tool_result,
                        request_text=request.text,
                        prior_observations=observations,
                    ).model_dump(mode="json")
                    observations.append(observation)
                    state.request.metadata["native_runtime_observations"] = observations
                    _record_native_observation_metadata(state, observation)
                    _append_native_tool_observation_event(self, state, observation)
                    if state.status == "failed":
                        return state
                    if len(observations) >= max_iterations:
                        remaining_calls = len(result.tool_calls) - call_index - 1
                        if remaining_calls:
                            self._record_native_tool_calls_skipped_for_budget(
                                state,
                                skipped_count=remaining_calls,
                            )
                        if self._request_native_final_answer_after_tool_limit(
                            request,
                            state=state,
                            observations=observations,
                            native_calls=native_calls,
                            event_sink=event_sink,
                            iteration=iteration,
                            max_iterations=max_iterations,
                        ):
                            return state
                        self._set_native_runtime_max_iteration_response(
                            state,
                            observations=observations,
                            max_iterations=max_iterations,
                        )
                        return state
                continue
```

- [x] **Step 2: Add budget skip metadata helper**

Add this method inside `AgentGraphRuntime`, near `_set_native_runtime_max_iteration_response(...)`:

```python
    def _record_native_tool_calls_skipped_for_budget(
        self,
        state: AgentState,
        *,
        skipped_count: int,
    ) -> None:
        if skipped_count <= 0:
            return
        metadata = state.request.metadata
        current = metadata.get("native_runtime_tool_calls_skipped_for_budget")
        previous = current if isinstance(current, int) and current >= 0 else 0
        metadata["native_runtime_tool_calls_skipped_for_budget"] = previous + skipped_count
        metadata["native_runtime_tool_call_budget_exhausted"] = True
```

- [x] **Step 3: Run focused tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_native_tool_call_handoff.py -q
```

Expected: all tests in `tests/test_native_tool_call_handoff.py` pass.

### Task 3: Validate Tool-Orchestration Scope

**Files:**
- Verify: `src/assistant_agent/agent/runtime.py`
- Verify: `tests/test_native_tool_call_handoff.py`

**Interfaces:**
- Consumes: implementation from Task 2.
- Produces: verified serial multi-tool orchestration change.

- [x] **Step 1: Run related native runtime tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase8a1_react_action_quality.py tests/test_phase8a2_react_final_answer_handoff.py tests/test_native_runtime_system_prompt_policy.py -q
```

Expected: all selected tests pass.

- [x] **Step 2: Run diff whitespace check**

Run:

```bash
git diff --check -- src/assistant_agent/agent/runtime.py tests/test_native_tool_call_handoff.py docs/superpowers/plans/2026-07-09-native-runtime-serial-multi-tool-calls.md
```

Expected: no output.

- [x] **Step 3: Review diff for scope**

Run:

```bash
git diff -- src/assistant_agent/agent/runtime.py tests/test_native_tool_call_handoff.py docs/superpowers/plans/2026-07-09-native-runtime-serial-multi-tool-calls.md
```

Expected: only serial native tool-call orchestration, focused tests, and this plan changed.

- [x] **Step 4: Commit implementation**

Run:

```bash
git add src/assistant_agent/agent/runtime.py tests/test_native_tool_call_handoff.py docs/superpowers/plans/2026-07-09-native-runtime-serial-multi-tool-calls.md
git commit -m "feat: execute serial native multi-tool batches"
```
