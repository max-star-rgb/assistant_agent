# Memory Kernel Hardening Plan

Last updated: 2026-06-29

This is the development plan for turning the current local-first memory service into an auditable, isolated, rollback-capable, token-aware, and testable Memory Kernel. Future memory engineering work should use this document as the phased execution plan, while `docs/memory-service-architecture.md` remains the current architecture and boundary source.

## Position

The current memory service is a usable local boundary, not yet a production-grade long-running memory system.

The next stage is not "smarter memory." It is memory hardening:

```text
LLM proposes.
Policy disposes.
Store persists.
Context engine selects.
Audit explains.
User can delete.
```

The main risk is not that the agent forgets. The main risk is that it writes noisy, wrong, sensitive, cross-user, long-lived, hard-to-delete, or hard-to-explain memories.

## Inputs And Prior Art

Local project inputs:

- `docs/memory-service-architecture.md`: current memory service boundary.
- `docs/CONTEXT_ENGINEERING_STATUS.md`: context pack, compaction, budget, and conversation summary boundary.
- `docs/agent-communication-routing.md`: multi-agent routing and cross-agent isolation boundary.
- `docs/phase1-7/100-memory-data-model-and-store-boundary.md`: Phase 5I memory schema/store boundary.
- `docs/phase1-7/101-memory-retrieval-ranking-context.md`: Phase 5I retrieval and context builder plan.
- `docs/phase1-7/102-memory-write-policy-and-lifecycle.md`: Phase 5I write policy and lifecycle plan.
- `docs/phase1-7/105-phase5i-memory-hardening-review-checklist.md` and `docs/phase1-7/106-phase5i-memory-hardening-review.md`: historical hardening completion and remaining limits.

External design signals, used as reference only:

- OpenClaw's context engine separates ingest, assemble, compact, and after-turn lifecycle hooks, with `assemble` responsible for returning model context within a token budget.
- OpenClaw separates memory and context: memory can persist outside the current model window, while context is the bounded material sent to a model run.
- OpenClaw compaction summarizes older conversation turns into a session transcript summary and keeps recent messages; compaction changes what the model sees next, not necessarily what is promoted to long-term memory.
- Claude Code treats persistent memory files and auto memory as context, not enforced policy; hard constraints require policy/hooks rather than trusting memory text.

These references support the local rule: learn the boundary pattern, do not copy the whole platform.

## Scope

Build a Memory Kernel:

- Durable local store with migration and transaction semantics.
- Explicit write policy and promotion decisions.
- User/project/session isolation.
- Token-aware memory context selection.
- Safe lifecycle operations: soft delete, export, retention, audit, rollback/rebuild.
- Deterministic offline tests, evals, and demos.

Do not build a Memory RAG Platform in this stage.

## Target Shape

```text
AgentGraphRuntime
  -> ContextEngine
       -> MemoryRetriever
       -> MemoryContextBuilder
       -> TokenBudgetReporter
       -> CompactionPolicy

  -> ToolExecutor
       -> memory_save / memory_retrieval tool
            -> MemoryService / MemoryManager
                 -> MemoryWritePolicy
                 -> MemoryRetrievalPolicy
                 -> MemoryStore
                 -> AuditLog
                 -> LifecycleManager
```

Boundary rules:

- `MemoryService` / `MemoryManager` decides whether memory can be written, searched, deleted, exported, audited, or repaired.
- Context engineering decides what memory is injected into this run and how much budget it can consume.
- Compaction writes session summaries, not long-term memory, unless a separate candidate passes write policy.
- LLM can propose a memory candidate. It cannot bypass policy or store governance.
- Tools adapt input/output only; memory service logic stays out of `tools/memory_tool.py`.

## Non-Goals

Current stage must not:

- Add an external memory service.
- Add Vector DB as a default dependency.
- Build a complex RAG platform.
- Let LLM output directly create durable long-term memory.
- Automatically promote compaction summaries to long-term memory.
- Save raw user text by default.
- Save raw tool results, provider raw responses, base64, inline media, secrets, API keys, or tokens.
- Trust request body `user_id` as the final identity boundary.
- Cross user/session/project scopes during retrieval or delegation.
- Let Dify, MCP, A2A, gateway, or agent delegation directly operate on memory stores.

## Core Contracts To Introduce Or Harden

### RequestIdentity

Memory APIs should eventually accept identity from authenticated/request context rather than trusting path/body fields.

```python
class RequestIdentity:
    tenant_id: str | None
    user_id: str
    project_id: str | None
    session_id: str | None
    allowed_scopes: list[str]
```

Transition rule:

- Local/mock paths may continue deriving identity from `UserRequest` during P0.
- Service APIs should be shaped so swapping to auth-bound identity later does not require rewriting stores or tools.

### MemoryWriteDecision

`MemoryWritePolicy` should become a first-class decision boundary.

```python
class MemoryWriteDecision:
    allowed: bool
    destination: Literal[
        "reject",
        "session_summary",
        "task_checkpoint",
        "project_memory",
        "user_profile",
        "video_memory",
        "product_memory",
    ]
    reason: str
    require_user_confirmation: bool
    sensitivity: Literal["low", "medium", "high", "secret"]
    ttl_days: int | None
    redacted_payload: dict
```

Default policy direction:

| content | default action | durable memory | confirmation |
| --- | --- | --- | --- |
| Explicit "remember this" | candidate/write through policy | allowed for low sensitivity | high sensitivity requires confirmation |
| Stable preference | candidate | allowed after explicit intent or repeated signal | recommended |
| Task progress | task checkpoint | TTL/session/task scoped | no |
| Session summary | session summary | no | no |
| Project architecture decision | project memory candidate | allowed | explicit confirmation preferred |
| Video/product summary | domain memory candidate | allowed by product policy | depends on sensitivity |
| Raw user text | reject | no | no |
| Provider raw response | reject | no | no |
| Base64/raw media | reject | no | no |
| API key/token/secret | reject and audit | no | no |
| Temporary debug guess | reject or session only | no | no |
| Unconfirmed inference | candidate only | no direct write | yes |

### MemoryItem Evolution

The current `MemoryItem` is sufficient for local use. Engineering hardening should evolve toward these fields without breaking current tests at once:

```python
class MemoryItem:
    memory_id: str
    tenant_id: str | None
    user_id: str
    project_id: str | None
    session_id: str | None

    scope: Literal["session", "task", "project", "user_profile", "video", "product"]
    kind: Literal[
        "fact",
        "preference",
        "decision",
        "artifact_summary",
        "task_checkpoint",
        "video_summary",
        "product_interest",
    ]

    content_summary: str
    content_hash: str
    source_type: str
    source_ref: str | None

    confidence: float
    importance: float
    sensitivity: Literal["low", "medium", "high", "secret"]
    consent: Literal["explicit", "implicit", "system", "none"]

    ttl_seconds: int | None
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None

    last_accessed_at: datetime | None
    access_count: int
    version: int
    supersedes_id: str | None
    metadata: dict
```

Migration guidance:

- Do not rename current fields prematurely.
- Add compatibility helpers and migrations before changing public API responses.
- Keep `summary` and `content` compatible until all tools, evals, API snapshots, and demos use the new shape.

### MemoryContextBuilder

Current memory context uses character bounds. The target builder should be token-aware while keeping deterministic local behavior.

```python
class MemoryContextBuilder:
    def build(
        self,
        query: MemoryQuery,
        budget_tokens: int,
        scopes: list[str],
        identity: RequestIdentity,
    ) -> MemoryContextPack:
        ...
```

Output:

```python
class MemoryContextPack:
    items: list[MemorySearchResult]
    rendered_context: str
    total_tokens: int
    omitted_count: int
    rejected_reasons: list[str]
    retrieval_version: str
```

Selection rules:

- Never inject expired, deleted, unauthorized, high-sensitive, or unconfirmed memory.
- Prefer current task relevance.
- Then explicit user preferences/profile.
- Then project decisions/rules.
- Then artifact/domain summaries.
- Include source, timestamp, confidence, and reason in debug/audit views.
- Keep raw tool result and provider payload out of prompt context.

## Storage Strategy

| backend | role | status |
| --- | --- | --- |
| `InMemoryStore` | unit tests, demo, short-lived runtime | keep |
| `JsonlMemoryStore` | local/debug readable persistence | keep, do not make it production default |
| `SQLiteMemoryStore` | local engineering store with transactions, indexes, migration | P0 v1 implemented; hardening continues |
| `PostgresMemoryStore` | future multi-user managed production store | P1/P2 |
| Vector adapter | optional semantic recall | P2 only, not default |

P0 progress:

- 2026-06-29: Added `SQLiteMemoryStore` behind the existing `MemoryStore` contract, with `ProviderConfig(memory_backend="sqlite")` and factory routing. The v1 schema includes `memory_schema_version`, indexed `memory_items`, user-scoped primary key, content hash column, transactional upsert, and delete behavior that hides rows from retrieval/list/get. Store contract and runtime integration tests cover save/search/get/delete, relative paths, default sqlite path selection, and cross-runtime persistence.
- 2026-06-29: Added SQLite hardening tests for newer-schema rejection, older-schema migration hook, soft-delete hiding and restore-on-save, failed transaction rollback, concurrent store-instance writes, and stable content hash/version increments. The store enables WAL only when creating a new database to avoid repeatedly switching journal mode on every store initialization; it keeps explicit transactions, `busy_timeout`, and `synchronous=NORMAL`.
- 2026-06-29: Added `RequestIdentity` as the memory service identity boundary and threaded it through `MemoryManager`, memory tools, `MemoryAuditService`, `MemorySnapshotService`, and memory API routes. Current identity is still derived from local requests, tool context, or API path parameters; future auth-bound identity can replace that construction point without rewriting stores.
- 2026-06-29: Added optional `tenant_id`, `project_id`, and coarse `scope` fields to `MemoryItem`/`MemoryQuery`, plus service-layer filtering for retrieval, list, get, and delete. Legacy unscoped user memories remain visible for compatibility; tenant/project-scoped memories require matching identity metadata.
- 2026-06-29: Expanded `MemoryWriteDecision` into the first-class write-policy result for explicit saves and promotion candidates. The decision now carries destination, confirmation requirement, sensitivity, TTL, and redacted payload metadata. Explicit saves are evaluated through `MemoryWritePolicy.evaluate_explicit_save(...)`; raw provider payloads, base64/raw media, API keys, bearer tokens, and secret-like text are rejected before a `MemoryItem` is built. Promotion audit records now include the decision fields while still excluding candidate content.
- 2026-06-29: Added `MemoryContextBuilder` as a memory-local token-aware injection boundary. It renders the existing memory layers, estimates tokens deterministically, honors optional memory token budgets, rejects sensitive/expired items from injection, and reports injected IDs, token count, budget, omitted count, rejection reasons, and retrieval version through `memory_context_*` metadata. Global assistant context compaction still uses the existing context-engine character budget unless separately changed.
- 2026-06-29: Added an offline memory retrieval eval baseline through `multimodal_agent.memory.retrieval_eval` and `scripts/run_evals.py --suite memory`. The initial suite covers relevant retrieval, correct empty recall, cross-user isolation, expired exclusion, sensitive memory non-injection, and memory token budget compliance. Summary metrics include Recall@k, MRR, false-positive rate, correct-empty rate, cross-user leakage rate, sensitive/expired injection rate, and token budget compliance.
- 2026-06-29: Added first-pass user-scoped memory export and expired-memory retention sweep through `MemoryAuditService` and API routes. The sweep supports dry-run, soft-delete, and explicit hard-delete modes while preserving identity scoping. `InMemoryStore`, `JsonlMemoryStore`, and `SQLiteMemoryStore` expose a `hard_delete(...)` hook; production-grade audit log storage and rollback/rebuild remain future work.
- 2026-06-29: Added bounded in-process `MemoryAuditEvent` and `MemoryMetricsReport` foundation. `MemoryManager` records context load, explicit save/reject, promotion decision, soft delete, hard delete, session delete, and user-clear events; `MemoryAuditService` records export and retention-sweep events and exposes `/events` and `/metrics` API views. This is local/debug visibility only; durable audit-log storage, external metrics export, and rollback/rebuild remain future work.

SQLite P0 requirements:

- `schema_version` table. Initial v1 exists.
- Forward migrations and rollback notes. Migration hook and newer-schema rejection are tested; rollback/runbook notes still needed.
- Transaction wrapper for save/update/delete/profile upsert. Store-level save/delete rollback tests exist; profile-specific transaction tests still needed.
- Unique user-scoped content hash or dedupe key. Current primary key is `(user_id, memory_id)` with `content_hash`; dedupe-key policy remains future work.
- Indexes for `user_id`, `project_id`, `session_id`, `scope`, `kind`, `expires_at`, `deleted_at`, `content_hash`. Current schema indexes `user_id`, `session_id`, `memory_type`, `expires_at`, `deleted_at`, and `content_hash`; project/scope/kind fields wait for schema evolution.
- Soft delete support. Delete hides SQLite rows via `deleted_at`, restore-on-save is tested, and a user-scoped retention sweeper can soft-delete or explicitly hard-delete expired items.
- Backup/export path. First-pass user export API is implemented; backup/runbook packaging remains future work.
- Corruption/migration failure tests. Newer-schema rejection is covered; corrupt database and migration rollback tests still needed.

## Lifecycle And Privacy

Memory lifecycle must support:

- Soft delete.
- Hard delete or retention sweeper.
- User export.
- Session delete.
- TTL expiry.
- Profile rebuild/repair.
- Audit log for writes, rejections, deletes, migrations, and policy decisions.

Privacy rules:

- Raw provider responses and raw tool outputs stay out of memory.
- Artifact/media bodies stay behind refs.
- Secret-like content is rejected, not merely hidden.
- Memory is context, not enforcement. Enforcement belongs to validator, policy, auth, sandbox, and runtime profiles.

## Observability

Add memory metrics and trace fields before adding more intelligence.

Counters/histograms:

```text
memory.write.allowed.count
memory.write.rejected.count
memory.write.needs_confirmation.count
memory.search.count
memory.search.empty.count
memory.search.hit.count
memory.search.latency_ms
memory.context.injected_tokens
memory.context.omitted_items
memory.context.rejected_sensitive_items
memory.delete.soft.count
memory.delete.hard.count
memory.export.count
memory.ttl.expired.count
memory.ttl.swept.count
memory.profile.update.count
memory.profile.conflict.count
memory.store.error.count
memory.store.transaction.rollback.count
```

Per run trace/debug summary:

```text
memory_query
retrieved_memory_ids
injected_memory_ids
memory_tokens
rejected_memory_reasons
write_candidates
write_decisions
```

Trace output must stay redacted.

## Eval Before Embeddings

Do not add embeddings before a deterministic retrieval eval exists.

Minimum eval cases:

| query | expected behavior |
| --- | --- |
| "上次那个黑色包是什么风格？" | retrieve the relevant product/task memory |
| "我之前喜欢什么配色？" | retrieve user preference/profile memory |
| "刚才让我放到客厅的是哪个商品？" | retrieve task checkpoint or product memory |
| "我预算是多少？" | retrieve budget preference or correctly return none |
| unrelated concrete entity | return empty, not recent unrelated memory |
| expired item query | exclude expired item by default |
| cross-user query | no leakage |
| high-sensitive memory | not injected |
| tiny budget | respect memory token budget |

Metrics:

- Recall@k.
- MRR.
- False positive rate.
- Correct empty recall rate.
- Expired exclusion rate.
- Cross-user leakage rate.
- Sensitive injection rate.
- Token budget compliance.

## Phased Plan

### P0: Memory Kernel Foundation

Goal: local engineering-grade memory, still offline and deterministic.

Work:

1. Add `SQLiteMemoryStore`. Initial implementation done on 2026-06-29.
2. Add schema version and migration runner. Initial v1 schema, migration hook, and newer-schema rejection tests done on 2026-06-29; migration rollback/runbook still needed.
3. Add transaction/lock behavior and user-scoped unique dedupe key.
4. Shape `RequestIdentity` and thread it through service APIs where practical. Initial request-derived identity boundary is implemented on 2026-06-29; service-layer project/tenant/scope filtering is in place for scoped memories. Auth-bound principal integration and database-level tenant/project indexes remain future work.
5. Promote `MemoryWritePolicy` into a decision object with allow/reject/confirmation reasons. Initial `MemoryWriteDecision` fields and explicit-save/promotion-candidate evaluation are implemented on 2026-06-29; user-facing confirmation workflow remains future work.
6. Add token-aware `MemoryContextBuilder` behind existing context metadata. Initial memory-local builder and metadata reporting are implemented on 2026-06-29; broader memory evals and trace metrics remain future work.
7. Add soft delete, export, and audit log foundation. Soft-delete behavior, user export, retention sweep, in-process audit events, and derived local metrics are implemented; durable audit-log storage remains future work.
8. Keep `InMemoryStore` and `JsonlMemoryStore` as local/offline paths.
9. Add retrieval eval suite before vector work. Initial offline retrieval eval suite and summary metrics are implemented on 2026-06-29; broader corpus coverage and regression thresholds remain future work.
10. Add store corruption, migration, and concurrency tests.

Acceptance:

- Default mock/local/offline path unchanged.
- Existing pytest/eval/demo flows remain offline.
- No raw provider response, base64, raw media, API key, or secret can be saved.
- Search/delete/export remain user-scoped.
- Deleted memory is not retrieved or injected.
- Expired memory is not injected by default.
- Memory context respects configured token budget.
- SQLite path passes store contract tests.
- JSONL remains supported for debug/local compatibility.

### P1: Governance And Lifecycle

Goal: user-visible control and operator-safe lifecycle.

Work:

1. Retention sweeper.
2. Soft delete to hard delete flow.
3. User export API/CLI.
4. Profile rebuild/repair.
5. Conflict detection and supersedes chain.
6. Memory metrics.
7. Migration rollback/index rebuild/backup restore runbook.
8. Consent policy.
9. Sensitive memory confirmation flow.

Acceptance:

- User can list, inspect, export, and delete long-term memory.
- System can explain why a memory was written, rejected, injected, omitted, or deleted.
- Sweeper does not break audit.
- Profile can be rebuilt from source memory items.
- Sensitive memory cannot become durable without the configured confirmation path.

### P2: Intelligence After Baselines

Goal: improve recall only after deterministic baselines exist.

Work:

1. Optional embedding adapter.
2. Optional vector store adapter.
3. Hybrid retrieval.
4. Reranking behind a deterministic/mocked interface.
5. LLM-generated memory candidates.
6. Dreaming-like promotion job with thresholds and review.
7. Preference aging/decay.
8. Stale memory conflict resolution.

Acceptance:

- All intelligent features are opt-in.
- Local tests do not require network providers.
- Embedding/vector failure does not silently become mock success.
- Retrieval eval improves without raising false positives or leakage.
- Promotion decisions are auditable and reversible.

### P3: Multi-Agent Integration

Goal: memory remains isolated under explicit local/remote agent routing.

Work:

1. Ensure delegated tasks carry parent run, correlation, user, session, project, and budget metadata.
2. Prevent Agent A from directly reading Agent B's memory store.
3. Add memory budget accounting for delegated work.
4. Add gateway/A2A memory isolation tests.
5. Only then consider shared project memory with explicit scope and allowlist.

Acceptance:

- Default `/agent/run`, CLI, eval, and Web demo remain single-agent.
- Multi-agent memory access is scoped and auditable.
- A2A/remote agent adapters never operate stores directly.

## Implementation Rules

- Start each implementation task from this plan and `docs/memory-service-architecture.md`.
- Keep every step mock/local/offline by default.
- Add tests before replacing JSONL behavior.
- Prefer small migrations over large schema rewrites.
- Keep public API compatibility unless a migration task explicitly changes it.
- Document every new durable field, lifecycle transition, and audit event.

## Suggested Task Order

1. `SQLiteMemoryStore` contract tests.
2. SQLite schema/migration skeleton.
3. Transactional save/delete/profile upsert.
4. Soft delete and export.
5. `RequestIdentity` service API shape.
6. `MemoryWriteDecision` and candidate evaluation.
7. Token-aware `MemoryContextBuilder`.
8. Retrieval eval suite.
9. Metrics/trace summaries.
10. Retention sweeper and repair tools.

## Validation Commands

Focused:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_manager.py tests/test_memory_retrieval_strategy.py tests/test_memory_store_boundary.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_write_policy.py tests/test_memory_privacy_redaction.py tests/test_memory_lifecycle.py tests/test_memory_audit_api.py tests/test_memory_snapshot_api.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_tool_boundary.py
```

Broad offline:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_demo_flows.py
```

## References

- OpenClaw Context Engine: https://docs.openclaw.ai/concepts/context-engine
- OpenClaw Context: https://docs.openclaw.ai/concepts/context
- OpenClaw Compaction: https://docs.openclaw.ai/concepts/compaction
- OpenClaw Memory: https://docs.openclaw.ai/concepts/memory
- Claude Code Memory: https://code.claude.com/docs/en/memory
