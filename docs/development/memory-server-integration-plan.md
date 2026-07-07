# Memory Server Integration Plan

Last updated: 2026-07-07

This is the development plan for integrating the external Memory Server into
`assistant_agent`. It is not the architecture authority. The architecture
authority remains `docs/memory-service-architecture.md`; when implementation
changes `MemoryManager`, memory store behavior, memory tool routing, memory
APIs, or context injection boundaries, update that document in the same change.

Source documents for the external service:

- `docs/memory_server_api_spec.md`: current HTTP API contract.
- `docs/memory_server_software_implementation_design.md`: external service
  implementation design and operational notes.

## Purpose

`assistant_agent` already has a local-first memory service. The external Memory
Server provides a different capability set: multimodal media ingestion,
PostgreSQL/pgvector-backed retrieval, optional keyframe evidence, session
history APIs, and optional direct answer generation.

The integration target is:

```text
assistant_agent remains the agent runtime and memory governance owner.
Memory Server is an opt-in remote retrieval and media-ingestion backend.
```

This keeps the current agent boundaries intact:

- `MemoryManager` remains the boundary for memory retrieval, context building,
  explicit saves, write policy, profile updates, confirmation, audit, delete,
  export, and retention behavior.
- `memory_retrieval` and `memory_save` remain thin tool adapters over
  `MemoryManager`.
- Context builders and prompt renderers consume bounded memory context; they do
  not call the external service directly.
- The external service is never enabled merely because a URL or credential is
  present. It must be configured explicitly through a pilot/provider profile.

## Current Capability Comparison

| Area | `assistant_agent` current memory | External Memory Server |
| --- | --- | --- |
| Runtime role | Agent long-term memory service and governance boundary | Remote multimodal memory platform |
| Default behavior | Local/mock/offline | Docker/GPU/PostgreSQL plus optional real providers |
| Retrieval | Deterministic local keyword and fallback ranking | `long_context`, vector, hybrid retrieval |
| Writes | Explicit memory save through `MemoryWritePolicy` and `MemoryItem` validation | Media ingestion stores extracted memories; no equivalent safe explicit text-save API in the provided contract |
| Media | Stores safe refs in memory items; media understanding is separate tool capability | Upload media, materialize files, extract memories, optional keyframes/media index |
| Direct answer | Owned by assistant loop/final response path | Optional query-time answer backend |
| Audit/lifecycle | List/get/export/delete/retention/audit/confirmation/profile APIs | Task status and query trace; no full memory audit/delete/export contract in provided docs |
| Identity | `RequestIdentity` with tenant/project/session/scope fields | `user_id` and optional `session_id`; current task lookup does not enforce user scope |

## Recommended Architecture

Phase 1 should integrate the external service as a remote retrieval source, not
as a full replacement for the local memory service.

```text
AgentGraphRuntime
  -> create_memory_store(ProviderConfig)
  -> MemoryManager(store)
  -> ToolExecutor(context_metadata={"memory_manager": manager})

store = HybridMemoryStore
  -> local store: save/list/get/delete/confirm/audit/profile-compatible behavior
  -> remote client: /v1/memories/query for opt-in remote retrieval
```

Rules:

- `HybridMemoryStore.search(...)` may call the remote query endpoint and merge
  remote results with local results.
- `HybridMemoryStore.save(...)`, `get(...)`, `list_by_user(...)`, `delete(...)`,
  `hard_delete(...)`, confirmation methods, and lifecycle behavior should
  delegate to the local store until the external service exposes equivalent
  governance APIs.
- Remote failures must be recoverable. A timeout or HTTP error should return
  local results plus a prompt-safe `MemorySearchResult.errors` entry; it must
  not fail the whole agent run.
- Remote results must be converted into safe internal `MemoryItem` objects
  before context injection.
- `direct_answer` should default to `false`. The assistant loop remains the
  default final-answer owner.

## Out Of Scope For Initial Integration

Do not implement these in the first phase:

- Replacing `MemoryManager` with a Memory Server client.
- Letting `memory_save` directly call `/v1/media/upload`.
- Enabling Memory Server direct answers as the default agent answer path.
- Syncing all local memory audit/export/delete/profile operations to the
  external service.
- Treating external `session_history` as `assistant_agent` conversation history.
- Enabling remote memory for default mock/local/offline tests.
- Sending raw provider payloads, base64 media, API keys, tokens, or unredacted
  user data into tracked files or trace summaries.

## API Mapping

### Health

Use `/v1/health` for optional startup or smoke checks only. Runtime operation
should not require health checks before every agent run.

Mapping:

| Internal need | External request |
| --- | --- |
| User-scoped readiness check | `GET /v1/health?user_id=<user_id>` |
| Session-scoped readiness check | `GET /v1/health?user_id=<user_id>&session_id=<session_id>` |

### Retrieval

Use `/v1/memories/query` for remote search.

| `MemoryQuery` field | Memory Server request field |
| --- | --- |
| `user_id` | `user_id` |
| `session_id` | `session_id` |
| `query` | `query` |
| `top_k` | `top_k` |
| `since` | `after_timestamp` |
| configurable retrieval strategy | `options.strategy` |
| debug opt-in only | `options.trace` |

Defaults:

- `direct_answer=false`
- `options.include_media_chunks=false` for initial text-only context injection
- timeout bounded by assistant runtime config
- no retry unless a later reliability plan defines retry budget

### Remote Result To `MemoryItem`

Only `results[]` entries with `type == "text"` should become `MemoryItem`
records. Image/keyframe chunks should be folded into `artifact_refs` or safe
metadata on the related text memory.

| Memory Server `ScoredMemory` | Internal `MemoryItem` |
| --- | --- |
| `source.memory_id` | `memory_id` with remote prefix if needed |
| request `user_id` | `user_id` |
| request `session_id` or `source.session_id` if later added | `session_id` |
| `content` | `summary` |
| `score` | bounded `relevance` |
| `source.timestamp_start` | `created_at` |
| `source.timestamp_end` | `content.timestamp_end` |
| `source.source_id` | `content.source_id` |
| `image_url` or keyframe URL | `artifact_refs` |
| `metadata.topic`, `metadata.subtopic` | safe tags or content metadata |

Memory type mapping:

| Memory Server type | Initial internal type | Notes |
| --- | --- | --- |
| `semantic` | `preference` or `task` | Prefer `task` unless the external payload clearly represents a stable preference. |
| `episodic` | `conversation` or `task` | Use `conversation` only for session-like remembered dialogue; otherwise `task`. |
| `procedural` | `task` | Internal schema does not yet expose procedural memory. |
| `spatial` | `video` | Keep source and timestamp metadata for media grounding. |
| `keyframe` | none | Store URL as artifact ref, not as a standalone memory item. |

Safety mapping:

- Drop `image_base64` and `media.base64`.
- Drop raw prompt, raw provider response, embeddings, and large metadata.
- Sanitize summaries and reasons through normal `MemoryItem` validation.
- Preserve remote provenance only as safe IDs, URLs, timestamps, and compact
  metadata.

### Media Ingestion

Media ingestion should be a separate service/tool path after remote retrieval
works.

```text
memory_media_ingest tool/service
  -> RemoteMemoryClient.upload_media(...)
  -> /v1/media/upload
  -> returns task_id

memory_ingest_status tool/service
  -> RemoteMemoryClient.task_status(...)
  -> /v1/tasks_status
```

Rules:

- Do not route media upload through `memory_save`.
- Generate globally unique `file_id` values in `assistant_agent` because the
  current external service treats `media_files.file_id` as a global primary key.
- Surface upload status as task state or tool result, not as durable memory
  until the external ingestion task completes and remote retrieval can find it.
- Treat `/v1/tasks_status` user scope as weak until the external service enforces
  task lookup by user.

### Session History

Do not wire `/v1/sessions/add_history` or `/v1/sessions/get_history` into the
current conversation/session context by default.

Reason: `assistant_agent` already has session/conversation services and context
compaction boundaries. The external session history API can be evaluated later
as an import/export or cross-device history source, but it should not become the
current context authority without a separate context-engineering review.

## Configuration

Add explicit opt-in config only when implementation begins.

Candidate environment variables:

```text
MULTIMODAL_AGENT_MEMORY_BACKEND=hybrid_remote
MULTIMODAL_AGENT_MEMORY_REMOTE_ENABLED=true
MEMORY_SERVER_BASE_URL=http://127.0.0.1:5200
MEMORY_SERVER_TIMEOUT_SECONDS=2.0
MEMORY_SERVER_QUERY_STRATEGY=vector
MEMORY_SERVER_DIRECT_ANSWER=false
MEMORY_SERVER_INCLUDE_MEDIA_CHUNKS=false
```

Provider profile rules:

- Default `memory`, `jsonl`, and `sqlite` behavior remains local/offline.
- Remote memory should require an explicit pilot/provider-smoke profile or an
  explicit remote-enabled flag checked by runtime config.
- Remote answer/extraction providers used inside Memory Server are outside this
  repository, but the final run report must mention that the agent called a
  remote service when smoke testing is requested.

## Phased Plan

### Phase 0: Contract Fixtures And Boundary Tests

Goal: lock the adapter contract without making network calls.

Work:

- Add response fixtures for `/v1/memories/query`, including text-only,
  text-plus-keyframe, empty results, direct-answer-disabled, and error-like
  responses.
- Add a remote result mapper that converts fixture payloads to safe
  `MemoryItem` objects.
- Test memory type mapping, timestamp handling, relevance bounds, artifact refs,
  base64 dropping, and unsafe metadata rejection.
- Test that remote `user_id` is bound from runtime identity, not model input.

Acceptance:

- Unit tests pass offline.
- No real Memory Server is required.
- No remote base64/raw provider payload can enter `MemoryItem.content`.

### Phase 1: Remote Query Client

Goal: implement a small HTTP client for query and health.

Work:

- Add `RemoteMemoryClient` with bounded timeout, JSON parsing, and prompt-safe
  error summaries.
- Implement `query_memories(...)` and optional `health(...)`.
- Keep dependencies minimal; prefer the repo's existing HTTP approach if one is
  already established, otherwise use standard-library or existing installed
  client without adding dependencies.

Acceptance:

- Client tests use fake transport or monkeypatched request function.
- Timeout and HTTP errors return structured, redacted failures.

### Phase 2: Hybrid Store

Goal: make remote retrieval available through `MemoryManager` without changing
memory tools or prompt builders.

Work:

- Extend memory backend config to allow a remote/hybrid backend.
- Add `HybridMemoryStore` that delegates lifecycle/write methods to a local
  store and merges remote search results into `search(...)`.
- Preserve deterministic ordering: local exact matches should remain stable;
  remote scores can participate in ranking only after mapping to bounded
  relevance.
- Record retrieval source in safe content metadata or tags.

Acceptance:

- Existing local memory tests still pass.
- Remote disabled path exactly preserves current behavior.
- Remote failure degrades to local search.
- `memory_retrieval` and graph `load_memory` use remote results only through
  `MemoryManager`.

### Phase 3: Runtime Wiring And Smoke Test

Goal: enable opt-in manual testing against a running Memory Server.

Work:

- Add env parsing and runtime config fields.
- Add a smoke script or targeted test marker that calls `/v1/health` and
  `/v1/memories/query` only when explicitly enabled.
- Document command lines and expected output.

Acceptance:

- Offline suite does not call the remote service.
- Opt-in smoke test reports remote URL, query strategy, result count, and
  degraded/failure reason.

### Phase 4: Media Ingestion Tool/Service

Goal: expose Memory Server media ingestion as a separate controlled capability.

Work:

- Add typed request/response models for media upload and task status.
- Add a dedicated tool or service boundary for ingestion and status polling.
- Ensure `file_id` generation is globally unique.
- Add tests for accepted task response, task status mapping, and user-scope
  warnings.

Acceptance:

- Media upload does not bypass tool governance.
- Upload/status results are structured and trace-safe.
- `memory_save` remains explicit text memory save through local policy.

### Phase 5: Authority Doc Update

Goal: promote stable integration boundaries into the memory architecture doc.

Work:

- Update `docs/memory-service-architecture.md` after the implementation
  boundary is stable.
- Update `AGENTS.md` only if repository-level routing or default operating
  rules change.

Acceptance:

- Architecture doc states whether remote memory is retrieval-only, hybrid, or a
  full store backend.
- Development plan remains a phase record, not the source of truth.

## Risks And Open Questions

- The external service currently has no production-grade auth boundary.
- `/v1/tasks_status` accepts `user_id` but currently looks up by `task_id` only.
- The external API does not provide full list/get/delete/export/retention/audit
  behavior, so it cannot replace local memory lifecycle APIs yet.
- The external API does not provide an equivalent explicit safe text-memory save
  contract with `source_intent`, confirmation, and `MemoryWritePolicy`.
- Memory Server `direct_answer` may duplicate or bypass the assistant loop's
  final-answer responsibility.
- Remote media/keyframe data can contain base64 payloads; these must not enter
  internal memory content or trace payloads.
- External memory types do not match current internal `MemoryType` exactly.
- Remote retrieval latency can affect every agent run if not timeout-bounded.
- Running Memory Server may call real external providers through its own
  extraction or answer backends; smoke reports must make that explicit.

## Validation Commands

For documentation-only changes:

```bash
git diff --check -- docs/development/memory-server-integration-plan.md
```

For the first implementation phase:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_store_boundary.py tests/test_memory_tool_boundary.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_runtime_integration.py -q
```

For remote smoke testing, add an explicit opt-in command after the client exists.
