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

- `README.md`
- `AGENTS.md`
- `docs/CONTEXT_ENGINEERING_STATUS.md`
- `docs/memory-service-architecture.md`

If the task touches architecture ownership, use the boundary rules in `AGENTS.md`.

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
- Session summary rolls forward with the recent-turn window: only newly aged-out turns are merged into `context_summary`.
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
- Sliding-window updates do not summarize the same turn twice.
- `reset_conversation=True` starts without old turns or summary.
- Session summary does not create a `MemoryItem`.

Regression tests:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_shared_assistant_run_service.py \
  tests/test_memory_runtime_integration.py \
  tests/test_memory_snapshot_api.py -q
```

Follow-up hardening on 2026-06-29:

- `DeterministicContextCompactor` skips turns whose `run:` or `trace:` refs already exist in the current session summary.
- `run_assistant_request` uses `ContextPolicy.keep_recent_turns` for recent-window selection instead of a local hard-coded value.
- This remains deterministic sliding-window compaction; no scene classifier, quality feedback loop, component registry, or undo log was introduced.

## Phase 3: LLM Compactor Hardening

Status: done.

Done:

- `LLMCompactor` calls the configured `ChatAdapter`.
- It is selected only under `provider_smoke` or `pilot` when chat adapter is not mock.
- `SummaryValidator` checks schema and rejects raw/secret-like payloads.
- Invalid output falls back to deterministic compactor.
- Trace records `compactor_type`.
- A fake real adapter test exercises the LLM compactor path without network.
- Provider context overflow is normalized to `provider_context_overflow`, marks metadata, forces hard compaction, retries once, and then stops.
- `SummaryValidator` rejects summaries that keep only one side of a tool-call/tool-result reference pair.
- LLM compactor prompts omit raw provider payload fields before any provider call.

Implemented files:

- `src/multimodal_agent/agent/assistant_loop_nodes.py`
- `src/multimodal_agent/services/chat_adapter.py`
- `src/multimodal_agent/services/context/compactor.py`
- `src/multimodal_agent/services/provider_errors.py`

Regression tests:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_assistant_context_renderer.py \
  tests/test_phase8a1_react_action_quality.py \
  tests/test_trace_query_api.py -q
```

Validation on 2026-06-29:

- Focused Phase 3 regression above: passed.
- Context/memory regression set: passed.
- `scripts/check_env.py`: passed.
- `git diff --check`: passed.

Remaining risks:

- Budgeting is still character-based; token-aware reporting remains Phase 5 work.
- Real provider smoke remains opt-in only and was not run during offline regression.

## Phase 4: Memory Promotion Workflow

Status: done.

Done:

- `MemoryPromotionCandidate` models proposed durable writes.
- `MemoryWritePolicy.evaluate_promotion_candidate(...)` separates candidate generation from actual writes.
- Defaults are conservative:
  - `allow_session_summary_write=True`
  - `allow_long_term_promotion=False`
  - `require_user_intent_for_profile_memory=True`
  - `allow_auto_write=False`
- Explicit user memory still goes through `memory_save` / `MemoryManager.save_explicit(...)`.
- `build_run_summary_promotion_candidate(...)` produces safe completed-run candidates without raw request/provider payloads.
- `MemoryManager.save_from_run(...)` records candidate audit metadata and rejects automatic writes by default.
- `MemoryManager.save_from_run(...)` writes only when policy explicitly allows automatic writes.
- Trace/run summaries merge redacted promotion counts from the final save-memory stage.
- Session `context_summary` candidates are rejected and remain session-scoped.
- Raw provider payload aliases such as `raw_provider_payload`, `raw_payload`, and `raw_html` are rejected.

Implemented files:

- `src/multimodal_agent/memory/write_policy.py`
- `src/multimodal_agent/memory/manager.py`
- `src/multimodal_agent/schemas/memory.py`
- `src/multimodal_agent/services/trace_store.py`
- `src/multimodal_agent/services/trace_query.py`

Regression tests:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_memory_write_policy.py \
  tests/test_memory_manager.py \
  tests/test_memory_tool_boundary.py -q
```

Validation on 2026-06-29:

- Focused Phase 4 regression above plus trace query/run summary tests: passed.
- Memory backend integration with opt-in auto promotion: passed.
- Standard context/memory regression set: passed.

Remaining risks:

- Candidate audit is metadata/trace only; there is no separate durable candidate review queue yet.
- Real LLM-generated memory candidates remain future work and must stay policy-gated.

## Phase 5: Token-Aware Budget And Pruning

Status: done.

Goal:

- Add optional token-aware budget reporting while keeping char budget as the default control path.
- Use provider token usage/estimates when available, without changing mock/local/offline behavior.
- Harden large tool/media/file output pruning as a separate content policy step.

### Phase 5a: Token-Aware Budget Reporter

Status: done.

Scope:

- Introduce `TokenBudgetReporter` behind a small context-service interface.
- Keep char-based budget as the source of truth for compaction triggers.
- Add optional token fields to budget reports when usage metadata or estimates exist.
- Expose token fields through existing trace/API context summaries.
- Do not add new dependencies or provider calls.
- Do not change tool/media/file pruning behavior in this subphase.

Implemented files:

- `src/multimodal_agent/schemas/context.py`
- `src/multimodal_agent/services/context/token_budget.py`
- `src/multimodal_agent/services/context/builder.py`

Done:

- Added optional token fields to `ContextBudgetReport`.
- Added `TokenBudgetReporter` with deterministic local estimates.
- Enabled estimates only via metadata such as `context_budget_estimate_tokens=True` or `context_budget_max_tokens`.
- Preferred provider usage metadata from `context_token_usage`, `provider_token_usage`, or `last_chat_usage` when present.
- Exposed token fields through existing trace/API context summaries.

Acceptance checks:

- Existing char-budget tests still pass.
- Token reporter absence does not change default behavior.
- Token estimates are visible in `ContextBudgetReport` and trace context when enabled by metadata.
- Provider token usage metadata is preferred over estimates when present.

Suggested tests:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_assistant_context_renderer.py \
  tests/test_trace_query_api.py -q
```

Validation on 2026-06-29:

- Focused Phase 5a regression above: passed.
- Standard context/memory regression set: passed.
- `scripts/check_env.py`: passed.
- `git diff --check`: passed.

### Phase 5b: Provider Token Usage And Overflow Metadata

Status: done.

Scope:

- Normalize provider token usage from real `ChatResult`/adapter metadata when available.
- Carry provider token usage into the next context budget report.
- Keep provider overflow retry-once behavior covered by Phase 3 regression tests.

Implemented files:

- `src/multimodal_agent/agent/assistant_loop_nodes.py`
- `src/multimodal_agent/services/context/token_budget.py`

Done:

- Added safe provider usage normalization that keeps only token counters.
- Recorded `ChatResult.usage` into request metadata after assistant chat calls, repair calls, final-only calls, and overflow retries.
- Carried provider token usage into the next assistant context budget report through existing token budget metadata.
- Kept overflow retry bounded to one retry.

Acceptance checks:

- Provider usage metadata does not leak raw provider response.
- Overflow retry remains bounded to one retry.
- Missing token usage falls back to Phase 5a estimates.

Validation on 2026-06-29:

- Focused provider token usage and overflow regression: passed.
- Full assistant-loop behavior regression: passed.
- Standard context/memory regression set: passed.
- `scripts/check_env.py`: passed.
- `git diff --check`: passed.

### Phase 5c: Tool/Media/File Pruning Policy

Status: done.

Scope:

- Add tool/media pruning rules:
  - large files keep summary and artifact ref only,
  - images/videos keep refs and recognition summary only,
  - command output keeps bounded lines and chars.
- Ensure raw file/media/provider payloads do not enter prompt, trace, or memory.

Implemented files:

- `src/multimodal_agent/services/context/compaction.py`
- `src/multimodal_agent/agent/assistant_loop_nodes.py`

Done:

- Added explicit raw provider/file/media payload key pruning for assistant-facing observation copies.
- Added inline media data URI pruning so base64 image/video/audio payloads do not enter rendered prompts.
- Added bounded command output rendering for stdout/stderr/log-style fields.
- Preserved prompt-safe refs and recognition summaries such as `output_ref`, `artifact_ref`, `image_ref`, `recognized_text`, and transcripts.
- Extended observation compaction metadata with pruned key names and command-output truncation limits.
- Extended trace compaction summaries with pruning/truncation counts only, not raw payloads.

Acceptance checks:

- Oversized media/raw file payloads do not enter prompt, trace, or memory.
- Existing observation compaction tests continue to pass.

Validation on 2026-06-29:

- Focused Phase 5c renderer/trace regression: passed.
- Standard context/memory/trace regression set: passed.
- Assistant-loop trace summary regression: passed.
- `scripts/check_env.py`: passed.
- `git diff --check`: passed.

## Phase 6: Observability And API Polish

Status: done.

Already done:

- Trace context includes budget, source counts, compaction summary, tool catalog, `compactor_type`, `context_summary_present`, `memory_promotion_candidates`, and `memory_promotion_written`.
- Trace context includes `context_schema_version="context_observability_v1"` for stable public field versioning.
- Run/trace query summaries merge the latest assistant context summary with redacted completed-run memory promotion counts.
- Trace sanitization drops raw provider/file/media payload keys such as `raw_provider_payload` and `image_base64` before public API summaries.
- `docs/observability-local.md` documents the public context field set and safety boundaries.

Decisions:

- Web Console display was not changed in this phase; the existing run/trace API payload is sufficient for local debugging.
- Context summaries remain debug summaries, not rendered prompts or durable memory records.

Validation on 2026-06-29:

- Focused observability/API regression: passed.
- Provider safety redaction regression: passed.
- Standard context/memory/observability regression set: passed.
- `scripts/check_env.py`: passed.
- `git diff --check`: passed.

## Phase 7: Documentation And Boundary Cleanup

Status: done.

Required docs:

- `docs/CONTEXT_ENGINEERING_STATUS.md`
- `docs/memory-service-architecture.md`
- `docs/development/context-engine-memory-policy-plan.md`

Optional docs when API fields or workflows change:

- `README.md`
- `AGENTS.md`

Update rule:

- When completing a phase, change its status here.
- Add the exact validation commands used.
- Record known gaps and next phase entry point.

Done:

- Marked this staged implementation plan as complete and clarified that it is now a reference log, not the active architecture source.
- Added `docs/CONTEXT_ENGINEERING_STATUS.md` to the current entry docs as the canonical context-engineering entry.
- Clarified final ownership boundaries:
  - Context Engine owns assembly, budget, prune, compact, session summary, and trace/debug context summaries.
  - Memory Service owns durable memory, retrieval, write policy, audit, delete, profile memory, and store selection.
  - Assistant/LLM may propose actions or candidates, but local policy decides compaction triggers and durable writes.
- Documented `context_summary`, `MemoryPromotionCandidate`, and long-term memory as separate lifecycle states.

Validation on 2026-06-29:

- `scripts/check_env.py`: passed.
- `git diff --check`: passed.

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

This staged plan is complete.

For future context work, start with `docs/CONTEXT_ENGINEERING_STATUS.md`.

For future memory work, start with `docs/memory-service-architecture.md`.

For future trace/API observability work, start with `docs/observability-local.md`.
