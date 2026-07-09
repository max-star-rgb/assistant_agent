# Provider Event Boundary Design

Date: 2026-07-09

## Purpose

Phase 6 defines the internal LLM provider event boundary for the runtime event
stream migration. It does not implement new runtime behavior yet.

## Decision

Introduce a provider-neutral `LLMEvent` contract before attempting async-native
provider streaming. Keep `ChatAdapter.chat()`, `ChatResult`, and
`ChatRequest.stream_callback` compatible while the provider parser moves from
vendor chunks to internal events.

## Scope

- Define `LLMEvent` as a provider-boundary event.
- Keep `AgentEvent` as the runtime event protocol.
- Keep Gateway and realtime frames unchanged.
- Keep tool execution behind the existing governance boundary.
- Keep final provider output represented by `ChatResult`.

## Architecture

Current provider streaming:

```text
vendor chunk
  -> text callback
  -> AgentEvent(response_delta)
```

Target provider boundary:

```text
vendor chunk
  -> LLMEvent
  -> ChatResult accumulator
  -> optional AgentEvent(response_delta) mapping
```

`LLMEvent` should support token deltas, tool-call deltas, completion metadata,
and provider errors. It should not contain session ids, run ids, Gateway fields,
TTS fields, UI fields, or raw vendor chunks.

## Implementation Direction

The next implementation phase should add the schema and accumulator first, then
refactor the OpenAI-compatible stream parser to emit `LLMEvent` internally while
preserving legacy callback output. Only after that should an optional async
`stream_chat()` path be considered.

## Review

No placeholders, no public protocol breaks, and no async-everywhere migration are
part of this design.
