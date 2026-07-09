# Provider Event Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the provider-neutral `LLMEvent` boundary without breaking existing chat adapter callbacks or runtime `AgentEvent` consumers.

**Architecture:** Add a small provider-event schema and accumulator, route OpenAI-compatible streaming chunks through it, then adapt token deltas back into the existing callback and `AgentEvent(response_delta)` behavior. Keep `ChatAdapter.chat()` and `ChatResult` as compatibility contracts.

**Tech Stack:** Python 3.12, Pydantic, pytest, existing `ChatAdapter`, `ChatRequest`, `ChatResult`, and `AgentEvent`.

## Global Constraints

- Do not change Gateway wire frame names.
- Do not replace `AgentEvent`.
- Do not remove `ChatRequest.stream_callback`.
- Do not expose raw vendor chunks outside provider adapters.
- Do not bypass `ActionValidator -> ToolExecutor -> ToolRegistry`.
- Keep tests mock/local/offline by default.

---

### Task 1: Add LLMEvent Schema And Accumulator

**Files:**
- Create: `src/assistant_agent/schemas/llm_events.py`
- Test: `tests/test_llm_events.py`

**Interfaces:**
- Produces: `LLMEvent`, `LLMToolCallDelta`, `LLMProviderError`, `LLMEventAccumulator`

- [x] **Step 1: Write schema and accumulator tests**

Create tests for token aggregation, tool-call argument aggregation by index,
completion metadata, and provider error events.

- [x] **Step 2: Run tests to verify they fail**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_llm_events.py -q
```

Expected: fail because `assistant_agent.schemas.llm_events` does not exist.

- [x] **Step 3: Implement schema and accumulator**

Implement the minimum contract from
`docs/provider-event-boundary-architecture.md`.

- [x] **Step 4: Run tests to verify they pass**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_llm_events.py -q
```

Expected: all tests pass.

### Task 2: Refactor OpenAI-Compatible Stream Parsing Internally

**Files:**
- Modify: `src/assistant_agent/services/chat_adapter.py`
- Test: `tests/test_direct_chat_adapter.py`

**Interfaces:**
- Consumes: `LLMEvent`, `LLMToolCallDelta`, `LLMEventAccumulator`
- Preserves: `ChatRequest.stream_callback`, `ChatResult`

- [x] **Step 1: Add failing tests for internal LLMEvent emission**

Extend focused stream tests so token deltas and tool-call deltas can be asserted
through the accumulator without changing legacy callback output.

- [x] **Step 2: Run focused chat adapter tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_direct_chat_adapter.py::test_stream_chunks_aggregate_content tests/test_direct_chat_adapter.py::test_stream_chunks_aggregate_tool_call_arguments -q
```

Expected: existing tests pass before refactor; new internal-event tests fail
until the parser is updated.

- [x] **Step 3: Route vendor chunks through LLMEvent**

Refactor `_parse_openai_chat_stream()` so it converts provider chunks into
`LLMEvent` records, feeds them into `LLMEventAccumulator`, and adapts
`token_delta` events back into the existing legacy callback payload.

- [x] **Step 4: Run focused chat adapter tests again**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_direct_chat_adapter.py -q
```

Expected: all direct chat adapter tests pass.

### Task 3: Add Runtime Mapping Helper

**Files:**
- Create or modify: `src/assistant_agent/agent/llm_event_mapping.py`
- Modify: `src/assistant_agent/agent/runtime.py`
- Modify: `src/assistant_agent/agent/assistant_loop_nodes.py`
- Modify: `src/assistant_agent/agent/graph_nodes.py`
- Test: `tests/test_agent_events.py`

**Interfaces:**
- Consumes: `LLMEvent(token_delta)`
- Produces: existing `AgentEvent(type="response_delta")`

- [x] **Step 1: Write mapping tests**

Assert that token deltas map to `response_delta` with provider/model payload
preserved, and tool-call deltas do not produce user-visible runtime events in
V1.

- [x] **Step 2: Implement mapper**

Add a small helper used by existing callback sites. The helper should keep
Runtime-owned `source` values explicit.

- [x] **Step 3: Run runtime event tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_events.py tests/test_native_tool_call_handoff.py::test_native_tool_call_emits_replaceable_progress_and_suppresses_first_call_content -q
```

Expected: all selected tests pass and streamed first-call tool preamble remains
suppressed.

### Task 4: Full Fast Verification

**Files:**
- No new files

**Interfaces:**
- Verifies all prior tasks

- [x] **Step 1: Run formatting and fast tests**

Run:

```bash
git diff --check
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

Expected: no whitespace errors and all fast tests pass.
