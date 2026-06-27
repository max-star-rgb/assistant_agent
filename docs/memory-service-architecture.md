# Memory Service Architecture

Last updated: 2026-06-27

This document is the current canonical entry for memory service architecture. Update it whenever `MemoryManager`, memory stores, retrieval, write policy, user profile behavior, memory tools, memory APIs, or memory context boundaries change.

Older Phase 8 memory documents remain background references. They are not the current design source when this file and code disagree.

## Scope

The memory service is local-first long-term memory for the agent. It covers:

- Loading bounded memory context before an agent run.
- Searching user-scoped long-term memory.
- Saving explicit user-requested memories.
- Saving safe completed-run summaries where allowed.
- Maintaining a compact `user_profile` memory derived from explicit memories.
- Exposing audit, delete, and snapshot views through service/API boundaries.

Conversation history is related but separate. Session conversation context is owned by conversation/session services and is combined with memory only in context packs and memory snapshots.

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
| `src/multimodal_agent/memory/manager.py` | Boundary for memory retrieval, layered context formatting, explicit saves, duplicate merge, user profile upsert, run-summary saves, get/list/delete passthroughs. |
| `src/multimodal_agent/memory/store.py` | `MemoryStore` protocol and process-local `InMemoryStore`. |
| `src/multimodal_agent/memory/jsonl_store.py` | Local JSONL persistent store implementing the same store contract. |
| `src/multimodal_agent/memory/retrieval.py` | Query filtering, relevance gating, type/capability priority, recency fallback rules, context formatting. |
| `src/multimodal_agent/memory/retriever.py` | Deterministic keyword and Chinese phrase-fragment retrieval. |
| `src/multimodal_agent/memory/write_policy.py` | Safe memory item construction, TTL defaults, raw payload restrictions, explicit memory typing. |
| `src/multimodal_agent/memory/profile.py` | Compact `user_profile` memory derived from explicit preference/product/task memories. |
| `src/multimodal_agent/tools/memory_tool.py` | Agent-callable `memory`, `memory_retrieval`, and `memory_save` tools. Uses `MemoryManager` from tool context when present. |
| `src/multimodal_agent/services/memory_audit.py` | User-scoped list/get/delete/audit service over `MemoryManager`. |
| `src/multimodal_agent/services/memory_snapshot.py` | Read-only snapshot combining memory context, session records, conversation history, audit, and storage boundary info. |
| `src/multimodal_agent/schemas/memory.py` | Public memory contracts and payload safety validation. |
| `src/multimodal_agent/schemas/memory_audit.py` | API-facing audit/delete/list models. |
| `src/multimodal_agent/schemas/memory_snapshot.py` | API-facing memory boundary snapshot models. |

Agent nodes, assistant loops, API routes, MCP routes, and tools should not depend directly on concrete stores. Use `MemoryManager` or a service that wraps it.

## Storage

Configured by `ProviderConfig`:

- `memory_backend="memory"`: default process-local `InMemoryStore`.
- `memory_backend="jsonl"`: local `JsonlMemoryStore`.
- `memory_path`: default `.local/memory/long_term_memories.jsonl`.

Environment variables:

- `MULTIMODAL_AGENT_MEMORY_BACKEND`
- `MULTIMODAL_AGENT_MEMORY_PATH`

Relative JSONL paths resolve from the repository root. JSONL is still local-first storage, not a real external provider. Future SQLite, PostgreSQL, vector DB, or external memory service adapters must sit behind `MemoryStore` and `MemoryManager`.

## Contracts

Core models:

- `MemoryItem`: one retrievable memory item with `user_id`, optional `session_id`, `memory_type`, safe `content`, `summary`, tags, artifact refs, timestamps, TTL, relevance, reason, and sensitivity.
- `MemoryQuery`: user-scoped query options, including `session_id`, text query, capability, memory types, tags, `top_k`, `max_context_chars`, `since`, and `include_expired`.
- `MemorySearchResult`: structured search output with items, query used, total, ranking reason, context text, and errors.

`MemoryItem` validation rejects unsafe payload keys such as API keys, tokens, raw media, base64 payloads, and raw provider responses. It also rejects inline media data URIs and sanitizes summaries, reasons, tags, and string content.

Do not store raw user text by default, raw provider responses, real media bodies, secrets, bearer tokens, API keys, or unredacted external service payloads. Store references and safe summaries instead.

## Layers

`MemoryManager.build_context()` groups memory items into prompt-safe layers:

| memory type | layer |
| --- | --- |
| `preference` | `semantic` |
| `conversation` | `session` |
| `task` | `episodic` |
| `product`, `artifact`, `image`, `video`, `generation`, `render` | `artifact` |
| future procedural memory | `procedural` |

The rendered memory context is written to:

- `AgentState.memory_context`
- `request.metadata["memory_context_text"]`
- `request.metadata["memory_context_summaries"]`
- `request.metadata["memory_context_refs"]`
- `request.metadata["memory_context_blocks"]`

Prompt rendering must treat memory as user-history data, not as system instruction.

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

## Writes

Explicit saves:

- Flow through `memory_save` or `MemoryManager.save_explicit(...)`.
- Require non-empty text or summary.
- Infer memory type from explicit text/content. Stable preferences become `preference`; product-like content becomes `product`; otherwise default is `task`.
- Do not save raw user text unless `MemoryWritePolicy.auto_save_raw_user_text=True`.
- Merge duplicates by normalized summary and memory type.
- Update compact `user_profile` for explicit `preference`, `product`, and `task` items.

Run-summary saves:

- Flow through `MemoryManager.save_from_run(...)`.
- Only save completed runs with a response.
- Skip pure memory-save runs.
- Store safe task summaries and output refs, not raw request/provider payloads.
- Respect `MemoryWritePolicy.auto_save_task_summary`.
- Are skipped in assistant-loop non-mock chat paths so the model can choose `memory_save` explicitly.

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
- `GET /memory/users/{user_id}/snapshot`
- `DELETE /memory/users/{user_id}/items/{memory_id}`
- `DELETE /memory/users/{user_id}/sessions/{session_id}`

List and snapshot endpoints do not include memory `content` by default. `include_content=True` returns sanitized content only. Deletion is user-scoped and must not cross users even when memory IDs or session IDs match.

`DELETE /beta/users/{user_id}/data` clears memory through `runtime.memory_manager.clear_user(user_id)` as part of broader user-data deletion.

## Design Rules

- Read this document before designing or changing memory service behavior.
- Keep Agent/API/MCP code behind `MemoryManager`, `MemoryAuditService`, `MemorySnapshotService`, `ToolExecutor`, or memory tools.
- Do not let assistant nodes or API routes directly instantiate or query concrete stores.
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
```

For broad behavior changes, run the full offline suite:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_demo_flows.py
```
