# Memory Service Architecture

Last updated: 2026-06-29

This document is the current canonical entry for memory service architecture. Update it whenever `MemoryManager`, memory stores, retrieval, write policy, user profile behavior, memory tools, memory APIs, or memory context boundaries change.

Older Phase 8 memory documents remain background references. They are not the current design source when this file and code disagree.

Future engineering hardening should follow `docs/development/memory-kernel-hardening-plan.md` after reading this architecture document.

## Scope

The memory service is local-first long-term memory for the agent. It covers:

- Loading bounded memory context before an agent run.
- Searching user-scoped long-term memory.
- Saving explicit user-requested memories.
- Saving safe completed-run summaries where allowed.
- Maintaining a compact `user_profile` memory derived from explicit memories.
- Exposing audit, audit-event, metrics, export, retention sweep, delete, and snapshot views through service/API boundaries.

Conversation history is related but separate. Session conversation context, including session-scoped `context_summary`, is owned by conversation/session services and is combined with memory only in context packs and memory snapshots. `context_summary` is not a long-term memory item.

## Boundary With Context Engineering

Memory service produces bounded, prompt-safe memory context. Context engineering consumes that context as one input among request text, conversation history, plan state, tool observations, and tool specs.

Memory service owns:

- Memory item storage, validation, retrieval, ranking, filtering, and fallback rules.
- Memory context grouping into semantic/session/episodic/artifact/procedural layers, plus memory-local token budget selection.
- Explicit saves, duplicate merge, write policy, TTL, user profile updates, audit, snapshot, and deletion.
- Writing `AgentState.memory_context` and `request.metadata["memory_context_*"]` through `MemoryManager.load_into_state(...)`.

Context engineering owns:

- `AssistantContextPack` assembly from request, conversation, memory context, plan state, observations, and tool specs.
- Session-scoped context summary generation and prompt injection through `ContextCompactor` and `ConversationStore`.
- Prompt-json, native-tool, and final-only rendering.
- Tool observation compaction.
- Global context character budget, trimming order, source counts, and trace/debug context summaries.

Do not move memory retrieval, ranking, fallback, write policy, profile merge, or store selection into context builders or prompt renderers. Do not move prompt rendering, observation compaction, session summary, or global context budget into `MemoryManager`.

Final boundary:

- `context_summary` is session transcript state. It may be injected into the current session context, but it is not a durable memory item.
- `MemoryPromotionCandidate` is a proposed durable write. It is audit/debug metadata until `MemoryWritePolicy` approves it.
- `long_term_memory` is durable memory stored through `MemoryManager` and `MemoryStore`, after policy and `MemoryItem` validation.
- Assistant/LLM output may propose tool actions or candidates, but local policy decides compaction triggers and durable memory writes.

## Boundary With Agent Delegation

Cross-agent delegation treats memory as scoped context, not as transferable authority.

- `delegate_to_agent` and `AgentCommunicationService` must not forward parent `memory_context_text`, `memory_context_summaries`, `memory_context_refs`, `memory_context_blocks`, raw memory snapshots, or parent conversation history to child agents.
- Delegated child requests may carry explicit `context_refs`, `child_context_budget`, and `agent_context.memory_scope` metadata for audit and replay.
- `agent_context.memory_scope` records that parent memory content was not forwarded and that the child run identity comes from `AgentSessionRef`.
- If a child runtime needs memory, it must load memory through its own runtime path and `MemoryManager` using the bound user/session identity. It must not read parent memory payloads out of delegation metadata.
- Raw parent tool results should be replaced with output references or summaries before they cross the agent boundary.

## Runtime Flow

Current default flow:

```text
AgentGraphRuntime
  -> create_memory_store(ProviderConfig)
  -> MemoryManager(store)
  -> ToolExecutor(context_metadata={"memory_manager": manager})

UserRequest
  -> graph load_memory node
  -> MemoryManager.load_into_state(...)
  -> MemoryStore.search(MemoryQuery)
  -> MemoryRetrievalStrategy / KeywordMemoryRetriever
  -> AgentState.memory_context + request.metadata["memory_context_*"]
  -> AssistantContextPack / prompt renderer
  -> assistant decision or tool call
  -> memory_retrieval / memory_save tool through ToolExecutor
  -> MemoryManager.search(...) or MemoryManager.save_explicit(...)
  -> compose_response
  -> save_memory node
  -> MemoryManager.save_from_run(...) when policy allows
```

Both assistant-loop and compatibility graph start with `load_memory` and finish with `save_memory`. In assistant-loop non-mock chat paths, automatic task-summary saving is skipped because long-term memory writes should be explicit LLM tool calls through `memory_save`.

## Ownership

| module | responsibility |
| --- | --- |
| `src/multimodal_agent/memory/manager.py` | Boundary for memory retrieval, layered context formatting, explicit saves, duplicate merge, user profile upsert, run-summary saves, get/list/delete/hard-delete passthroughs. |
| `src/multimodal_agent/memory/context_builder.py` | Token-aware, prompt-safe memory context selection and layer rendering. Produces injected items, rendered context, token count, omission count, rejection reasons, and retrieval version. |
| `src/multimodal_agent/memory/store.py` | `MemoryStore` protocol and process-local `InMemoryStore`, including soft-delete-compatible delete and hard-delete store boundary methods. |
| `src/multimodal_agent/memory/jsonl_store.py` | Local JSONL persistent store implementing the same store contract. |
| `src/multimodal_agent/memory/sqlite_store.py` | Local SQLite persistent store implementing the same store contract with schema version, indexes, upsert, and soft-delete-compatible delete behavior. |
| `src/multimodal_agent/memory/retrieval.py` | Query filtering, relevance gating, type/capability priority, recency fallback rules, context formatting. |
| `src/multimodal_agent/memory/retriever.py` | Deterministic keyword and Chinese phrase-fragment retrieval. |
| `src/multimodal_agent/memory/write_policy.py` | Safe memory item construction, TTL defaults, raw payload restrictions, explicit memory typing. |
| `src/multimodal_agent/memory/profile.py` | Compact `user_profile` memory derived from explicit preference/product/task memories. |
| `src/multimodal_agent/schemas/identity.py` | `RequestIdentity` contract for request/auth-derived user, tenant, project, session, and allowed memory scopes. |
| `src/multimodal_agent/tools/memory_tool.py` | Agent-callable `memory`, `memory_retrieval`, and `memory_save` tools. Uses `MemoryManager` from tool context when present. |
| `src/multimodal_agent/services/memory_audit.py` | User-scoped list/get/export/retention-sweep/delete/audit/event/metrics service over `MemoryManager`. |
| `src/multimodal_agent/services/memory_snapshot.py` | Read-only snapshot combining memory context, session records, conversation history, audit, and storage boundary info. |
| `src/multimodal_agent/schemas/memory.py` | Public memory contracts and payload safety validation. |
| `src/multimodal_agent/schemas/memory_audit.py` | API-facing audit/export/retention/delete/list/event/metrics models. |
| `src/multimodal_agent/schemas/memory_snapshot.py` | API-facing memory boundary snapshot models. |

Agent nodes, assistant loops, API routes, MCP routes, and tools should not depend directly on concrete stores. Use `MemoryManager` or a service that wraps it.

## Identity Boundary

Memory access is scoped by `RequestIdentity` at the service boundary:

```text
RequestIdentity
  -> tenant_id
  -> user_id
  -> project_id
  -> session_id
  -> allowed_scopes
```

Current P0 behavior:

- Local/mock paths derive identity from `UserRequest`, `ToolContext`, API path/query parameters, or inbound A2A metadata.
- API routes resolve request-derived identity through `services/api_identity.py` before trial-access checks and memory service calls. This centralizes provenance (`request_body`, `path`, `query`, `a2a_metadata`, `websocket_query`) without treating it as production authentication. `IdentityPolicy` can classify the resolved identity as auth-bound, request-derived warning, local bypass warning, or production-blocking failure.
- `MemoryManager` exposes identity-aware methods such as `search_for_identity(...)`, `load_context_for_identity(...)`, `save_explicit_for_identity(...)`, `get_for_identity(...)`, `list_for_identity(...)`, and identity-scoped delete helpers.
- `MemoryAuditService` and `MemorySnapshotService` expose identity-aware methods and keep the legacy `user_id` methods as compatibility wrappers.
- Memory tools bind identity from `ToolContext` before invoking `MemoryManager`, so model-supplied `user_id` cannot override runtime context.
- `MemoryItem` and `MemoryQuery` carry optional `tenant_id`, `project_id`, and coarse `scope` fields. If a memory item has tenant/project/scope metadata, retrieval, list, get, and delete paths must filter it against `RequestIdentity`.
- Unscoped legacy user-level memories remain visible to the same `user_id` for compatibility. Project-scoped memories require a matching `project_id`; tenant-scoped memories require a matching `tenant_id`.
- Duplicate merge compares tenant, project, and effective scope before merging. Project/tenant-scoped memories do not update the current global `user_profile`; project/tenant-specific profile handling needs a separate schema/index design.

Current limits:

- API routes still use request-derived identity unless a future auth context is wired into `services/api_identity.py`. They are shaped for auth-bound identity and policy evaluation, but they are not yet using a real authentication principal.
- Store schemas still index primarily by `user_id`; tenant/project/scope filtering is enforced in memory service/retrieval code rather than database-level indexes.

## Service Core Vs Tool Adapter

Memory is a service capability. `memory_retrieval` and `memory_save` are Agent-callable adapters for that capability, not the owner of memory behavior.

Use this routing matrix:

| caller | allowed path |
| --- | --- |
| Graph/runtime automatic memory load/save | `graph node -> MemoryManager -> MemoryStore` |
| Assistant/LLM explicit memory action | `AssistantDecision -> ActionValidator -> ToolExecutor -> memory tool -> MemoryManager` |
| API audit/export/retention/snapshot/delete | `API route -> MemoryAuditService / MemorySnapshotService -> MemoryManager` |
| Unit tests for storage/retrieval/policy | direct `MemoryManager` or store instantiation is allowed only inside focused tests |

`src/multimodal_agent/tools/memory_tool.py` must stay a thin tool adapter. It may:

- Bind `ToolContext.user_id` and `ToolContext.session_id`.
- Validate tool-facing required input.
- Convert tool input into `MemoryQuery` or `MemoryManager.save_explicit(...)`.
- Wrap manager output as `ToolResult` and capability output contracts.
- Keep small legacy/mock compatibility paths only when required by existing tests or demos.

It must not own or reimplement:

- Retrieval/ranking/fallback strategy.
- Write policy, TTL, duplicate merge, sensitivity, or raw payload filtering.
- User profile extraction or merge behavior.
- Storage backend selection or direct `MemoryStore` access.
- API audit, export, retention sweep, snapshot, deletion, or user-data lifecycle behavior.

If memory logic grows beyond identity binding, input adaptation, or result wrapping, move it into `MemoryManager`, `memory/` helpers, or a `services/memory_*` service before exposing it through a tool.

## Storage

Configured by `ProviderConfig`:

- `memory_backend="memory"`: default process-local `InMemoryStore`.
- `memory_backend="jsonl"`: local `JsonlMemoryStore`.
- `memory_backend="sqlite"`: local `SQLiteMemoryStore`.
- `memory_path`: default `.local/memory/long_term_memories.jsonl`; when `MULTIMODAL_AGENT_MEMORY_BACKEND=sqlite` is set without an explicit path, the default is `.local/memory/long_term_memories.sqlite3`.

Environment variables:

- `MULTIMODAL_AGENT_MEMORY_BACKEND`
- `MULTIMODAL_AGENT_MEMORY_PATH`

Relative JSONL and SQLite paths resolve from the repository root. JSONL and SQLite are still local-first storage, not real external providers. Future PostgreSQL, vector DB, or external memory service adapters must sit behind `MemoryStore` and `MemoryManager`.

## Contracts

Core models:

- `MemoryItem`: one retrievable memory item with `user_id`, optional `session_id`, `memory_type`, safe `content`, `summary`, tags, artifact refs, timestamps, TTL, relevance, reason, and sensitivity.
- `MemoryQuery`: user-scoped query options, including `session_id`, text query, capability, memory types, tags, `top_k`, `max_context_chars`, `since`, and `include_expired`.
- `MemorySearchResult`: structured search output with items, query used, total, ranking reason, context text, and errors.

`MemoryItem` validation rejects unsafe payload keys such as API keys, tokens, raw media, base64 payloads, and raw provider responses. It also rejects inline media data URIs and sanitizes summaries, reasons, tags, and string content.

Do not store raw user text by default, raw provider responses, real media bodies, secrets, bearer tokens, API keys, or unredacted external service payloads. Store references and safe summaries instead.

## Layers

`MemoryManager.build_context()` groups memory items into prompt-safe layers:

`MemoryItem.memory_type` is the storage/business category. `MemoryContextBlock.layer` is a derived prompt/context grouping. Keep the internal layer constants stable, and use the display title to make the group readable in prompts, API snapshots, and Web UI.

| memory type | internal layer | display title |
| --- | --- | --- |
| `preference` | `semantic` | 偏好/事实记忆 |
| `conversation` | `session` | 长期化对话 |
| `task` | `episodic` | 任务/经历记忆 |
| `product`, `artifact`, `image`, `video`, `generation`, `render` | `artifact` | 产物/对象引用 |
| future procedural memory | `procedural` | 过程/规则记忆 |

Do not rename `semantic`, `session`, `episodic`, `artifact`, or `procedural` just to improve UI wording. Those constants are internal context layers and may appear in metadata, API snapshots, and tests. Change display titles when the wording needs to be clearer.

The rendered memory context is written to:

- `AgentState.memory_context`
- `request.metadata["memory_context_text"]`
- `request.metadata["memory_context_summaries"]`
- `request.metadata["memory_context_refs"]`
- `request.metadata["memory_context_blocks"]`
- `request.metadata["memory_context_tokens"]`
- `request.metadata["memory_context_budget_tokens"]`
- `request.metadata["memory_context_omitted_count"]`
- `request.metadata["memory_context_rejected_reasons"]`
- `request.metadata["memory_context_retrieval_version"]`
- `request.metadata["memory_context_injected_ids"]`

Prompt rendering must treat memory as user-history data, not as system instruction.

## Context Injection Budget

`MemoryContextBuilder` selects the actual memory items injected into the model context. Retrieval may return more items than the rendered prompt can carry; `MemoryContext.items` and `AgentState.memory_context` represent the injected subset.

Current behavior:

- Character budget still applies through `MemoryQuery.max_context_chars` and `MemoryManager.default_max_context_chars`.
- Optional token budget applies through `MemoryManager(..., default_max_context_tokens=...)`, `load_context_for_request(..., max_context_tokens=...)`, `load_context_for_identity(..., max_context_tokens=...)`, or request metadata `memory_context_max_tokens` / `memory_context_budget_tokens`.
- Token estimates are deterministic and local; no tokenizer dependency and no provider call are introduced.
- `sensitive` memory items are not injected.
- Expired memory items are not injected.
- Omitted and rejected items are reported through `memory_context_omitted_count` and `memory_context_rejected_reasons`.

## Retrieval

Retrieval is deterministic and local:

- Non-empty query uses `KeywordMemoryRetriever`.
- Chinese query segments are expanded into short phrase fragments for local recall.
- A concrete entity/topic miss returns no memories.
- Recent-memory fallback is allowed only for explicit contextual follow-ups such as "继续", "上次", "刚才", "之前", "这个", "那个", "同款", or similar markers.
- Empty query lists recent user-scoped memory and is mainly for browsing, audit, debug, and snapshots.

Filters apply after candidate selection:

- `user_id` isolation is mandatory.
- Optional `session_id`, `memory_types`, `tags`, `since`, and expiration filters apply.
- Expired memories are excluded unless `include_expired=True`.

Ranking combines relevance, capability/type priority, artifact-ref signal, and recency. Capability-specific priorities currently exist for image generation, product search, render 3D, and direct chat.

## Retrieval Eval

Memory retrieval quality is measured before adding embedding or vector dependencies.

Current local eval boundary:

- `src/multimodal_agent/memory/retrieval_eval.py` runs deterministic `InMemoryStore + MemoryManager` retrieval and context-injection cases.
- `scripts/run_evals.py --suite memory` includes the retrieval eval cases from `tests/evals/eval_cases.json`.
- Metrics include Recall@k, MRR, false-positive rate, correct-empty rate, cross-user leakage rate, sensitive/expired injection rate, and token budget compliance.
- Initial coverage includes black-bag recall, color preference recall, task/product resume, budget preference, unrelated empty recall, cross-user isolation, expired exclusion, sensitive non-injection, and token budget compliance.

## Writes

Explicit saves:

- Flow through `memory_save` or `MemoryManager.save_explicit(...)`.
- Are evaluated by `MemoryWritePolicy.evaluate_explicit_save(...)` before any item is built.
- Return a `MemoryWriteDecision` with `allowed`, `destination`, `reason`, `require_user_confirmation`, `sensitivity`, `ttl_days`, and `redacted_payload`.
- Require non-empty text or summary.
- Infer memory type from explicit text/content. Stable preferences become `preference`; product-like content becomes `product`; otherwise default is `task`.
- Do not save raw user text unless `MemoryWritePolicy.auto_save_raw_user_text=True`.
- Reject API keys, tokens, bearer credentials, raw provider payloads, and base64/raw media even when the user explicitly asks to remember them.
- Merge duplicates by normalized summary and memory type.
- Update compact `user_profile` for explicit `preference`, `product`, and `task` items.

Run-summary saves:

- Flow through `MemoryManager.save_from_run(...)`.
- Only consider completed runs with a response.
- Skip pure memory-save runs.
- First produce a `MemoryPromotionCandidate` from safe task summaries and output refs, not raw request/provider payloads.
- Respect `MemoryWritePolicy.auto_save_task_summary` for candidate generation.
- Evaluate the candidate through `MemoryWritePolicy.evaluate_promotion_candidate(...)` before any durable write.
- Default policy records candidate audit metadata and rejects the automatic write because `allow_auto_write=False`.
- Write a durable task memory only when policy explicitly allows automatic writes.
- Are skipped in assistant-loop non-mock chat paths so the model can choose `memory_save` explicitly.

Promotion candidates:

- `MemoryPromotionCandidate` is a proposed durable memory, not a write.
- `MemoryWritePolicy.evaluate_promotion_candidate(...)` returns the same `MemoryWriteDecision` shape used for explicit saves.
- Defaults are conservative: `allow_auto_write=False`, `allow_long_term_promotion=False`, `require_user_intent_for_profile_memory=True`.
- Candidate audit metadata is stored on request metadata and trace summaries as counts plus decision fields and redacted candidate summaries; candidate `content` is not exposed in trace/API summaries.
- User-explicit "remember this" intent remains allowed through `memory_save` / `MemoryManager.save_explicit(...)`.
- Temporary debug notes, one-off searches, failed attempts, speculation, raw outputs, provider payloads, base64/media bodies, API keys, tokens, and other secret-like content are rejected or left as non-written candidates.
- Session `context_summary` is handled by `ConversationStore.save_summary(...)`; it must not be promoted to long-term memory by automatic candidate generation.

Default TTL policy:

| memory type | TTL |
| --- | --- |
| `preference` | no default expiration |
| `conversation` | 30 days |
| `task`, `artifact`, `product`, `image`, `video`, `generation`, `render` | 90 days |

Sensitive-looking summaries are sanitized. If task-summary sanitization changes the summary and `require_explicit_save_for_sensitive=True`, automatic task-summary saving is skipped.

## User Profile

The compact user profile is stored as a normal memory item:

```text
memory_id = user_profile
memory_type = preference
source = user_profile
```

Its content stores:

- `profile_version`
- `preferences`
- `facts`
- `source_memory_ids`

This keeps profile retrieval compatible with the existing store/search/delete contract while avoiding a separate profile storage path.

## API And Audit

Memory API routes use `MemoryAuditService` and `MemorySnapshotService` over the runtime `MemoryManager`:

- `GET /memory/users/{user_id}/items`
- `GET /memory/users/{user_id}/items/{memory_id}`
- `GET /memory/users/{user_id}/audit`
- `GET /memory/users/{user_id}/events`
- `GET /memory/users/{user_id}/metrics`
- `GET /memory/users/{user_id}/export`
- `GET /memory/users/{user_id}/snapshot`
- `POST /memory/users/{user_id}/retention/sweep`
- `DELETE /memory/users/{user_id}/items/{memory_id}`
- `DELETE /memory/users/{user_id}/sessions/{session_id}`

List and snapshot endpoints do not include memory `content` by default. `include_content=True` returns sanitized content only. Export is identity-scoped and can omit content with `include_content=false`. Retention sweep scans only identity-visible memories, supports `dry_run=true`, soft-deletes expired items by default, and uses `MemoryManager.hard_delete_for_identity(...)` plus `MemoryStore.hard_delete(...)` when `hard_delete=true`. SQLite removes the row on hard delete; in-memory and JSONL stores already delete physically. Deletion is user-scoped and must not cross users even when memory IDs or session IDs match.

`MemoryManager` keeps a bounded, in-process `MemoryAuditEvent` list for prompt-safe lifecycle events: context load, explicit save/reject, promotion decision, soft delete, hard delete, session delete, and user clear. `MemoryAuditService` adds export and retention-sweep events, and derives `MemoryMetricsReport` counters from the same event stream. This foundation is useful for local API/debug visibility, but it is not a durable audit log; production-grade audit storage, rollback/rebuild, and long-term metrics export remain future work. Event metadata must stay redacted and must not include raw memory content, raw tool/provider payloads, base64/media bodies, or secrets.

`DELETE /beta/users/{user_id}/data` clears memory through `runtime.memory_manager.clear_user(user_id)` as part of broader user-data deletion.

## Design Rules

- Read this document before designing or changing memory service behavior.
- Keep Agent/API/MCP code behind `MemoryManager`, `MemoryAuditService`, `MemorySnapshotService`, `ToolExecutor`, or memory tools.
- Do not let assistant nodes or API routes directly instantiate or query concrete stores.
- Keep memory tools thin. Tool code may adapt tool input/output, but service behavior belongs in `MemoryManager`, `memory/`, or `services/memory_*`.
- Do not bypass `MemoryWritePolicy` or `MemoryItem` validation when writing memory.
- Do not add embedding/vector/external memory by changing prompt builders or agent nodes directly; add an adapter behind `MemoryStore`/retrieval and keep deterministic local behavior for tests.
- Keep default behavior mock/local/offline. A memory backend must not become a network provider merely because credentials exist.
- When memory context rendering, conversation context, context budget, or prompt injection handling changes, also read `docs/CONTEXT_ENGINEERING_STATUS.md`.
- Update this file, `docs/DOCS_INDEX.md`, and any affected tests when the architecture changes.

## Validation

Focused validation for memory changes:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_manager.py tests/test_memory_retrieval_strategy.py tests/test_memory_store_boundary.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_write_policy.py tests/test_memory_privacy_redaction.py tests/test_memory_lifecycle.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_audit_api.py tests/test_memory_snapshot_api.py tests/test_memory_runtime_integration.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_tool_boundary.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_retrieval_eval.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py --suite memory
```

For broad behavior changes, run the full offline suite:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_demo_flows.py
```
