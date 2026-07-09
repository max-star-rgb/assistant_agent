# Native Runtime Serial Multi-Tool Calls Design

Date: 2026-07-09

## Goal

Make the provider-native runtime handle multiple tool calls returned in a single model response without bypassing tool governance.

Today, `AgentGraphRuntime._run_native_runtime()` only executes `result.tool_calls[0]`. If a provider returns two or more native tool calls in one response, the later calls are ignored. The target behavior is to execute accepted tool calls in provider order, append one observation per tool, and then continue the normal ReAct loop.

## Scope

This design covers only safe serial orchestration for provider-native tool calls:

- Execute multiple native tool calls from one provider response in original order.
- Keep every tool execution behind `ActionValidator -> ToolExecutor -> ToolRegistry`.
- Preserve existing progress, trace, event, provider-budget, risk-gate, and observation behavior per tool.
- Preserve the final-only handoff added for the last allowed tool-call budget.

This design does not add parallel execution, tool safety white/blacklists, path conflict detection, new ToolSpecs, Gateway behavior, provider retry/backoff, or large-result persistence.

## Current Behavior

Current native runtime flow:

```text
LLM returns tool_calls=[call_1, call_2, ...]
runtime selects call_1 only
call_1 -> AssistantDecision -> ActionValidator -> ToolExecutor -> observation
call_2 and later calls are ignored
next LLM call sees only call_1 + observation
```

This is safe but incomplete. It loses useful model intent and can make a task require extra LLM round trips or produce an answer from partial evidence.

## Proposed Behavior

When a provider-native response returns `tool_calls`:

1. Discard first-call streamed preamble exactly as today.
2. Iterate through `result.tool_calls` in order.
3. For each call:
   - Save the native call payload in `native_calls` and `request.metadata["native_tool_calls"]`.
   - Record native preamble metadata.
   - Convert the call to `AssistantDecision`.
   - Record decision metadata and `react.decision` trace.
   - Validate with `ActionValidator`.
   - If rejected, append a rejected observation, record observation metadata/event, set the existing validation rejection response, and stop the batch.
   - If accepted, emit the existing progress message, execute through `ToolExecutor`, convert to observation, and append it.
4. If execution fails or cancellation marks state failed/cancelled, stop immediately.
5. If the run reaches `max_tool_iterations`, call the existing final-only handoff with all accumulated observations.
6. If budget remains after the batch, continue to the next LLM iteration.

## Budget Semantics

`max_tool_iterations` should remain a cap on executed or attempted tool turns, not a loophole for one large provider batch.

The runtime should count one budget unit per appended tool observation, including rejected observations. For a batch larger than the remaining budget:

- Execute calls until the remaining budget is exhausted.
- Do not execute or validate the rest of the batch.
- Record prompt-safe metadata such as `native_runtime_tool_calls_skipped_for_budget`.
- Trigger the final-only handoff using the observations that were produced.

This preserves the existing safety intent of the tool-call limit while allowing efficient batches within that limit.

## Ordering

Ordering is part of the behavior contract:

- Tool calls execute in provider order.
- Native assistant tool-call messages are reconstructed in that same order.
- Tool observation messages are appended in the same order as their corresponding calls.
- The next LLM request sees valid assistant/tool message pairs for every executed or rejected call.

No parallel execution is introduced in this phase.

## Error Handling

- Validator rejection stops the current batch and returns the existing validation rejection response.
- Tool failure behavior stays owned by `ToolExecutor` and existing runtime state checks.
- Cancellation behavior stays owned by `ToolExecutor` and `AgentGraphRuntime`.
- If a later call in the batch is not reached because of budget exhaustion, it is not executed and not represented as a tool observation.
- Provider responses with no valid tool calls keep existing final-answer/error behavior.

## Observability

The implementation should preserve existing per-tool trace and event behavior:

- One `react.decision` trace per processed native tool call.
- One `action.validation.finished` trace per validated call.
- Existing `tool_started`, `tool_finished` / `tool_failed`, and `tool_observation` events per executed call.
- `native_tool_calls` metadata contains only processed calls.
- `native_runtime_observations` contains one observation per processed call.
- If calls are skipped due to budget, metadata records the skipped count without storing raw provider payloads.

## Tests

Add focused tests in `tests/test_native_tool_call_handoff.py`.

Required cases:

1. A provider response with `product_search` and `price_compare` executes both tools in order.
2. The next LLM request contains two `tool` messages in the same order.
3. If the first tool call is rejected, the second call is not executed.
4. If only one tool budget remains and the provider returns two tool calls, only the first call executes and the existing final-only handoff runs.
5. If enough budget remains and the last call in a multi-call batch consumes the budget, final-only handoff sees all produced observations.

Validation command:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_native_tool_call_handoff.py -q
```

Run related native-runtime regressions after implementation:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase8a1_react_action_quality.py tests/test_phase8a2_react_final_answer_handoff.py tests/test_native_runtime_system_prompt_policy.py -q
```

## Non-Goals

- No parallel execution.
- No tool batch safety whitelist or path conflict analysis.
- No change to `ToolExecutor` public contract.
- No new tool registration.
- No provider retry/backoff.
- No Gateway or realtime protocol change.
- No real provider calls in tests.
