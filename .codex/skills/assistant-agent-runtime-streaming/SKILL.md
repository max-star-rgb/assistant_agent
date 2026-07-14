---
name: assistant-agent-runtime-streaming
description: Use when Codex needs to design, review, debug, document, or modify assistant_agent runtime event streams, AgentRunStream, LLMEvent, provider chat streaming, stream/result separation, worker-thread bridges, or shared assistant run streaming.
---

# Assistant Agent Runtime Streaming

Use this skill for runtime and provider streaming work. `docs/runtime-event-stream-architecture.md` is the current authority; Gateway wire/session lifecycle remains owned by `docs/gateway-architecture.md` and `.codex/skills/assistant-runtime-reference`.

## Start

1. Read `AGENTS.md` and `docs/runtime-event-stream-architecture.md`.
2. Inspect the task-relevant source and tests before editing.
3. Read Gateway authority only when frame mapping, session lifecycle, cancel/interrupt, WebSocket, or realtime delivery changes.

## Source Map

- `src/assistant_agent/schemas/llm_events.py`: provider-neutral `LLMEvent` and accumulator.
- `src/assistant_agent/services/chat_adapter.py`: sync chat plus native async provider streaming.
- `src/assistant_agent/agent/provider_streaming.py`, `llm_event_mapping.py`: provider event consumption and `AgentEvent` mapping.
- `src/assistant_agent/agent/event_stream.py`, `runtime.py`: `AgentRunStream`, queue bridge, runtime facade.
- `src/assistant_agent/services/assistant_run_service.py`: shared service-level stream and final artifacts.
- `src/assistant_agent/realtime/`: `AgentEvent` to realtime event mapping and backend consumption.

## Working Rules

- Keep `LLMEvent`, `AgentEvent`, realtime events, and Gateway frames as distinct contracts.
- Keep streaming events separate from terminal `AgentState`/`AssistantRunArtifacts`/realtime results.
- Never write directly to `asyncio.Queue` from worker threads; cross the loop with thread-safe scheduling.
- Keep sync adapters compatible while selective async migration has measurable value.
- Do not force-cancel blocking SDK/tool calls; preserve cooperative cancellation and document limits.
- Keep provider chunks inside adapters and tool/memory calls behind existing governance boundaries.
- Update the authority when contracts, source ownership, threading, cancellation, or validation changes.

## Validation

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_agent_runtime_stream.py tests/test_async_chat_stream.py \
  tests/test_llm_events.py tests/test_runtime_provider_streaming.py \
  tests/test_shared_assistant_run_service.py -q
git diff --check -- AGENTS.md README.md docs/runtime-event-stream-architecture.md \
  .codex/skills/assistant-agent-runtime-streaming src tests
```
