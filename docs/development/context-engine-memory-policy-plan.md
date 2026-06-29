# Context Engine + Memory Policy Development Plan

Last updated: 2026-06-29

This is the step-by-step development plan for the mixed context mechanism:

```text
Rules trigger compaction.
Compactor writes session summary.
MemoryWritePolicy decides durable memory.
Default runtime stays mock/local/offline.
```

Use this document for follow-up implementation work. It is a development plan, not the architecture source. Before changing code, read:

- `docs/CODEX_PROJECT_GUIDE.md`
- `docs/CONTEXT_ENGINEERING_STATUS.md`
- `docs/memory-service-architecture.md`
- `docs/development.md`

If the task touches architecture ownership, also read `docs/architecture-layers.md`.

## Current Baseline

Implemented baseline:

- `ContextPolicy` owns default context thresholds:
  - `max_context_chars=12000`
  - `compact_at_ratio=0.80`
  - `hard_compact_at_ratio=0.92`
  - `keep_recent_turns=2`
  - `max_tool_result_chars=1200`
  - `max_memory_context_chars=500`
- `CompactionPolicy` triggers on high usage, over budget, large tool observations, provider overflow metadata, and explicit `/compact` / `compact_context=True`.
- `ContextSummary` is session-scoped context, not long-term memory.
- `ContextCompactor` has deterministic default behavior.
- `LLMCompactor` exists behind explicit provider profiles and falls back to deterministic output when schema validation fails.
- `ConversationStore` can persist turns and session summary.
- `reset_conversation=True` clears both turns and session summary.
- `MemoryPromotionCandidate` is separate from memory writes.
- `MemoryWritePolicy` defaults block automatic long-term promotion.
- Trace context exposes compaction and summary observability fields.

## Execution Rules

Follow these rules for every phase:

1. Keep default profile mock/local/offline.
2. Do not call real providers unless the task explicitly requests smoke/pilot validation.
3. Do not write raw provider responses, media bodies, base64, secrets, API keys, tokens, or real user data.
4. Do not let assistant/LLM directly decide persistence; route durable writes through `MemoryWritePolicy`.
5. Do not put memory retrieval, ranking, TTL, dedupe, profile merge, or store selection into context builders.
6. Do not put prompt rendering, tool observation compaction, session summary, or global context budget into `MemoryManager`.
7. After each phase, update this file with status, changed files, validation commands, and remaining risks.

## Phase 1: Policy And Deterministic Session Summary

Status: done.

Goal:

- Centralize context thresholds and compaction triggers.
- Preserve recent turns verbatim.
- Summarize older turns into session-scoped `context_summary`.
- Keep summary out of long-term memory.

Implemented files:

- `src/multimodal_agent/schemas/context.py`
- `src/multimodal_agent/services/context/policy.py`
- `src/multimodal_agent/services/context/compactor.py`
- `src/multimodal_agent/services/context/builder.py`
- `src/multimodal_agent/services/context/renderer.py`
- `src/multimodal_agent/services/assistant_run_service.py`

Regression tests:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_assistant_context_renderer.py \
  tests/test_conversation_context_compaction.py \
  tests/test_shared_assistant_run_service.py -q
```

## Phase 2: Session Transcript Persistence And Recovery

Status: done.

Goal:

- Persist normal turns and session summary in `ConversationStore`.
- Restore summary before recent turns on the next request.
- Clear turns and summary together on reset.

Acceptance checks:

- Summary persists in in-memory and JSONL conversation stores.
- Next turn receives `context_summary` and recent turn context.
- `reset_conversation=True` starts without old turns or summary.
- Session summary does not create a `MemoryItem`.

Regression tests:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_shared_assistant_run_service.py \
  tests/test_memory_runtime_integration.py \
  tests/test_memory_snapshot_api.py -q
```

## Phase 3: LLM Compactor Hardening

Status: partial.

Already done:

- `LLMCompactor` calls the configured `ChatAdapter`.
- It is selected only under `provider_smoke` or `pilot` when chat adapter is not mock.
- `SummaryValidator` checks schema and rejects raw/secret-like payloads.
- Invalid output falls back to deterministic compactor.
- Trace records `compactor_type`.

Next steps:

1. Add an opt-in provider smoke script or test that uses a fake real adapter to exercise the LLM compactor path without network.
2. Add provider context overflow retry handling:
   - detect provider overflow error,
   - mark `provider_context_overflow`,
   - hard compact,
   - retry exactly once,
   - prevent infinite retry.
3. Extend validator tests for tool-call/tool-result pairing references.
4. Ensure provider raw response never appears in trace, session summary, prompt context, or memory candidates.

Suggested tests:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_assistant_context_renderer.py \
  tests/test_trace_query_api.py -q
```

## Phase 4: Memory Promotion Workflow

Status: partial.

Already done:

- `MemoryPromotionCandidate` models proposed durable writes.
- `MemoryWritePolicy.evaluate_promotion_candidate(...)` separates candidate generation from actual writes.
- Defaults are conservative:
  - `allow_session_summary_write=True`
  - `allow_long_term_promotion=False`
  - `require_user_intent_for_profile_memory=True`
  - `allow_auto_write=False`
- Explicit user memory still goes through `memory_save` / `MemoryManager.save_explicit(...)`.

Next steps:

1. Add a candidate generator boundary that can produce candidates from safe run summaries.
2. Store candidate audit metadata without writing durable memory by default.
3. Expose promotion candidate counts in API/trace only after redaction.
4. Add tests proving:
   - candidate does not imply write,
   - default policy rejects automatic promotion,
   - explicit "remember this" still writes,
   - raw/sensitive payloads are rejected,
   - session `context_summary` is never auto-promoted.

Suggested tests:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_memory_write_policy.py \
  tests/test_memory_manager.py \
  tests/test_memory_tool_boundary.py -q
```

## Phase 5: Token-Aware Budget And Overflow Recovery

Status: not started.

Goal:

- Add optional token-aware budget reporting while keeping char budget fallback.
- Use provider token usage when available.
- Harden large tool/media/file output pruning.

Steps:

1. Introduce `TokenBudgetReporter` behind a small interface.
2. Keep char-based budget as default for mock/local/offline.
3. Add provider-token metadata when real chat adapters return usage.
4. Add tool/media pruning rules:
   - large files keep summary and artifact ref only,
   - images/videos keep refs and recognition summary only,
   - command output keeps bounded lines and chars.
5. Add provider overflow retry-once behavior from Phase 3 if not already complete.

Acceptance checks:

- Existing char-budget tests still pass.
- Token reporter absence does not change default behavior.
- Oversized media/raw file payloads do not enter prompt, trace, or memory.

## Phase 6: Observability And API Polish

Status: partial.

Already done:

- Trace context includes budget, source counts, compaction summary, tool catalog, `compactor_type`, `context_summary_present`, `memory_promotion_candidates`, and `memory_promotion_written`.

Next steps:

1. Review `/runs/{run_id}` and `/traces/{trace_id}` payloads for stable public field names.
2. Add UI/Web Console display only if it improves debugging.
3. Update `docs/observability-local.md` with the final context fields.
4. Add API regression tests for any newly exposed public fields.

## Phase 7: Documentation And Boundary Cleanup

Status: in progress.

Required docs:

- `docs/CONTEXT_ENGINEERING_STATUS.md`
- `docs/memory-service-architecture.md`
- `docs/development/context-engine-memory-policy-plan.md`
- `docs/development.md`

Optional docs when API fields or workflows change:

- `docs/observability-local.md`
- `docs/CODEX_PROJECT_GUIDE.md`
- `docs/DOCS_INDEX.md`

Update rule:

- When completing a phase, change its status here.
- Add the exact validation commands used.
- Record known gaps and next phase entry point.

## Standard Validation

Focused context/memory regression:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_assistant_context_renderer.py \
  tests/test_conversation_context_compaction.py \
  tests/test_shared_assistant_run_service.py \
  tests/test_memory_write_policy.py \
  tests/test_memory_manager.py \
  tests/test_trace_query_api.py \
  tests/test_run_summary_query.py -q
```

Baseline repository checks:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
git diff --check
```

Optional wider regression:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_demo_flows.py
```

## Next Development Entry

Start with Phase 3 hardening if the next task is about compactor quality or provider overflow.

Start with Phase 4 if the next task is about memory promotion, candidate audit, profile memory, or durable write policy.

Start with Phase 5 if the next task is about token budgets, provider token usage, or large file/media/tool output pruning.
