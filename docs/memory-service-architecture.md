# Memory Service Architecture

Last updated: 2026-06-30

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
       -> allow: MemoryStore.save(...)
       -> needs confirmation: MemoryPendingConfirmation
       -> reject: audit-only rejection
  -> optional API user confirmation/rejection for pending explicit memory
  -> compose_response
  -> save_memory node
  -> MemoryManager.save_from_run(...) when policy allows
```

Both assistant-loop and compatibility graph start with `load_memory` and finish with `save_memory`. In assistant-loop non-mock chat paths, automatic task-summary saving is skipped because long-term memory writes should be explicit LLM tool calls through `memory_save`.

## Ownership

| module | responsibility |
| --- | --- |
| `src/assistant_agent/memory/manager.py` | Boundary for memory retrieval, layered context formatting, explicit saves, pending confirmation flow, duplicate merge, user profile upsert, run-summary saves, get/list/delete/hard-delete passthroughs. |
| `src/assistant_agent/memory/context_builder.py` | Token-aware, prompt-safe memory context selection and layer rendering. Produces injected items, rendered context, token count, omission count, rejection reasons, and retrieval version. |
| `src/assistant_agent/memory/store.py` | `MemoryStore` protocol and process-local `InMemoryStore`, including soft-delete-compatible delete, hard-delete, and memory-confirmation methods. |
| `src/assistant_agent/memory/jsonl_store.py` | Local JSONL persistent store implementing the same store contract, with redacted confirmation state stored in a sidecar JSONL file. |
| `src/assistant_agent/memory/sqlite_store.py` | Local SQLite persistent store implementing the same store contract with schema version, indexes, upsert, soft-delete-compatible delete behavior, durable audit-event rows, and durable confirmation rows. |
| `src/assistant_agent/memory/remote.py` | Opt-in external Memory Server adapter. Converts remote query responses into safe `MemoryItem` / `MemorySearchResult` objects, provides query/health plus media upload/task-status client methods, and exposes `HybridMemoryStore` where only `search(...)` uses the remote service while writes, delete, confirmation, profile, audit, and lifecycle operations remain local. |
| `src/assistant_agent/memory/retrieval.py` | Query filtering, relevance gating, type/capability priority, recency fallback rules, context formatting. |
| `src/assistant_agent/memory/retriever.py` | Deterministic keyword and Chinese phrase-fragment retrieval. |
| `src/assistant_agent/memory/write_policy.py` | Safe memory item construction, TTL defaults, raw payload restrictions, explicit memory typing. |
| `src/assistant_agent/memory/profile.py` | Compact `user_profile` memory derived from explicit preference/product/task memories. |
| `src/assistant_agent/schemas/identity.py` | `RequestIdentity` contract for request/auth-derived user, tenant, project, session, and allowed memory scopes. |
| `src/assistant_agent/tools/memory_tool.py` | Agent-callable `memory`, `memory_retrieval`, and `memory_save` tools. Uses `MemoryManager` from tool context when present. |
| `src/assistant_agent/services/memory_media_ingestion.py` | Governed service boundary for Memory Server media ingestion. Binds trusted `RequestIdentity`, generates globally unique upload `file_id` values, calls `RemoteMemoryClient.upload_media(...)` / `task_status(...)`, and returns structured prompt-safe results. |
| `src/assistant_agent/tools/memory_media_tool.py` | Agent-callable `memory_media_ingest` and `memory_ingest_status` tool adapters. They bind runtime identity from `ToolContext`, call `MemoryMediaIngestionService`, and wrap structured `ToolResult` / capability contracts. They do not implement `memory_save`. |
| `src/assistant_agent/services/memory_audit.py` | User-scoped list/get/export/retention-sweep/delete/audit/event/metrics/confirmation service over `MemoryManager`. |
| `src/assistant_agent/services/memory_snapshot.py` | Read-only snapshot combining memory context, session records, conversation history, audit, and storage boundary info. |
| `src/assistant_agent/schemas/memory.py` | Public memory contracts and payload safety validation. |
| `src/assistant_agent/schemas/memory_audit.py` | API-facing audit/export/retention/delete/list/event/metrics/confirmation models. |
| `src/assistant_agent/schemas/memory_snapshot.py` | API-facing memory boundary snapshot models. |

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
- API routes resolve request-derived identity through `services/api_identity.py` before trial-access checks and memory service calls. `api/auth.py` provides the default FastAPI `AuthContext` dependency, which returns anonymous/no-auth context and ignores auth-like headers unless the explicit `MULTIMODAL_AGENT_AUTH_HEADER_ENABLED` pilot flag is enabled. In that pilot mode, only controlled `X-Multimodal-Agent-*` headers are converted into `AuthContext`; body/path/query user mismatch is rejected by the identity resolver. This centralizes provenance (`request_body`, `path`, `query`, `a2a_metadata`, `websocket_query`, `auth_context`) without treating it as production JWT/session authentication. `IdentityPolicy` can classify the resolved identity as auth-bound, request-derived warning, local bypass warning, or production-blocking failure.
- `MemoryManager` exposes identity-aware methods such as `search_for_identity(...)`, `load_context_for_identity(...)`, `save_explicit_for_identity(...)`, `get_for_identity(...)`, `list_for_identity(...)`, and identity-scoped delete helpers.
- `MemoryAuditService` and `MemorySnapshotService` expose identity-aware methods and keep the legacy `user_id` methods as compatibility wrappers.
- Memory tools bind identity from `ToolContext` before invoking `MemoryManager`, so model-supplied `user_id` cannot override runtime context.
- `MemoryItem` and `MemoryQuery` carry optional `tenant_id`, `project_id`, and coarse `scope` fields. If a memory item has tenant/project/scope metadata, retrieval, list, get, and delete paths must filter it against `RequestIdentity`.
- Unscoped legacy user-level memories remain visible to the same `user_id` for compatibility. Project-scoped memories require a matching `project_id`; tenant-scoped memories require a matching `tenant_id`.
- Duplicate merge compares tenant, project, and effective scope before merging. Project/tenant-scoped memories do not update the current global `user_profile`; project/tenant-specific profile handling needs a separate schema/index design.

Current limits:

- API routes still use request-derived identity unless the `api/auth.py` dependency returns a trusted auth context. They are shaped for auth-bound identity and policy evaluation, but they are not yet using a real authentication principal.
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

`src/assistant_agent/tools/memory_tool.py` must stay a thin tool adapter. It may:

- Bind `ToolContext.user_id` and `ToolContext.session_id`.
- Validate tool-facing required input.
- Convert tool input into `MemoryQuery` or `MemoryManager.save_explicit(...)`.
- Wrap manager output as `ToolResult` and capability output contracts.
- Surface `MemoryConfirmationRequired` as a recoverable `memory_save` partial result with a `confirmation_id`; it must not report pending confirmation as a completed save.
- Keep small legacy/mock compatibility paths only when required by existing tests or demos.

It must not own or reimplement:

- Retrieval/ranking/fallback strategy.
- Write policy, TTL, duplicate merge, sensitivity, or raw payload filtering.
- User profile extraction or merge behavior.
- Storage backend selection or direct `MemoryStore` access.
- API audit, export, retention sweep, snapshot, deletion, or user-data lifecycle behavior.

If memory logic grows beyond identity binding, input adaptation, or result wrapping, move it into `MemoryManager`, `memory/` helpers, or a `services/memory_*` service before exposing it through a tool.

## Memory Tool Selection Strategy

Assistant-loop memory tool selection follows an LLM-first strategy:

- In the assistant loop, the LLM is the decision maker for calling `memory_save` or `memory_retrieval` (also called memory search in higher-level discussions).
- `memory_save` calls must declare `source_intent`, `source_reason`, `future_use`, and `evidence`.
- `source_intent=user_explicit` means the LLM judged that the user explicitly asked to remember or save the content. If `MemoryWritePolicy` allows it, this path may write a durable `MemoryItem`.
- `source_intent=assistant_candidate` means the LLM inferred a potentially useful future memory. If `MemoryWritePolicy` allows it, this path records candidate/audit output by default and does not write a durable `MemoryItem`.
- `source_intent=user_confirmed` is reserved for confirmation service internals. LLM/tool calls using it are rejected before durable write.
- Keyword and vector matching are not used in the current source-intent decision path. They may remain future extension points, but they must not override the LLM-declared source intent unless this architecture document and tests are updated.
- Regardless of who triggers `memory_save`, durable writes still go through `ToolExecutor -> memory_save -> MemoryManager -> MemoryWritePolicy -> MemoryItem` validation before storage.
- Audit/candidate records are not long-term memory. Automatic candidates do not directly write long-term memory by default.

## Storage

Configured by `ProviderConfig`:

- `memory_backend="memory"`: default process-local `InMemoryStore`.
- `memory_backend="jsonl"`: local `JsonlMemoryStore`.
- `memory_backend="sqlite"`: local `SQLiteMemoryStore`.
- `memory_backend="hybrid_remote"`: opt-in `HybridMemoryStore` with local JSONL lifecycle storage plus external Memory Server query augmentation. This backend is selected from environment only when `MULTIMODAL_AGENT_MEMORY_REMOTE_ENABLED=true` or a runtime profile that allows real/network providers is active.
- `memory_path`: default `.local/memory/long_term_memories.jsonl`; when `MULTIMODAL_AGENT_MEMORY_BACKEND=sqlite` is set without an explicit path, the default is `.local/memory/long_term_memories.sqlite3`.

Environment variables:

- `MULTIMODAL_AGENT_MEMORY_BACKEND`
- `MULTIMODAL_AGENT_MEMORY_PATH`
- `MULTIMODAL_AGENT_MEMORY_REMOTE_ENABLED`
- `MEMORY_SERVER_BASE_URL`
- `MEMORY_SERVER_TIMEOUT_SECONDS`
- `MEMORY_SERVER_QUERY_STRATEGY`
- `MEMORY_SERVER_DIRECT_ANSWER`
- `MEMORY_SERVER_INCLUDE_MEDIA_CHUNKS`

Relative JSONL and SQLite paths resolve from the repository root. JSONL and SQLite are still local-first storage, not real external providers. Additional PostgreSQL, vector DB, or external memory service adapters must sit behind `MemoryStore` and `MemoryManager`.

`hybrid_remote` is retrieval augmentation, not a full memory-service
replacement. `HybridMemoryStore.search(...)` merges local search results with
safe remote query results from the external Memory Server. Local results remain
first, remote failures are returned as recoverable `MemorySearchResult.errors`,
and the agent run must still proceed with local results when the remote service
is unavailable. `HybridMemoryStore.save(...)`, get/list/delete/hard-delete,
confirmation, profile, audit, export, retention, and user-data lifecycle paths
delegate to the local store until the external service exposes equivalent
governed APIs.

Remote Memory Server query defaults are conservative: `direct_answer=false` and
media chunks disabled unless explicitly configured. Remote text results are
validated into internal `MemoryItem` objects before context injection. Remote
image/keyframe chunks may become safe artifact refs; base64 payloads, data URIs,
raw provider payloads, and unsafe metadata must not enter memory content or
trace summaries. `RemoteMemoryClient.upload_media(...)` and
`RemoteMemoryClient.task_status(...)` are low-level adapter methods only.
`/v1/media/upload` is not an implementation of `memory_save`; media ingestion
is exposed through the separate `MemoryMediaIngestionService` plus
`memory_media_ingest` / `memory_ingest_status` tools. Upload metadata is
rejected when it contains raw/base64 or secret-like keys, generated `file_id`
values are created inside `assistant_agent`, and task status results carry a
scope warning because the external service's current task lookup is not
user-enforced. Default mock/local/offline configuration registers the tools but
returns `provider_unconfigured` until `hybrid_remote` and a Memory Server base
URL are explicitly configured.

Standard `MemoryStore` backends implement the confirmation workflow methods: `save_confirmation(...)`, `get_confirmation(...)`, `list_confirmations(...)`, and `delete_confirmation(...)`. InMemory keeps confirmation state in process memory. JSONL stores redacted pending/resolved confirmations in a sidecar file next to the memory JSONL file, for example `long_term_memories.confirmations.jsonl`. SQLite stores them in schema v3 `memory_confirmations`.

`SQLiteMemoryStore` also exposes local operator helpers for `backup_to(...)`, `restore_backup(...)`, `integrity_check()`, and `rebuild_indexes()`. These helpers cover `memory_items`, `memory_audit_events`, and `memory_confirmations`. Operational steps and rollback guidance live in `docs/development/memory-sqlite-operator-runbook.md`.

SQLite durability defaults remain production-oriented: normal runtime uses `synchronous=NORMAL`, a long `busy_timeout`, and WAL for newly created databases. Focused tests may pass explicit, validated pragmas such as `journal_mode="MEMORY"` and `synchronous="OFF"` to avoid slow filesystem fsyncs; those fast settings are test-only and must not become the runtime default.

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
- Superseded memories, identified by `content["superseded_by_memory_id"]`, are excluded from active retrieval and context injection by default. Debug/read-only callers may set `MemoryQuery.include_superseded=True`; the current public debug route is the memory snapshot API. Agent-callable memory tools do not expose this flag.

Ranking combines relevance, capability/type priority, artifact-ref signal, and recency. Capability-specific priorities currently exist for image generation, product search, render 3D, and direct chat.

## Retrieval Eval

Memory retrieval quality is measured before adding embedding or vector dependencies.

Current local eval boundary:

- `src/assistant_agent/memory/retrieval_eval.py` runs deterministic `InMemoryStore + MemoryManager` retrieval and context-injection cases.
- `scripts/run_evals.py --suite memory` includes the retrieval eval cases from `tests/evals/eval_cases.json`.
- Metrics include Recall@k, MRR, false-positive rate, correct-empty rate, cross-user leakage rate, sensitive/expired injection rate, and token budget compliance.
- Initial coverage includes black-bag recall, color preference recall, task/product resume, budget preference, unrelated empty recall, cross-user isolation, expired exclusion, sensitive non-injection, token budget compliance, and superseded-preference exclusion from active profile/context.

## Writes

Explicit saves:

- Flow through `memory_save` or `MemoryManager.save_explicit(...)`.
- Are evaluated by `MemoryWritePolicy.evaluate_explicit_save(...)` before any item is built.
- Return a `MemoryWriteDecision` with `allowed`, `destination`, `reason`, `require_user_confirmation`, `sensitivity`, `ttl_days`, and `redacted_payload`.
- If `allowed=True`, the durable `MemoryItem` is built through `build_explicit_memory_item(...)` and still passes `MemoryItem` payload validation before storage.
- If `require_user_confirmation=True`, the write creates a `MemoryPendingConfirmation` with only redacted summary and safe content preview. Standard stores persist or retain these confirmations through the `MemoryStore` confirmation methods: SQLite uses `memory_confirmations`, JSONL uses the confirmation sidecar, and InMemory keeps process-local state. No durable memory item is stored until the user confirms it through the confirmation API/service path.
- Confirming a pending explicit memory re-runs the normal explicit-memory builder on the redacted payload, stores the resulting item, and records both `memory_explicit_saved` and `memory_confirmation_decided` audit events.
- Rejecting a pending explicit memory records `memory_confirmation_decided` and does not write a memory item.
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

Current confirmation limit: SQLite and JSONL-backed pending confirmations survive runtime restart; InMemory remains process-local by design. `MemoryManager` keeps a process-local fallback only for legacy/non-conforming custom stores, but standard backends should implement the confirmation methods directly.

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

Explicit preference memories may carry a deterministic `content["preference_key"]`, such as `style` or `budget`. When a new explicit preference uses the same key and governance scope as an older active preference with a different summary, `MemoryManager` marks the older memory with `content["superseded_by_memory_id"]` and marks the newer memory with `content["supersedes_memory_ids"]`. This is a deterministic conflict/supersedes chain, not semantic inference. The first-pass rules only use explicit `preference_key`, known structured fields such as `style` and `budget`, and a small budget-summary fallback.

`MemoryManager.rebuild_user_profile_for_identity(...)` can check or repair the compact profile from current source memories. Source memories are identity-visible, unexpired, unscoped `preference`, `product`, and `task` items; tenant/project-scoped items are excluded until scoped profile storage is designed. Superseded source memories are excluded from the active profile and reported through `superseded_source_memory_ids` and `profile_conflicts`. The repair result reports missing, stale, orphaned, unresolved-conflict, and out-of-sync profile state. Repair can create, update, delete, or no-op the `user_profile` item and records a prompt-safe `memory_profile_repaired` audit event when invoked through the repair path.

## API And Audit

Memory API routes use `MemoryAuditService` and `MemorySnapshotService` over the runtime `MemoryManager`:

- `GET /memory/users/{user_id}/items`
- `GET /memory/users/{user_id}/items/{memory_id}`
- `GET /memory/users/{user_id}/audit`
- `GET /memory/users/{user_id}/events`
- `GET /memory/users/{user_id}/metrics`
- `GET /memory/users/{user_id}/confirmations`
- `POST /memory/users/{user_id}/confirmations/{confirmation_id}/confirm`
- `POST /memory/users/{user_id}/confirmations/{confirmation_id}/reject`
- `GET /memory/users/{user_id}/profile/status`
- `POST /memory/users/{user_id}/profile/rebuild`
- `GET /memory/users/{user_id}/export`
- `GET /memory/users/{user_id}/snapshot`
- `POST /memory/users/{user_id}/retention/sweep`
- `DELETE /memory/users/{user_id}/items/{memory_id}`
- `DELETE /memory/users/{user_id}/sessions/{session_id}`

List and snapshot endpoints do not include memory `content` by default. `include_content=True` returns sanitized content only. Snapshot also supports `include_superseded=True` for read-only debugging of supersedes chains without changing normal agent retrieval behavior. Export is identity-scoped and can omit content with `include_content=false`. Retention sweep scans only identity-visible memories, supports `dry_run=true`, soft-deletes expired items by default, and uses `MemoryManager.hard_delete_for_identity(...)` plus `MemoryStore.hard_delete(...)` when `hard_delete=true`. SQLite removes the row on hard delete; in-memory and JSONL stores already delete physically. Deletion is user-scoped and must not cross users even when memory IDs or session IDs match. Session/user delete also clears identity-visible pending confirmations.

Confirmation endpoints list pending or resolved explicit-memory confirmations and let a user accept or reject a sensitive-but-redacted explicit memory. Confirmed entries become normal durable memory items; rejected or expired entries remain audit/governance state only.

`MemoryManager` records prompt-safe lifecycle events: context load, explicit save/reject, confirmation create/decide, promotion decision, soft delete, hard delete, session delete, and user clear. `MemoryAuditService` adds export and retention-sweep events, and derives `MemoryMetricsReport` counters from the same event stream. In-memory and JSONL paths keep a bounded in-process event list. SQLite schema v2 persists events in `memory_audit_events`, with common filter fields split into columns and the full redacted event saved as JSON payload, so events survive runtime restarts for the local SQLite backend. SQLite schema v3 persists redacted pending/resolved confirmations in `memory_confirmations`; JSONL persists the same confirmation payload shape in its sidecar file. Production-grade external metrics export, backup packaging, and full rollback/rebuild runbooks remain future work. Event metadata must stay redacted and must not include raw memory content, raw tool/provider payloads, base64/media bodies, or secrets.

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
- Update this file, `AGENTS.md`, and any affected tests when the architecture changes. `README.md` remains a temporary placeholder until the project stabilizes.

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
