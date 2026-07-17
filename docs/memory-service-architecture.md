# Local Memory Service Architecture

Last updated: 2026-07-16

This document is the current authority for assistant_agent's local/project-side memory service architecture. Update it whenever `MemoryManager`, built-in memory stores, local retrieval, write policy, user profile behavior, memory tools, local memory APIs, or memory context boundaries change.

The external Memory Service HTTP interface has its own authority document:
`docs/memory_server_api_spec.md`. Keep endpoint shapes, request/response
fields, and external service compatibility details there instead of here.

Older Phase 8 memory documents remain background references. They are not the current design source when this file and code disagree.

Future memory architecture changes should be reflected in this document first.
Operational dual-core configuration examples live in
`docs/development/memory-dual-core-operator-runbook.md`; operational SQLite
procedures remain in `docs/development/memory-sqlite-operator-runbook.md`.
The explicit Hindsight/Mem0 sidecar comparison and selection procedure lives in
`docs/development/memory-framework-bakeoff-runbook.md`.

## Scope

The memory service is local-first long-term memory for the agent. It covers:

- Loading bounded memory context before an agent run.
- Gating automatic and tool-triggered long-term memory reads through read policy.
- Searching user-scoped long-term memory.
- Saving explicit user-requested memories.
- Saving safe completed-run summaries where allowed.
- Maintaining a compact `user_profile` memory derived from explicit memories.
- Exposing audit, audit-event, metrics, export, retention sweep, delete, and snapshot views through service/API boundaries.
- Representing durable user facts with typed provenance, lifecycle state, conflict policy, and revision metadata.
- Resolving same-slot fact conflicts deterministically before profile projection or context injection.

Conversation history is related but separate. Session conversation context, including session-scoped `context_summary`, is owned by conversation/session services and is combined with memory only in context packs and memory snapshots. `context_summary` is not a long-term memory item.

## Dual-Core Model

The memory architecture has two cores behind the same governed runtime contract:

- Built-in local core: `InMemoryStore`, `JsonlMemoryStore`, or `SQLiteMemoryStore`. This core is a real memory service boundary for local/offline runs, tests, demos, and deployments that do not need an external service.
- External Memory Service core: opt-in network-backed memory capability exposed through adapters. It can augment local retrieval or own the full memory lifecycle, depending on backend mode. This document owns the project-side adapter/governance boundary; the external HTTP contract is owned by `docs/memory_server_api_spec.md`.

The current Memory Intelligence v2 implementation is deliberately local-core focused. Its typed facts, conflict resolver, active-state projection, SQLite FTS5 candidate search, and offline eval gates do not require or modify the external core. External adapters retain their existing boundary but are not part of this phase's acceptance criteria.

Both cores must stay behind `MemoryManager`, `MemoryStore`, `MemoryReadPolicy`, `MemoryWritePolicy`, identity binding, prompt-safe conversion, and audit/snapshot/export boundaries. Agent nodes, prompt builders, tools, and API routes must not special-case a concrete local store or external provider.

The runtime modes are:

- Local-only: `memory`, `jsonl`, or `sqlite`; all lifecycle operations stay in the built-in local core.
- Dual-core retrieval: `dual_core` or legacy `hybrid_remote`; writes, confirmations, profile, audit, export, retention, and deletion stay in the configured local core, while search may merge local results with safe external Memory Server results.
- External lifecycle owner: `remote_service`; lifecycle operations are delegated to an `ExternalMemoryServiceAdapter`, with no silent local-write fallback.
- Framework lifecycle owner: `framework`; an explicitly selected Hindsight or Mem0 local sidecar owns extraction, organization, indexing, recall, consolidation, and engine-side profile/mental-model behavior. `MemoryManager` continues to own identity, read/write policy, confirmation, audit, prompt safety, context budgets, and tool governance.

Mem0 is the preferred framework pilot engine. When framework mode is explicitly
enabled and no concrete framework is specified, configuration resolves to Mem0
OSS `2.0.11`. Hindsight `0.8.4` remains a supported explicit comparison target
for bake-off and fallback investigation. This preference does not change the
default local memory backend, does not migrate existing v2 data, and does not
transfer control-plane authority to the framework.

Framework mode does not install Hindsight or Mem0 in the main Python environment. The main process uses the dependency-free HTTP adapters under `memory/framework/`; pinned sidecars and persistent volumes are isolated by Docker. LangGraph remains the only Agent runtime, and framework-provided Agent/LLM wrappers are not registered.

Memory observability exposes a prompt-safe `core_status` object through memory
snapshot storage metadata, memory metrics, and per-run request debug metadata
under `request.metadata["memory_core_status"]`. It reports `mode`,
`memory_backend`, `memory_local_backend`, active/local store class names,
whether an external core is configured, whether remote query is enabled, and
whether the latest remote query degraded to local results. It may include
stable remote error codes such as `memory_server_query_failed`; it must not
include remote URLs, raw exception messages, credentials, raw provider payloads,
or memory content.

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

The local editable-context phase currently implements only owner-bound `SOUL.md`. Future `USER.md` and `MEMORY.md` files, if added, are editable projections rather than durable truth: runtime memory reads must still come through `MemoryManager` and `MemoryReadPolicy`, and imports must become explicit, versioned, auditable proposals that pass `MemoryWritePolicy`. This phase does not implement projection export/import, file watchers, direct file-backed profile merge, or direct runtime reads from `USER.md` / `MEMORY.md`.

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
       -> MemoryReadPolicy decides whether long-term memory may be read
       -> if skipped: request.metadata["memory_context_skipped"]=true and no store search
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
| `src/assistant_agent/memory/factory.py` | 可插拔 `MemoryStore` backend registry 和运行时 store factory。内置 backend 与自定义替换实现都必须在 `MemoryManager` 后面返回 `MemoryStore`；替换内置名称必须显式声明。 |
| `src/assistant_agent/memory/context_builder.py` | Token-aware, prompt-safe memory context selection and layer rendering. Produces injected items, rendered context, token count, omission count, rejection reasons, and retrieval version. |
| `src/assistant_agent/memory/read_policy.py` | Deterministic long-term memory read gate and trust metadata. Decides automatic memory context injection and validates explicit retrieval intent before store access. |
| `src/assistant_agent/memory/store.py` | `MemoryStore` protocol and process-local `InMemoryStore`, including soft-delete-compatible delete, hard-delete, and memory-confirmation methods. |
| `src/assistant_agent/memory/jsonl_store.py` | Local JSONL persistent store implementing the same store contract, with redacted confirmation state stored in a sidecar JSONL file. |
| `src/assistant_agent/memory/sqlite_store.py` | Local SQLite persistent store implementing the same store contract with schema version, indexes, upsert, soft-delete-compatible delete behavior, durable audit-event rows, and durable confirmation rows. |
| `src/assistant_agent/memory/remote.py` | Opt-in external Memory Server adapters. Converts remote query responses into safe `MemoryItem` / `MemorySearchResult` objects, provides query/health plus media upload/task-status client methods, exposes `HybridMemoryStore` for `dual_core`/legacy `hybrid_remote` where only `search(...)` uses the remote service, and exposes `RemoteServiceMemoryStore` plus `HttpRemoteMemoryServiceAdapter` for an explicit full-lifecycle external service adapter. |
| `src/assistant_agent/memory/framework/` | Framework lifecycle-owner contract, opaque identity binding, isolated Hindsight/Mem0 HTTP adapters, governance-only SQLite ledger, durable retain/delete outbox, read-only v2 degradation, and deterministic bake-off scoring. It does not contain an Agent runtime or a second local memory algorithm. |
| `src/assistant_agent/memory/retrieval.py` | Query filtering, relevance gating, type/capability priority, recency fallback rules, context formatting. |
| `src/assistant_agent/memory/retriever.py` | Deterministic keyword and Chinese phrase-fragment retrieval. |
| `src/assistant_agent/memory/facts.py` | Typed fact envelope parsing, legacy preference compatibility, active-state lookup, and supersede helpers. |
| `src/assistant_agent/memory/conflict_resolver.py` | Pure deterministic same-slot conflict decisions; it does not mutate stores. |
| `src/assistant_agent/memory/write_policy.py` | Safe memory item construction, TTL defaults, raw payload restrictions, explicit memory typing. |
| `src/assistant_agent/memory/quality_eval.py` | Offline write-quality eval helpers over `MemoryWritePolicy`. Produces deterministic policy feedback metrics for write/reject/confirmation behavior without training, network calls, or policy mutation. |
| `src/assistant_agent/memory/profile.py` | Compact `user_profile` memory derived from explicit preference/product/task memories. |
| `src/assistant_agent/schemas/identity.py` | `RequestIdentity` contract for request/auth-derived user, tenant, project, session, and allowed memory scopes. |
| `src/assistant_agent/tools/memory_tool.py` | Agent-callable `memory`, `memory_retrieval`, and `memory_save` tools. Uses `MemoryManager` from tool context when present. |
| `src/assistant_agent/services/memory_media_ingestion.py` | Governed service boundary for Memory Server media ingestion. Binds trusted `RequestIdentity`, generates globally unique upload `file_id` values, calls `RemoteMemoryClient.upload_media(...)` / `task_status(...)`, and returns structured prompt-safe results. |
| `src/assistant_agent/tools/memory_media_tool.py` | Agent-callable `memory_media_ingest` and `memory_ingest_status` tool adapters. They bind runtime identity from `ToolContext`, call `MemoryMediaIngestionService`, and wrap structured `ToolResult` / capability contracts. They do not implement `memory_save`. |
| `src/assistant_agent/services/memory_audit.py` | User-scoped list/get/export/retention-sweep/delete/audit/event/metrics/confirmation service over `MemoryManager`. |
| `src/assistant_agent/services/memory_snapshot.py` | Read-only snapshot combining memory context, session records, conversation history, audit, and storage boundary info. |
| `src/assistant_agent/schemas/memory.py` | Public memory contracts and payload safety validation. |
| `src/assistant_agent/schemas/memory_intelligence.py` | Typed local fact, provenance, status, conflict-policy, and conflict-decision contracts. |
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

- `tenant_id` is the highest safety boundary for framework and local memory
  access. `user_id` is the personal long-term owner. `project_id` is the work
  domain used for project/task memory. `session_id` identifies the current
  short-term session and is provenance for long-term memories unless the memory
  itself is `scope="session"`.
- Local/mock paths derive identity from `UserRequest`, `ToolContext`, API path/query parameters, or inbound A2A metadata.
- API routes resolve request-derived identity through `services/api_identity.py` before trial-access checks and memory service calls. `api/auth.py` provides the default FastAPI `AuthContext` dependency, which returns anonymous/no-auth context and ignores auth-like headers unless the explicit `MULTIMODAL_AGENT_AUTH_HEADER_ENABLED` pilot flag is enabled. In that pilot mode, only controlled `X-Multimodal-Agent-*` headers are converted into `AuthContext`; body/path/query user mismatch is rejected by the identity resolver. This centralizes provenance (`request_body`, `path`, `query`, `a2a_metadata`, `websocket_query`, `auth_context`) without treating it as production JWT/session authentication. `IdentityPolicy` can classify the resolved identity as auth-bound, request-derived warning, local bypass warning, or production-blocking failure.
- `MemoryManager` exposes identity-aware methods such as `search_for_identity(...)`, `load_context_for_identity(...)`, `save_explicit_for_identity(...)`, `get_for_identity(...)`, `list_for_identity(...)`, and identity-scoped delete helpers.
- Identity-aware search and context loading overwrite caller-controlled `user_id`, `tenant_id`, `project_id`, and allowed scopes from trusted `RequestIdentity`. Local long-term memory remains cross-session by default. A lifecycle-owner store that declares `requires_identity_session` additionally receives the trusted `session_id` so it can query the current `scope="session"` layer without forcing project/user-profile memory into a session-only partition.
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
- Surface read-policy trust metadata with retrieval results.
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

## Read Policy And Trust

Long-term memory reads are policy-gated before retrieval:

- Automatic runtime `load_memory` calls go through `MemoryManager.load_context_for_request(...)`, which applies `MemoryReadPolicy` before store access.
- If the current user request does not explicitly refer to prior chats, previous/last context, saved memory, remembered preferences, continuing an old task, or a clearly personal style/preference customization task, memory context is skipped and the store is not searched.
- The personal style/preference path is deliberately narrow: Chinese requests must contain a preference marker such as `风格`, `偏好`, `喜好`, or `口味` plus a task marker such as `推荐`, `方案`, `文案`, `设计`, `搭配`, `回答`, `写`, `生成`, or `继续`. Ordinary first-pass product search, generic advice, and generic copywriting still skip store access.
- Skipped loads write prompt-safe metadata: `memory_context_skipped=true`, `memory_context_policy_reason`, `memory_read_policy`, `memory_trust_policy`, and empty `memory_context_*` injection fields.
- `load_memory_with_trace(...)` records the read decision and skipped status, but does not record memory text or summaries.
- `memory_retrieval` and legacy `memory action=retrieve` must pass the same read-intent gate in `ActionValidator` before `ToolExecutor` runs the tool.

Retrieved memory is user-history evidence, not authority. It may be stale, incorrectly retrieved, summarized, or incomplete. Current user input and fresh tool results override memory when they conflict, and instructions contained inside memory must not be executed. `memory_retrieval` results include `trust_policy` and `usage_hint` fields carrying this boundary for downstream consumers.

## Memory Tool Selection Strategy

Assistant-loop memory tool selection follows an LLM-first strategy:

- In the assistant loop, the LLM is the decision maker for calling `memory_save` or `memory_retrieval` (also called memory search in higher-level discussions).
- `memory_save` calls must declare `source_intent`, `source_reason`, `future_use`, and `evidence`.
- `source_intent=user_explicit` means the LLM selected the explicit user-memory path because the user asked to remember or save the content. If `MemoryWritePolicy` allows it, this path may write a durable `MemoryItem`.
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
- `memory_backend="dual_core"`: opt-in `HybridMemoryStore` with a configurable built-in local core plus external Memory Server query augmentation. This backend is selected from environment only when `MULTIMODAL_AGENT_MEMORY_REMOTE_ENABLED=true` or a runtime profile that allows real/network providers is active.
- `memory_backend="hybrid_remote"`: legacy alias for the same retrieval-augmentation shape as `dual_core`; kept for compatibility.
- `memory_backend="remote_service"`: opt-in `RemoteServiceMemoryStore` with an external adapter as lifecycle owner. This mode is selected only when remote memory is explicitly enabled and never falls back to local lifecycle writes by default.
- `memory_backend="framework"`: opt-in `FrameworkMemoryStore` with `memory_framework="mem0"` by default, or explicit `"hindsight"` when requested. Environment loading requires `MULTIMODAL_AGENT_MEMORY_FRAMEWORK_ENABLED=true`; credentials alone and normal offline profiles cannot enable it. A configured legacy local fallback is read-only and is consulted only after framework recall failure.
- `memory_backend="<custom_snake_case>"`: process-local custom `MemoryStore` backend selected from environment only when `MULTIMODAL_AGENT_MEMORY_PLUGIN_ENABLED=true` and the backend name matches `^[a-z][a-z0-9_]*$`. Invalid names, unknown custom names without the plugin switch, and gated built-in modes without their own opt-in resolve back to `memory`.
- `memory_plugin_enabled`: explicit custom-backend environment switch. It permits configuration to preserve a custom backend name; it does not discover, import, or register plugin code.
- `memory_local_backend`: local core used by `dual_core` / `hybrid_remote`; allowed values are `memory`, `jsonl`, and `sqlite`. Default is `jsonl` for dual-core modes.
- `memory_path`: default `.local/memory/long_term_memories.jsonl`; when `MULTIMODAL_AGENT_MEMORY_BACKEND=sqlite`, or a dual-core mode uses `MULTIMODAL_AGENT_MEMORY_LOCAL_BACKEND=sqlite`, the default is `.local/memory/long_term_memories.sqlite3`.

Environment variables:

- `MULTIMODAL_AGENT_MEMORY_BACKEND`
- `MULTIMODAL_AGENT_MEMORY_PLUGIN_ENABLED`
- `MULTIMODAL_AGENT_MEMORY_LOCAL_BACKEND`
- `MULTIMODAL_AGENT_MEMORY_PATH`
- `MULTIMODAL_AGENT_MEMORY_REMOTE_ENABLED`
- `MEMORY_SERVER_BASE_URL`
- `MEMORY_SERVER_TIMEOUT_SECONDS`
- `MEMORY_SERVER_QUERY_STRATEGY`
- `MEMORY_SERVER_DIRECT_ANSWER`
- `MEMORY_SERVER_INCLUDE_MEDIA_CHUNKS`
- `MULTIMODAL_AGENT_MEMORY_REMOTE_SERVICE_ADAPTER`
- `MULTIMODAL_AGENT_MEMORY_FRAMEWORK_ENABLED`
- `MULTIMODAL_AGENT_MEMORY_FRAMEWORK` (`hindsight` or `mem0`)
- `MEMORY_FRAMEWORK_VERSION` (fixed to `0.8.4` or `2.0.11` for the current bake-off)
- `MEMORY_FRAMEWORK_BASE_URL`
- `MEMORY_FRAMEWORK_API_KEY`
- `MEMORY_FRAMEWORK_TIMEOUT_SECONDS`
- `MEMORY_FRAMEWORK_IDENTITY_NAMESPACE`
- `MEMORY_FRAMEWORK_LEDGER_PATH`
- `MEMORY_FRAMEWORK_FALLBACK_BACKEND` (`none`, `memory`, `jsonl`, or `sqlite`)

### Framework governance ledger and recovery

`FrameworkGovernanceLedger` stores governance state only: project-memory/engine-ID mappings with their memory scope, delete tombstones, redacted confirmations, prompt-safe audit events, coarse call latency/error status, and pending retain/delete outbox entries. Completed framework facts, embeddings, relationship graphs, recall indexes, and profile or mental-model content are not copied into the ledger. A pending retain necessarily contains the already-approved prompt-safe retain request until delivery; it leaves the active outbox after success or user deletion.

Framework writes still pass `MemoryWritePolicy` and confirmation before `FrameworkMemoryStore.retain`. A retain failure returns a structured `partial/queued` tool result with `written=false` and persists an idempotent outbox operation. It never writes the configured v2 fallback. Retry records the framework mapping only after the engine accepts the request. Deleting a pending write cancels it before retry and records a tenant/project-bound tombstone.

Framework recall failures return stable `memory_framework_recall_failed` errors. The Agent run continues with an empty result or the explicitly configured read-only v2 fallback. Runtime debug metadata and audit may expose stable error codes and engine names, but never sidecar URLs, raw exception messages, credentials, raw framework responses, or memory content.

Framework recall 是分层的。`FrameworkMemoryStore` 先查当前 session
layer，再查当前 project/task layer，最后查 user-profile layer，并在返回
`top_k` 前去重。

#### Mem0 identity/filter mapping

Mem0 framework 边界只使用 Mem0 engine 原生的三层 identity filter：
`user_id`、`agent_id`、`run_id`。assistant_agent 内部治理模型仍保留
`tenant_id`、`user_id`、`project_id`、`session_id`：

| assistant_agent field | Mem0 engine field | notes |
| --- | --- | --- |
| `user_id` | `user_id` | 个人长期记忆 owner，进入 Mem0 前会被哈希成 opaque id。 |
| `project_id` | `agent_id` | 工作域 filter；不是 assistant_agent 多 Agent 的 agent。缺失时使用 `global` 工作域。 |
| `session_id` | `run_id` | 当前会话 filter，只用于 session layer。 |
| `tenant_id` | none | 不作为 Mem0 字段传递；参与 `user_id`、`agent_id`、`run_id` 哈希种子，保证跨 tenant 不碰撞。 |

Mem0 的身份过滤只有 `user_id / agent_id / run_id` 这三层。
`memory_id`、`project_memory_id`、`metadata` 以及 ledger 中的 engine mapping
都是治理、溯源、删除和导出辅助数据，不是 Mem0 identity scope id。

Mem0 scope 规则保持不变：`scope="session"` 使用
`user_id + agent_id + run_id`；`scope in {"project", "task", "video",
"product"}` 使用 `user_id + agent_id` 并故意省略 `run_id`；
`scope="user_profile"` 只使用 `user_id`，可在同一 tenant-bound user 下跨
projects/sessions 召回。Delete/list/export 仍使用 ledger 从
`project_memory_id` 映射到 engine id，因此跨 session recall 不会让删除语义变模糊。

Mem0 recall has a bounded read-after-write consistency wait. If a successful
Mem0 retain was just recorded for the same governed identity and memory scope,
and the first recall returns an empty result, `FrameworkMemoryStore` polls the
same governed recall request for a short window before returning empty. This handles Mem0 /
Qdrant indexing visibility after cold start without changing write policy,
identity binding, audit, fallback, or prompt-context governance. Hindsight and
ordinary recall failures do not use this wait path.

If Mem0 accepted the retain request but the engine still cannot make it
queryable inside that bounded window, `FrameworkMemoryStore` may use an
in-process recent-retain item for the same governed identity for a short
read-your-write window. This buffer is not durable truth, is not written to the
governance ledger or v2 fallback, is removed on delete/clear, and exists only
to mask Mem0/Qdrant insertion visibility lag immediately after a governed
retain. The cache key is scope-aware: session entries include `run_id`,
project/task entries omit `run_id`, and user-profile entries omit both
`agent_id` and `run_id`.

Mem0 retain uses `infer=false`: memory selection, permissions, and write
semantics remain owned by `MemoryManager` and policy; Mem0 is the framework
execution unit for storage and vector recall, not the authority that decides
what the assistant may remember.

When `FrameworkMemoryStore.framework_managed_algorithms` is active, `MemoryManager` skips built-in duplicate/conflict resolution and local `user_profile` projection. This hands extraction, update integration, ranking, and profile/mental-model algorithms to the selected framework while retaining project governance, confirmation, audit, identity, safety, and context-budget boundaries. New investment should prefer hardening the Mem0 framework path over expanding local v2 memory-intelligence algorithms, unless the work is required for governance, rollback, tests, or offline defaults.

### Framework bake-off gate

Hindsight `0.8.4` and Mem0 OSS `2.0.11` are scored from measured inputs by `memory/framework/bakeoff.py` and `scripts/run_memory_framework_bakeoff.py`. The fixed score is quality 45, governance 25, and operations 30. Cross-user leakage must be zero; export/delete/clear, runtime governance, offline defaults, restart recovery, and no-silent-loss must pass; total score must be at least 75 and quality at least 35. A difference of at most three points uses operations score, p95 recall, then RSS as tie-breakers. If neither passes, v2 remains the recommendation. No runtime adapter is removed until a real pilot/provider-smoke bake-off report establishes a winner.

Measured inputs are collected by `scripts/collect_memory_framework_bakeoff.py`
from the fixed 50-case synthetic corpus in
`memory/framework/collector.py`. The smoke subset and the full corpus execute
retain, recall, identity isolation, CRUD, export/delete/clear, confirmation,
restart, and durable-outbox recovery through an internal-only probe registered
with `ToolExecutor`. The probe delegates lifecycle behavior to `MemoryManager`;
`MemoryManager.retry_pending_writes(...)` is the governed recovery entry for a
lifecycle-owner store outbox. Direct calls are limited to sidecar health,
adapter history, and Docker resource/lifecycle observations.

The collector requires `pilot` or `provider_smoke`, one shell-only
`MEMORY_BAKEOFF_API_KEY`, and fixed Alibaba Cloud Model Studio configuration:
`https://dashscope.aliyuncs.com/compatible-mode/v1`, `qwen-plus`, and
`text-embedding-v4`. The CLI maps the same key to chat and embedding container
variables without writing it. Each invocation removes the dedicated
`assistant-agent-memory-bakeoff` Compose volumes before startup and never
opens, migrates, or modifies the v2 database. Its governance ledger is a
temporary runtime file; successful evidence contains only anonymous case and
memory IDs, stable error codes, latencies, probe outcomes, resource statistics,
and aggregate metrics under the ignored evidence directory. Smoke hard-gate
failure exits without metrics or JSON evidence.

Operational examples for local-only SQLite, `dual_core`, legacy
`hybrid_remote`, and `remote_service` configurations live in
`docs/development/memory-dual-core-operator-runbook.md`.

Relative JSONL and SQLite paths resolve from the repository root. JSONL and SQLite are still local-first storage, not real external providers. Additional PostgreSQL, vector DB, or external memory service adapters must sit behind `MemoryStore` and `MemoryManager`.

`memory/factory.py` 是进程内可替换记忆实现的插件入口，提供
`register_memory_store_backend(name, factory, replace=False)`、
`unregister_memory_store_backend(name)` 和 `list_memory_store_backends()`。
注册的 factory 会收到 `MemoryStoreBackendContext`，其中包含
`ProviderConfig`、仓库相对路径解析、SQLite 默认路径映射，以及组合内置
local store 的 helper。自定义 factory 必须返回满足 `MemoryStore` 契约的
对象，且不得绕过 `MemoryManager`、read/write policy、identity filtering、
confirmation、audit、prompt-safety 或 delete/export 边界。内置 backend 名称
（`memory`、`jsonl`、`sqlite`、`dual_core`、`hybrid_remote`、
`remote_service` 和 `framework`）只有在 `replace=True` 时才允许替换；
unregister 被替换的内置 backend 会恢复默认 factory。

自定义 backend 的注册仍由启动代码显式完成。允许的环境变量形态是：

```bash
MULTIMODAL_AGENT_MEMORY_PLUGIN_ENABLED=true
MULTIMODAL_AGENT_MEMORY_BACKEND=my_memory
```

`ProviderConfig.from_env(...)` 只负责在显式开关开启时保留合法 custom backend
名称。启动代码必须在 `create_memory_store(...)` 或 `AgentGraphRuntime(...)`
之前调用 `register_memory_store_backend("my_memory", factory)`；如果配置选择了
`my_memory` 但进程内没有注册，`create_memory_store(...)` 必须失败并抛出
`ValueError("unregistered memory backend: my_memory")`。本项目不做 pip entry
point 自动发现，也不会因为该开关改变默认离线行为。

`dual_core` / `hybrid_remote` are retrieval augmentation, not full memory-service
replacements. `HybridMemoryStore.search(...)` merges local search results with
safe remote query results from the external Memory Server. Local results remain
first, remote failures are returned as recoverable `MemorySearchResult.errors`,
and the agent run must still proceed with local results when the remote service
is unavailable. `HybridMemoryStore.save(...)`, get/list/delete/hard-delete,
confirmation, profile, audit, export, retention, and user-data lifecycle paths
delegate to the configured local core until the external service exposes
equivalent governed APIs.

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
returns `provider_unconfigured` until `dual_core` / `hybrid_remote` and a
Memory Server base URL are explicitly configured.

`remote_service` is the separate lifecycle-owner mode. The project-side
`RemoteServiceMemoryStore` wraps an `ExternalMemoryServiceAdapter` contract for
`search`, `save_explicit`, `record_candidate`, `confirm`, `reject`, `delete`,
`export`, `audit`, and `health`. The default factory still wires an unavailable
adapter that returns recoverable errors instead of silently writing locally.
When `MULTIMODAL_AGENT_MEMORY_REMOTE_SERVICE_ADAPTER=http`,
`MULTIMODAL_AGENT_MEMORY_BACKEND=remote_service`,
`MULTIMODAL_AGENT_MEMORY_REMOTE_ENABLED=true`, and `MEMORY_SERVER_BASE_URL` are
all configured, the factory builds `HttpRemoteMemoryServiceAdapter`. That HTTP
adapter is the project-side lifecycle integration point; it posts trusted
request/user identity in the body for save, search, delete, export, audit,
candidate, and confirmation operations. Any remote payload must still be
converted into internal safe `MemoryItem` / `MemorySearchResult` objects,
runtime identity must override remote-supplied identity, and unsafe
raw/base64/provider payloads must be rejected or dropped before prompt
injection or trace summaries.

Standard `MemoryStore` backends implement the confirmation workflow methods: `save_confirmation(...)`, `get_confirmation(...)`, `list_confirmations(...)`, and `delete_confirmation(...)`. InMemory keeps confirmation state in process memory. JSONL stores redacted pending/resolved confirmations in a sidecar file next to the memory JSONL file, for example `long_term_memories.confirmations.jsonl`. SQLite stores them in `memory_confirmations`, introduced in schema v3.

SQLite schema v4 adds the local FTS5 `memory_items_fts` candidate index. Save, update, soft delete, hard delete, session delete, user clear, migration backfill, and index rebuild keep it synchronized in the same database transaction as the canonical `memory_items` row. FTS content is an index, not durable truth; `memory_items` remains authoritative. The index stores deterministic local text fragments, including bounded Chinese 2-4 character n-grams, and candidate queries are constrained by user and memory type before normal service filtering.

`SQLiteMemoryStore` also exposes local operator helpers for `backup_to(...)`, `restore_backup(...)`, `integrity_check()`, and `rebuild_indexes()`. These helpers cover `memory_items`, `memory_audit_events`, `memory_confirmations`, and reconstruction of `memory_items_fts`. Operational steps and rollback guidance live in `docs/development/memory-sqlite-operator-runbook.md`.

SQLite durability defaults remain production-oriented: normal runtime uses `synchronous=NORMAL`, a long `busy_timeout`, and WAL for newly created databases. Focused tests may pass explicit, validated pragmas such as `journal_mode="MEMORY"` and `synchronous="OFF"` to avoid slow filesystem fsyncs; those fast settings are test-only and must not become the runtime default.

## Contracts

Core models:

- `MemoryItem`: one retrievable memory item with `user_id`, optional `session_id`, `memory_type`, safe `content`, `summary`, tags, artifact refs, timestamps, TTL, relevance, reason, and sensitivity.
- `MemoryQuery`: user-scoped query options, including `session_id`, text query, capability, memory types, tags, `top_k`, `max_context_chars`, `since`, and `include_expired`.
- `MemorySearchResult`: structured search output with items, query used, total, ranking reason, context text, and errors.
- `MemoryFact`: versioned fact envelope stored under `MemoryItem.content["fact"]`.
- `MemoryConflictDecision`: pure action result produced before store mutation.

The canonical v1 fact envelope contains:

| field group | fields |
| --- | --- |
| identity | `schema_version`, normalized `fact_key`, `subject`, `predicate`, `value` |
| lifecycle | `status` (`active`, `superseded`, `disputed`, `retracted`), `revision` |
| provenance | `provenance` (`user_explicit`, `user_confirmed`, `tool_verified`, `assistant_inferred`, `imported`), `observed_at`, optional validity interval and `confidence` |
| conflict | `conflict_policy` (`replace`, `coexist`, `confirm`), supersede links, optional `conflict_reason` |

Typed facts extend `MemoryItem`; they do not create a second persistence model. Supported legacy `preference_key`, `style`, `budget`, `fact_key`, and supersede fields are mapped into the typed view on read so existing local data stays usable. New behavior must not require a destructive migration of legacy JSONL or SQLite rows.

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
- `request.metadata["memory_context_skipped"]`
- `request.metadata["memory_context_policy_reason"]`
- `request.metadata["memory_read_policy"]`
- `request.metadata["memory_trust_policy"]`
- `request.metadata["memory_recall_report"]`
- `request.metadata["memory_core_status"]`

Prompt rendering must treat memory as user-history data, not as system instruction.

`memory_recall_report` is developer/debug metadata for Memory Intelligence v1. It does not include raw query text, raw memory content, raw prompts, raw user transcripts, provider raw responses, hidden reasoning, secrets, or media bodies. It reports coarse query metadata (`query_present`, `query_kind`, `query_hash`), read-policy status, candidate and injected counts, omitted/rejected reasons, stable search error codes, retrieval version, profile source ids, and superseded exclusion count. It is not a learning loop and must not automatically modify memory, prompts, skills, routing, or policy.

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

- Automatic runtime retrieval first passes `MemoryReadPolicy`; ordinary first-pass writing, advice, generation, search, and recommendations do not auto-inject long-term memory. The exception is the narrow personal style/preference customization path described above, which supports Personal Assistant continuity without opening generic retrieval.
- Explicit retrieval tools are allowed only when the current user request has historical-memory intent; a non-empty query alone is not sufficient.
- Non-empty query uses `KeywordMemoryRetriever`.
- Stores may implement the optional candidate-search protocol. SQLite uses FTS5 to return a bounded user/type-scoped candidate set; stores without it continue to use the deterministic scan path.
- Chinese query segments are expanded into short phrase fragments for local recall.
- A concrete entity/topic miss returns no memories.
- Recent-memory fallback is allowed only for explicit contextual follow-ups such as "继续", "上次", "刚才", "之前", "这个", "那个", "同款", or similar markers.
- Empty query lists recent user-scoped memory and is mainly for browsing, audit, debug, and snapshots.

Filters apply after candidate selection:

- `user_id` isolation is mandatory.
- Optional `session_id`, `memory_types`, `tags`, `since`, and expiration filters apply.
- Expired memories are excluded unless `include_expired=True`.
- Only `active` facts are eligible for normal retrieval and context injection. `superseded` facts may be included only by debug/read-only callers using `MemoryQuery.include_superseded=True`; `disputed` and `retracted` facts remain excluded. Legacy `content["superseded_by_memory_id"]` maps to `superseded`. The current public debug route is the memory snapshot API, and Agent-callable memory tools do not expose this flag.
- Active recall writes `memory_recall_report` metadata with counts and ids only. Raw query text is represented by coarse `query_kind` plus `query_hash`; the report must not contain the query itself or memory summaries/content.

Candidate search never bypasses identity, scope, expiration, lifecycle, sensitivity, or read-policy filtering. Final ranking combines local text relevance, exact structured fact match, capability/type priority, artifact-ref signal, and recency. Exact normalized `fact_key`, predicate, or value matches receive a deterministic boost; artifact references remain a tie-breaker. Capability-specific priorities currently exist for image generation, product search, render 3D, and direct chat. Ranking metadata uses backend-neutral wording because the same final ranking applies to scan and FTS candidates.

## Retrieval Eval

Memory retrieval quality is measured before adding embedding or vector dependencies.

Current local eval boundary:

- `src/assistant_agent/memory/retrieval_eval.py` runs deterministic `InMemoryStore` and temporary `SQLiteMemoryStore` cases through the same `MemoryManager` retrieval and context-injection boundary.
- `scripts/run_evals.py --suite memory` includes the retrieval eval cases from `tests/evals/eval_cases.json`.
- Metrics include Recall@k, MRR, false-positive rate, correct-empty rate, cross-user leakage rate, sensitive/expired injection rate, and token budget compliance, with an additional `by_backend` split for memory and SQLite cases.
- Coverage includes black-bag recall, Chinese phrase recall, color/budget preference recall, task/product resume, unrelated empty recall, cross-user isolation, expired exclusion, sensitive non-injection, token budget compliance, superseded/disputed/retracted exclusion, conflict confirmation, and explicitly coexisting same-slot facts.
- Memory intelligence contracts live in `tests/scopes/memory/test_memory_manager.py`,
  `tests/critical/test_memory_read_policy.py`, `tests/critical/test_memory_write_policy.py`,
  and `tests/scopes/memory/test_memory_retrieval_eval.py`; they verify candidate audit-only
  behavior, profile memory, deterministic supersede, governed recall/write, and local metrics.

## Write Quality Eval

The useful Hermes-style lesson for this project is a measurable feedback loop
around memory quality, not immediate RL training. The current implementation
keeps that loop offline and deterministic:

- `src/assistant_agent/memory/quality_eval.py` evaluates explicit saves and
  promotion candidates through the existing `MemoryWritePolicy`.
- `scripts/run_evals.py --suite memory_quality` runs fixed cases from
  `tests/evals/eval_cases.json`.
- `scripts/smoke_memory_dual_core.py --offline-only` includes the
  `memory_quality` suite in the broader dual-core operator acceptance smoke.
- Metrics include action accuracy, write precision/recall, reject recall,
  confirmation recall, secret rejection rate, and false-write rate.
- Eval output includes prompt-safe feedback fields: action, allowed,
  destination, confirmation requirement, and sensitivity. It must not include
  raw user text, secrets, provider payloads, raw media, or external service
  responses.

This eval suite may inform future policy changes or curated training data, but
it does not mutate `MemoryWritePolicy`, prompts, routes, skills, or stores at
runtime. Any later RL or preference-optimization work must treat these evals as
offline evidence and must still preserve explicit write policy, identity, audit,
and redaction boundaries.

## Writes

Explicit saves:

- Flow through `memory_save` or `MemoryManager.save_explicit(...)`.
- Are evaluated by `MemoryWritePolicy.evaluate_explicit_save(...)` before any item is built.
- Return a `MemoryWriteDecision` with `allowed`, `destination`, `reason`, `require_user_confirmation`, `sensitivity`, `ttl_days`, and `redacted_payload`.
- If `allowed=True`, the durable `MemoryItem` is built through `build_explicit_memory_item(...)` and still passes `MemoryItem` payload validation before storage.
- If `require_user_confirmation=True`, the write creates a `MemoryPendingConfirmation` with only redacted summary and safe content preview. Standard stores persist or retain these confirmations through the `MemoryStore` confirmation methods: SQLite uses `memory_confirmations`, JSONL uses the confirmation sidecar, and InMemory keeps process-local state. No durable memory item is stored until the user confirms it through the confirmation API/service path.
- Confirming a pending explicit memory re-runs the normal explicit-memory builder on the redacted payload and records both `memory_explicit_saved` and `memory_confirmation_decided` audit events. For a `fact_conflict` confirmation, `MemoryManager` also recomputes the current conflict decision against durable state and marks the candidate fact provenance as `user_confirmed` before the governed mutation. This prevents a stale pending decision from overwriting facts added after the confirmation was created.
- Rejecting a pending explicit memory records `memory_confirmation_decided` and does not write a memory item.
- Require non-empty text or summary.
- Infer memory type from explicit text/content. Stable preferences become `preference`; product-like content becomes `product`; otherwise default is `task`.
- Do not save raw user text unless `MemoryWritePolicy.auto_save_raw_user_text=True`.
- Reject API keys, tokens, bearer credentials, raw provider payloads, and base64/raw media even when the user explicitly asks to remember them.
- Merge duplicates by normalized summary and memory type.
- Parse a structured candidate fact when `content["fact"]` or supported legacy fact fields are present, then run `MemoryConflictResolver` against identity-visible active facts in the same governance scope.
- Use deterministic actions: append when no slot exists, merge equal values, coexist only when explicitly declared, supersede for safe preference replacement or a user-confirmed resolution, and otherwise create a conflict confirmation without mutating durable fact state.
- Never trust a generic model-declared `replace` as sufficient authority. Automatic replacement is limited to typed preference predicates; other differing values require confirmation unless the confirmation service marks the candidate `user_confirmed`.
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

The profile is a rebuildable projection, not fact authority. Only active, identity-visible, unexpired source facts contribute. Superseded, disputed, and retracted facts do not enter the active profile or normal context. Multiple active values with the same `fact_key` are reported as unresolved conflicts unless every participating fact explicitly declares `coexist`.

Explicit preference memories may carry a deterministic `content["preference_key"]`, such as `style` or `budget`. When a new explicit preference uses the same key and governance scope as an older active preference with a different summary, `MemoryManager` marks the older memory with `content["superseded_by_memory_id"]` and marks the newer memory with `content["supersedes_memory_ids"]`. This is a deterministic conflict/supersedes chain, not semantic inference. The first-pass rules only use explicit `preference_key`, known structured fields such as `style` and `budget`, and a small budget-summary fallback.

`MemoryManager.rebuild_user_profile_for_identity(...)` can check or repair the compact profile from current source memories. Source memories are identity-visible, unexpired, unscoped `preference`, `product`, and `task` items; tenant/project-scoped items are excluded until scoped profile storage is designed. Non-active source memories are excluded from the active profile and reported through lifecycle ids and `profile_conflicts`. Conflict reports include stable `fact_key` and disputed item ids. The repair result reports missing, stale, orphaned, unresolved-conflict, and out-of-sync profile state. Repair can create, update, delete, or no-op the `user_profile` item and records a prompt-safe `memory_profile_repaired` audit event when invoked through the repair path.

## Framework Reuse Boundary

No third-party memory framework is a runtime dependency of the built-in local core. The implemented `framework` mode is a separate, explicit lifecycle-owner path: Hindsight or Mem0 runs in an isolated pinned sidecar and is reached only through `MemoryManager -> FrameworkMemoryStore -> MemoryEngineAdapter`. Project identity, read/write policy, confirmation, prompt safety, governance ledger/outbox, audit, context budget, tool governance, and rollback remain owned by `assistant_agent`; the framework must not become Agent runtime authority or silently replace SQLite/JSONL.

Selection is evidence-gated rather than implied by adapter availability. Run the fixed offline/provider-smoke comparison in `docs/development/memory-framework-bakeoff-runbook.md`; until a measured report passes every hard gate and names a winner, built-in Memory Intelligence v2 remains the recommendation and framework mode remains opt-in. Other frameworks still require a separate adapter proposal, dependency approval, the same governed eval cases, and a rollback path. The local v2 typed-fact/conflict/FTS implementation itself adds no framework package and performs no network call.

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

`MemoryManager` records prompt-safe lifecycle events: context load, explicit
save/reject, confirmation create/decide, promotion decision, soft delete, hard
delete, session delete, user clear, remote query degradation, and remote
lifecycle failure. Dual-core retrieval failures emit `memory_remote_degraded`
when the external Memory Server search fails and the run continues with local
results. `remote_service` search/write failures emit
`memory_remote_lifecycle_failed` because the external service owns that
lifecycle path and no local lifecycle fallback is allowed. `MemoryAuditService`
adds export and retention-sweep events, and derives `MemoryMetricsReport`
counters from the same event stream, including `memory.remote.degraded.count`
and `memory.remote.lifecycle_failed.count`. In-memory and JSONL paths keep a
bounded in-process event list. SQLite schema v2 persists events in
`memory_audit_events`, with common filter fields split into columns and the full
redacted event saved as JSON payload, so events survive runtime restarts for the
local SQLite backend. SQLite schema v3 persists redacted pending/resolved
confirmations in `memory_confirmations`, and schema v4 adds the rebuildable FTS5
candidate index; JSONL persists the same confirmation
payload shape in its sidecar file. Production-grade external metrics export,
backup packaging, and full rollback/rebuild runbooks remain future work. Event
metadata must stay redacted and must not include remote URLs, raw exception
messages, raw memory content, raw tool/provider payloads, base64/media bodies,
or secrets.

Memory snapshot `storage.core_status` and memory metrics `core_status` expose
the same dual-core status contract used in request debug metadata. Metrics
counters remain derived from audit events; `core_status` is runtime topology
metadata, not a counter and not a remote health probe.

`DELETE /beta/users/{user_id}/data` clears memory through `runtime.memory_manager.clear_user(user_id)` as part of broader user-data deletion.

## Design Rules

- Read this document before designing or changing local/project-side memory service behavior.
- Read `docs/memory_server_api_spec.md` before changing the external Memory Service HTTP contract or remote adapter compatibility.
- Keep Agent/API/MCP code behind `MemoryManager`, `MemoryAuditService`, `MemorySnapshotService`, `ToolExecutor`, or memory tools.
- Do not let assistant nodes or API routes directly instantiate or query concrete stores.
- Keep memory tools thin. Tool code may adapt tool input/output, but service behavior belongs in `MemoryManager`, `memory/`, or `services/memory_*`.
- Do not bypass `MemoryWritePolicy` or `MemoryItem` validation when writing memory.
- Do not add embedding/vector/external memory by changing prompt builders or agent nodes directly; add an adapter behind `MemoryStore`/retrieval and keep deterministic local behavior for tests.
- Keep the built-in local memory core usable as a first-class service path; do not make normal local/offline memory depend on external Memory Server availability.
- Keep default behavior mock/local/offline. A memory backend must not become a network provider merely because credentials exist.
- When memory context rendering, conversation context, context budget, or prompt injection handling changes, also read `docs/CONTEXT_ENGINEERING_STATUS.md`.
- Update this file, `AGENTS.md`, relevant specialty skills, and affected tests when the architecture changes. Keep `README.md` as concise human navigation.

## Validation

Focused validation for memory changes:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/scopes/memory/test_memory_manager.py tests/scopes/memory/test_memory_retrieval_strategy.py tests/scopes/memory/test_memory_store_boundary.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/critical/test_memory_write_policy.py tests/critical/test_memory_privacy_redaction.py tests/scopes/memory/test_memory_lifecycle.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/scopes/memory/test_memory_audit_api.py tests/scopes/memory/test_memory_snapshot_api.py tests/scopes/memory/test_memory_runtime_integration.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/critical/test_memory_tool_boundary.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/scopes/memory/test_memory_retrieval_eval.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/scopes/memory/test_memory_fact_contract.py tests/scopes/memory/test_memory_conflict_resolver.py tests/scopes/memory/test_memory_manager_fact_conflicts.py tests/scopes/memory/test_memory_fact_status.py tests/scopes/memory/test_memory_retrieval_ranking.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/scopes/memory/test_memory_framework_adapters.py tests/scopes/memory/test_framework_memory_store.py tests/scopes/memory/test_memory_framework_config.py tests/scopes/memory/test_memory_framework_bakeoff.py tests/scopes/memory/test_memory_framework_bakeoff_cli.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py --suite memory
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py --suite memory_quality
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/smoke_memory_dual_core.py --offline-only
```

For broad behavior changes, run the full offline suite:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_demo_flows.py
```
