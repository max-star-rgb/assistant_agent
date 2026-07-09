# Native Runtime Final-Only Handoff Design

Date: 2026-07-09

## Goal

Harden the provider-native assistant loop when the run reaches its configured tool-call iteration limit.

Today, `AgentGraphRuntime._run_native_runtime()` can execute the final allowed tool call and then fall out of the loop with a fixed local message such as "已达到最大工具调用次数". That loses the strongest available evidence: the last tool observation. The target behavior is to let the model produce one final no-tool answer from the accumulated observations after the final allowed tool call completes.

## Scope

This design covers only the narrow runtime hardening path selected as phase A1:

- Execute the final allowed native tool call exactly as today.
- Convert the tool result into the normal prompt-safe observation.
- Immediately ask the chat adapter for one final answer with tools disabled.
- Return that final answer if usable.
- Fall back to the existing local max-iteration response if the final-only handoff fails.

This design does not add provider retry/backoff, 70%/90% budget warnings, parallel tool execution, or large tool-result persistence.

## Current Behavior

The native runtime loop is implemented in `src/assistant_agent/agent/runtime.py`.

The current flow is:

```text
for iteration in range(max_tool_iterations):
  build context pack
  call chat adapter with tools
  if provider returns tool_calls:
    normalize first tool call
    validate through ActionValidator
    execute through ToolExecutor
    append tool observation
    continue
  else:
    set final response from provider content
    return

set fixed local "max tool calls reached" response
```

The fixed fallback is safe, but it is lower quality than a final model answer grounded in the just-created observations.

## Proposed Behavior

When a provider response returns a tool call on the last allowed iteration:

1. Keep the existing tool governance path unchanged:
   `ActionValidator -> ToolExecutor -> ToolRegistry`.
2. Convert the `ToolResult` through `observation_from_tool_result(...)`.
3. Record the same native observation metadata and trace event as today.
4. Detect that `iteration + 1 >= max_iterations`.
5. Build a final-only chat request from the updated observations.
6. Send that request with no tool schemas and `tool_choice="none"`.
7. If the provider returns content or refusal without tool calls, set the final response from that result.
8. If the provider returns another tool call or an error, return the existing max-iteration fallback response with explicit metadata explaining why the handoff was not used.

The model gets one extra chat call only after the final allowed tool execution. The runtime does not permit another tool execution.

## Architecture

### Runtime

Add a helper in `AgentGraphRuntime`, for example:

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
    ...
```

The helper returns `True` when it sets `state.response`; otherwise the caller uses the existing max-iteration fallback.

### Context

The helper reuses `build_traced_assistant_context_pack(...)` and `render_native_user_message(...)` so observation compaction, context budget handling, trace summaries, and prompt-safe data boundaries remain centralized in the context services.

The final-only request should use:

```text
tools=[]
tool_choice="none"
system prompt profile=final_only
messages=[
  system final-only instruction,
  rendered context/user message,
  prior assistant tool_call messages,
  prior tool observation messages
]
```

The request must preserve the native tool call and tool observation pairings so provider-native message history remains valid.

### Tool Governance

No tool execution behavior changes. All tool calls still pass through:

```text
AssistantDecision -> ActionValidator -> ToolExecutor -> ToolRegistry
```

The final-only handoff is explicitly a no-tool response request, not a second tool-selection opportunity.

## Error Handling

The final-only handoff is best effort.

- If the handoff returns final content or refusal, the runtime uses `_set_native_runtime_response(...)`.
- If the handoff returns `tool_calls`, the runtime refuses to execute them and falls back to the max-iteration response.
- If the handoff returns provider errors, the runtime falls back to the max-iteration response.
- The fallback response data records a prompt-safe reason such as `final_only_returned_tool_call` or `final_only_handoff_failed`.
- Unexpected exceptions should not be introduced; the chat adapter already normalizes provider failures to `ChatResult.errors`.

## Observability

The runtime should emit or record enough metadata to debug the handoff:

- The second chat call appears in provider budget as `direct_chat` because it is a final answer call.
- `llm.chat.finished` trace records the final-only call status, message kind, finish reason, and tool call count.
- `context.report` records that selected tool count is zero.
- `state.response.data` includes:
  - `native_runtime=True`
  - `tool_count`
  - `tool_observations`
  - `final_only_handoff=True` when used successfully
  - `final_only_handoff_failed` or `final_only_returned_tool_call` when falling back

## Tests

Add focused tests in `tests/test_native_tool_call_handoff.py`.

Required cases:

1. With `max_tool_iterations=1`, the first provider response returns a tool call. The runtime executes that tool and then makes a second final-only chat request.
2. The second request has `tools == []` and `tool_choice == "none"`.
3. The final response comes from the second provider response, not from the fixed max-iteration fallback.
4. If the second provider response returns another tool call, no second tool is executed and the runtime falls back with `final_only_returned_tool_call` metadata.
5. If the second provider response fails, the runtime falls back with `final_only_handoff_failed` metadata.

Validation command:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_native_tool_call_handoff.py -q
```

Run `git diff --check` for touched files before completion.

## Non-Goals

- No provider retry or jittered backoff.
- No fallback provider chain.
- No budget warning injection into tool observations.
- No parallel native tool execution.
- No new tool or ToolSpec.
- No Gateway wire protocol change.
- No change to mock/local/offline defaults.
