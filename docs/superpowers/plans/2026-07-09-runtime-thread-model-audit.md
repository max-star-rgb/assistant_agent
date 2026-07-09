# Runtime Thread Model Audit Implementation Plan

Date: 2026-07-09

## Goal

Complete Phase 5 of the runtime event-stream migration by auditing current
threading and blocking boundaries, then recording which areas should remain
synchronous and which areas should become async-native later.

This phase is documentation and architecture governance only. It should not
remove `asyncio.to_thread()` or rewrite runtime, tool, memory, or provider
business logic.

## Scope

- Read runtime, service, realtime backend, Gateway, tool executor, provider, and
  stream facade code.
- Identify production `asyncio.to_thread()` bridges and lock usage.
- Separate runtime-core thread bridges from scripts/tests.
- Document migration guidance for selective async.
- Update the runtime event-stream architecture with the Phase 5 conclusion.

## Non-Scope

- No tool-system rewrite.
- No memory-system rewrite.
- No provider SDK replacement.
- No Gateway wire protocol changes.
- No removal of compatibility sync injection paths.

## Findings

- Production `asyncio.to_thread()` use is narrow and intentional:
  `AgentGraphRuntime.run_stream()`,
  `run_assistant_request_stream()`, and the realtime backend compatibility
  wrapper for synchronous `run_request=` injection.
- Gateway and realtime transports are already native async where that matters.
- Tool retry sleep is synchronous but cancel-aware and runs inside the sync
  runtime worker.
- Provider adapters contain several legitimate sync/blocking calls through
  sync SDKs or `urllib`.
- The highest-value async-native candidate is LLM streaming, not generic tool
  or memory conversion.

## Deliverables

- `docs/runtime-thread-model-audit.md`
- Phase 5 section in `docs/runtime-event-stream-architecture.md`

## Validation

Because this phase is documentation-only, validation should check formatting and
run the fast test suite to confirm no accidental behavior changes:

```bash
git diff --check
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```
