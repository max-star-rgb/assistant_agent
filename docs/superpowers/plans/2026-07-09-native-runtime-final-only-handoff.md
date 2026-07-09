# Native Runtime Final-Only Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the provider-native runtime ask for one final no-tool answer after executing the last allowed tool call, instead of immediately returning the fixed max-iteration fallback.

**Architecture:** Keep `AgentGraphRuntime._run_native_runtime()` as the owner of the native provider loop. Add a small final-only handoff helper in `src/assistant_agent/agent/runtime.py` that reuses existing context rendering, native tool-call history reconstruction, trace reporting, chat-call observability, and final response setting. Keep all tool execution behind `ActionValidator -> ToolExecutor -> ToolRegistry`.

**Tech Stack:** Python 3, pytest, existing `AgentGraphRuntime`, `ChatRequest` / `ChatResult`, `ToolExecutor`, `build_traced_assistant_context_pack`, `render_native_user_message`, `ProviderConfig`.

## Global Constraints

- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for Python and pytest commands.
- Do not add dependencies.
- Do not call real providers; use scripted adapters and mock/local/offline defaults.
- Do not change Gateway wire protocol, ToolSpec definitions, memory policy, or provider selection.
- Do not add retry/backoff, budget warning injection, parallel tool execution, or tool-result persistence.
- Preserve `ActionValidator -> ToolExecutor -> ToolRegistry` for every tool execution.
- Preserve prompt-safe context construction through context services.

---

## File Structure

- Modify `src/assistant_agent/agent/runtime.py`: add final-only handoff helper, max-iteration fallback helper, and the branch that invokes handoff after the final allowed tool call.
- Modify `tests/test_native_tool_call_handoff.py`: add focused regression tests for successful handoff, returned-tool-call fallback, and provider-error fallback.
- No docs update is required beyond the approved spec and this implementation plan.

### Task 1: Add Final-Only Handoff Regression Tests

**Files:**
- Modify: `tests/test_native_tool_call_handoff.py`

**Interfaces:**
- Consumes: `AgentGraphRuntime.run_state(request)`, `ProviderConfig(max_tool_iterations=1)`, `NativeToolChatAdapter`, `native_result(...)`, `final_result(...)`.
- Produces: failing tests that define final-only handoff behavior before implementation.

- [x] **Step 1: Add provider-error import**

At the top of `tests/test_native_tool_call_handoff.py`, update the chat adapter import:

```python
from assistant_agent.services.chat_adapter import ChatProviderError, ChatRequest, ChatResult, ProviderChatCapabilities
```

- [x] **Step 2: Add successful final-only handoff test**

Append this test near the existing native-runtime max-iteration and handoff tests:

```python
def test_native_runtime_requests_final_only_answer_after_last_allowed_tool_call() -> None:
    adapter = NativeToolChatAdapter(
        [
            native_result("product_search", {"query": "通勤耳机", "limit": 2}),
            final_result("这是基于最后一次工具 observation 的最终回答。"),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(max_tool_iterations=1),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机"))

    assert adapter.calls == 2
    assert adapter.requests[0].tools
    assert adapter.requests[0].tool_choice == "auto"
    assert adapter.requests[1].tools == []
    assert adapter.requests[1].tool_choice == "none"
    assert any(message["role"] == "tool" for message in adapter.requests[1].messages)
    assert [call.tool_name for call in state.tool_calls] == ["product_search"]
    assert state.response is not None
    assert state.response.message == "这是基于最后一次工具 observation 的最终回答。"
    assert state.response.data["final_only_handoff"] is True
    assert state.response.data["tool_observations"] == 1
    assert state.provider_budget.call_records[-1].capability == "direct_chat"
```

- [x] **Step 3: Add returned-tool-call fallback test**

Append this test:

```python
def test_native_runtime_final_only_handoff_refuses_additional_tool_call() -> None:
    adapter = NativeToolChatAdapter(
        [
            native_result("product_search", {"query": "通勤耳机", "limit": 2}),
            native_result("price_compare", {"query": "通勤耳机", "limit": 2}),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(max_tool_iterations=1),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机并比较价格"))

    assert adapter.calls == 2
    assert adapter.requests[1].tools == []
    assert adapter.requests[1].tool_choice == "none"
    assert [call.tool_name for call in state.tool_calls] == ["product_search"]
    assert state.response is not None
    assert state.response.message == "已达到最大工具调用次数 (1)，这是我能提供的最好回答。"
    assert state.response.data["final_only_returned_tool_call"] is True
    assert state.response.data["final_only_handoff_failed"] is False
    assert state.provider_budget.call_records[-1].capability == "direct_chat"
```

- [x] **Step 4: Add provider-error fallback test**

Append this test:

```python
def test_native_runtime_final_only_handoff_provider_error_falls_back() -> None:
    adapter = NativeToolChatAdapter(
        [
            native_result("product_search", {"query": "通勤耳机", "limit": 2}),
            ChatResult(
                provider="scripted-native",
                model="native-test",
                errors=[
                    ChatProviderError(
                        code="provider_timeout",
                        message="timeout",
                        recoverable=True,
                    )
                ],
            ),
        ]
    )
    runtime = AgentGraphRuntime(
        config=ProviderConfig(max_tool_iterations=1),
        chat_adapter=adapter,
    )

    state = runtime.run_state(UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机"))

    assert adapter.calls == 2
    assert adapter.requests[1].tools == []
    assert adapter.requests[1].tool_choice == "none"
    assert [call.tool_name for call in state.tool_calls] == ["product_search"]
    assert state.response is not None
    assert state.response.message == "已达到最大工具调用次数 (1)，这是我能提供的最好回答。"
    assert state.response.data["final_only_handoff_failed"] is True
    assert state.response.data["final_only_error_code"] == "provider_timeout"
```

- [x] **Step 5: Run tests and verify red**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_native_tool_call_handoff.py -q
```

Expected: the new tests fail because the runtime only makes one chat call at `max_tool_iterations=1` and returns the fixed max-iteration fallback.

### Task 2: Implement Native Runtime Final-Only Handoff

**Files:**
- Modify: `src/assistant_agent/agent/runtime.py`

**Interfaces:**
- Consumes: `self._native_runtime_chat_request(...)`, `self.chat_adapter.chat(...)`, `self._record_native_runtime_chat_call(...)`, `self._append_observability_event(...)`, `self._record_native_runtime_context_report(...)`, `_native_runtime_tool_call_payload(...)`, `_system_prompt_options_from_request(...)`.
- Produces:
  - `AgentGraphRuntime._request_native_final_answer_after_tool_limit(...) -> bool`
  - `AgentGraphRuntime._set_native_runtime_max_iteration_response(...) -> None`
  - `AgentGraphRuntime._record_native_runtime_chat_call(..., capability: str | None = None) -> None`

- [x] **Step 1: Replace final allowed tool `continue` with handoff branch**

In `AgentGraphRuntime._run_native_runtime()`, immediately after `_append_native_tool_observation_event(self, state, observation)` and the `if state.status == "failed": return state` check, replace the unconditional `continue` with:

```python
                if iteration + 1 >= max_iterations:
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

- [x] **Step 2: Replace loop-exhaustion fixed response with helper call**

At the end of `_run_native_runtime()`, replace the current `state.set_response(AgentResponse(...))` max-iteration block with:

```python
        self._set_native_runtime_max_iteration_response(
            state,
            observations=observations,
            max_iterations=max_iterations,
        )
        return state
```

- [x] **Step 3: Add final-only handoff helper**

Add this method inside `AgentGraphRuntime`, near `_native_runtime_chat_request(...)`:

```python
    def _request_native_final_answer_after_tool_limit(
        self,
        request: UserRequest,
        *,
        state: AgentState,
        observations: list[dict[str, Any]],
        native_calls: list[dict[str, Any]],
        event_sink: EventSink | None,
        iteration: int,
        max_iterations: int,
    ) -> bool:
        stream_callback = _native_runtime_stream_callback(state, event_sink)
        chat_request = self._native_runtime_final_only_chat_request(
            request,
            state=state,
            observations=observations,
            native_calls=native_calls,
            stream_callback=stream_callback,
            iteration=iteration,
            max_iterations=max_iterations,
        )
        chat_started_at = perf_counter()
        result = self.chat_adapter.chat(chat_request)
        chat_wall_latency_ms = int((perf_counter() - chat_started_at) * 1000)
        self._record_native_runtime_chat_call(
            state,
            request,
            result,
            capability="direct_chat",
        )
        self._append_observability_event(
            state,
            canonical_event="llm.chat.finished",
            node_name="native_runtime",
            status="succeeded" if result.success else "failed",
            provider=result.provider,
            model=result.model,
            latency_ms=result.latency_ms,
            attributes={
                "iteration": iteration + 2,
                "max_iterations": max_iterations,
                "final_only_handoff": True,
                "message_kind": result.message_kind,
                "finish_reason": result.finish_reason,
                "tool_call_count": len(result.tool_calls),
                "provider_latency_ms": result.latency_ms,
                "wall_latency_ms": chat_wall_latency_ms,
                "usage": result.usage,
            },
            error=_chat_result_error(result),
        )
        if result.success and not result.tool_calls:
            self._set_native_runtime_response(state, result, observations)
            if state.response is not None:
                state.response.data["final_only_handoff"] = True
            return True
        metadata = state.request.metadata
        if result.tool_calls:
            metadata["native_runtime_final_only_returned_tool_call"] = True
            metadata["native_runtime_final_only_handoff_failed"] = False
        else:
            metadata["native_runtime_final_only_handoff_failed"] = True
        if result.errors:
            metadata["native_runtime_final_only_error_code"] = result.errors[0].code
        return False
```

- [x] **Step 4: Add final-only chat request builder**

Add this method below `_request_native_final_answer_after_tool_limit(...)`:

```python
    def _native_runtime_final_only_chat_request(
        self,
        request: UserRequest,
        *,
        state: AgentState,
        observations: list[dict[str, Any]],
        native_calls: list[dict[str, Any]],
        stream_callback: Any | None,
        iteration: int,
        max_iterations: int,
    ) -> ChatRequest:
        context_pack = build_traced_assistant_context_pack(
            trace_store=self.trace_store,
            trace_id=state.trace_id,
            node_name="native_runtime",
            state=state,
            request=request,
            observations=observations,
            tool_specs=[],
            iteration=iteration + 1,
            max_iterations=max_iterations,
            context_compactor=None,
        )
        system_prompt = render_system_instruction(
            SystemPromptProfile.FINAL_ONLY,
            options=_system_prompt_options_from_request(request),
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": render_native_user_message(context_pack)},
        ]
        for index, observation in enumerate(observations):
            call = native_calls[index] if index < len(native_calls) else {}
            tool_call_payload = _native_runtime_tool_call_payload(call, observation, index)
            messages.append({"role": "assistant", "content": None, "tool_calls": [tool_call_payload]})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_payload["id"],
                    "name": tool_call_payload["function"]["name"],
                    "content": json.dumps(observation, ensure_ascii=False),
                }
            )
        self._record_native_runtime_context_report(
            state,
            context_pack=context_pack,
            system_prompt=system_prompt,
            selected_tool_specs=[],
            iteration=iteration + 1,
            max_iterations=max_iterations,
        )
        return ChatRequest(
            user_id=state.user_id,
            session_id=state.session_id,
            user_query=request.text or "native runtime final answer",
            messages=messages,
            tools=[],
            tool_choice="none",
            temperature=0.2,
            max_tokens=1024,
            stream_callback=stream_callback,
        )
```

- [x] **Step 5: Add max-iteration fallback helper**

Add this method near `_set_native_runtime_response(...)`:

```python
    def _set_native_runtime_max_iteration_response(
        self,
        state: AgentState,
        *,
        observations: list[dict[str, Any]],
        max_iterations: int,
    ) -> None:
        metadata = state.request.metadata
        state.set_response(
            AgentResponse(
                message=f"已达到最大工具调用次数 ({max_iterations})，这是我能提供的最好回答。",
                data={
                    "native_runtime": True,
                    "tool_count": len(state.tool_calls),
                    "tool_observations": len(observations),
                    "final_only_handoff_failed": bool(metadata.get("native_runtime_final_only_handoff_failed")),
                    "final_only_returned_tool_call": bool(metadata.get("native_runtime_final_only_returned_tool_call")),
                    "final_only_error_code": metadata.get("native_runtime_final_only_error_code"),
                    "provider_budget": state.provider_budget.summary(),
                },
            )
        )
```

- [x] **Step 6: Run focused tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_native_tool_call_handoff.py -q
```

Expected: all tests in `tests/test_native_tool_call_handoff.py` pass.

### Task 3: Validate Runtime Hardening Scope

**Files:**
- Verify: `src/assistant_agent/agent/runtime.py`
- Verify: `tests/test_native_tool_call_handoff.py`

**Interfaces:**
- Consumes: implementation from Task 2.
- Produces: verified, diff-clean runtime hardening change.

- [x] **Step 1: Run related context/action tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase8a1_react_action_quality.py tests/test_phase8a2_react_final_answer_handoff.py tests/test_native_runtime_system_prompt_policy.py -q
```

Expected: all selected tests pass.

- [x] **Step 2: Run diff whitespace check**

Run:

```bash
git diff --check -- src/assistant_agent/agent/runtime.py tests/test_native_tool_call_handoff.py docs/superpowers/plans/2026-07-09-native-runtime-final-only-handoff.md
```

Expected: no output.

- [x] **Step 3: Review diff for scope**

Run:

```bash
git diff -- src/assistant_agent/agent/runtime.py tests/test_native_tool_call_handoff.py docs/superpowers/plans/2026-07-09-native-runtime-final-only-handoff.md
```

Expected: only final-only handoff code, focused tests, and this plan changed.

- [x] **Step 4: Commit implementation**

Run:

```bash
git add src/assistant_agent/agent/runtime.py tests/test_native_tool_call_handoff.py docs/superpowers/plans/2026-07-09-native-runtime-final-only-handoff.md
git commit -m "feat: add native runtime final-only handoff"
```
