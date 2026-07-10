# Memory Dual-Core Operator Runbook

This runbook covers practical configuration for the built-in local memory core,
dual-core retrieval with an external Memory Server, and the external lifecycle
owner adapter mode. The architecture authority remains
`docs/memory-service-architecture.md`; this file is an operator checklist.

Use the project Python:

```bash
PY=/home/lenovo1/miniconda3/envs/hello_agent/bin/python
```

## Modes

### Local-Only SQLite

Use this for offline demos, local durability, and tests that should not depend
on an external Memory Server.

```bash
export MULTIMODAL_AGENT_MEMORY_BACKEND=sqlite
export MULTIMODAL_AGENT_MEMORY_PATH=.local/memory/long_term_memories.sqlite3
```

Expected behavior:

- `core_status.mode` is `local_only`.
- Durable memory writes, confirmations, audit events, export, retention, and
  profile state stay in local SQLite.
- No Memory Server request is made.

Preflight:

```bash
$PY scripts/check_env.py
$PY -m pytest tests/test_memory_runtime_integration.py::test_sqlite_memory_backend_writes_memory_file_when_auto_promotion_allowed -q
```

Optional SQLite operator procedures live in
`docs/development/memory-sqlite-operator-runbook.md`.

### Dual-Core Retrieval

Use this when local lifecycle ownership must remain available but external
Memory Server search should augment recall.

```bash
export MULTIMODAL_AGENT_MEMORY_BACKEND=dual_core
export MULTIMODAL_AGENT_MEMORY_LOCAL_BACKEND=sqlite
export MULTIMODAL_AGENT_MEMORY_PATH=.local/memory/long_term_memories.sqlite3
export MULTIMODAL_AGENT_MEMORY_REMOTE_ENABLED=true
export MEMORY_SERVER_BASE_URL=http://127.0.0.1:5200
export MEMORY_SERVER_TIMEOUT_SECONDS=2.0
export MEMORY_SERVER_QUERY_STRATEGY=vector
export MEMORY_SERVER_DIRECT_ANSWER=false
export MEMORY_SERVER_INCLUDE_MEDIA_CHUNKS=false
```

Expected behavior:

- `core_status.mode` is `dual_core`.
- `core_status.local_store` reports `SQLiteMemoryStore`.
- `core_status.external_core_configured` is `true` when the Memory Server URL is
  configured.
- `HybridMemoryStore.search(...)` merges local results with safe Memory Server
  results.
- `HybridMemoryStore.save(...)`, confirmations, profile, audit, export,
  retention, and delete still delegate to the local core.
- If the Memory Server query fails, the run proceeds with local results and
  `memory_core_status.remote_query_degraded=true` with stable error codes such
  as `memory_server_query_failed`.

Run the offline-first dual-core acceptance smoke before wiring a live external
service:

```bash
$PY scripts/smoke_memory_dual_core.py --offline-only
```

This command validates local SQLite memory lifecycle, dual-core remote query
degradation audit, `remote_service` lifecycle failure audit, and the
`memory_quality` eval suite without calling a provider or Memory Server.

Optionally include an explicitly configured Memory Server health/query check:

```bash
$PY scripts/smoke_memory_dual_core.py --memory-server-base-url "$MEMORY_SERVER_BASE_URL" --user-id u1 --query "上次早餐"
```

Smoke check the external Memory Server without starting the full assistant:

```bash
$PY scripts/smoke_memory_server.py --base-url "$MEMORY_SERVER_BASE_URL" --user-id u1 --query "上次早餐"
```

Run a local assistant server with the configured dual-core memory mode:

```bash
$PY scripts/run_server.py --provider mock --image-provider mock
```

Then inspect memory status:

```bash
curl "http://127.0.0.1:8000/memory/users/u1/snapshot?query=%E4%B8%8A%E6%AC%A1"
curl "http://127.0.0.1:8000/memory/users/u1/metrics"
```

Check for:

- `storage.core_status.mode == "dual_core"` in snapshot.
- `core_status.mode == "dual_core"` in metrics.
- `remote_query_degraded == false` when the Memory Server responds.
- `remote_query_degraded == true` plus prompt-safe `remote_error_codes` when it
  does not.
- `/memory/users/u1/events?event_type=memory_remote_degraded` returns a
  prompt-safe audit event after a degraded remote query.
- `/memory/users/u1/metrics` increments `memory.remote.degraded.count`.

`hybrid_remote` is a legacy alias for the same retrieval-augmentation shape.
Prefer `dual_core` in new docs, scripts, and operator instructions. Keep
`hybrid_remote` only for compatibility with older local setups.

### External Lifecycle Owner

Use this only when the external Memory Service is intended to own the full
memory lifecycle. The repository still wires an unavailable adapter by default.
Enable the HTTP lifecycle adapter only when the external service implements the
project-side lifecycle contract.

```bash
export MULTIMODAL_AGENT_MEMORY_BACKEND=remote_service
export MULTIMODAL_AGENT_MEMORY_REMOTE_ENABLED=true
export MULTIMODAL_AGENT_MEMORY_REMOTE_SERVICE_ADAPTER=http
export MEMORY_SERVER_BASE_URL=http://127.0.0.1:5200
```

Expected behavior:

- `core_status.mode` is `remote_service`.
- `core_status.external_lifecycle_owner` is `true`.
- `RemoteServiceMemoryStore` uses `HttpRemoteMemoryServiceAdapter` only with
  the explicit adapter setting and Memory Server base URL.
- The project does not silently fall back to local lifecycle writes.
- If the adapter setting or base URL is absent, lifecycle operations fail
  recoverably rather than writing to local JSONL or SQLite.
- Remote responses are converted back into internal `MemoryItem` /
  `MemorySearchResult` contracts; trusted runtime identity overrides
  remote-supplied user or session fields.
- `/memory/users/u1/events?event_type=memory_remote_lifecycle_failed` records
  prompt-safe lifecycle failures such as unavailable remote save/search.
- `/memory/users/u1/metrics` increments
  `memory.remote.lifecycle_failed.count`.

The HTTP lifecycle adapter is separate from dual-core `RemoteMemoryClient`
query/media ingestion. Its default project-side paths are:

- `POST /v1/memories/search`
- `POST /v1/memories`
- `POST /v1/memories/delete`
- `POST /v1/memories/export`
- `POST /v1/memory_audit/search`
- `POST /v1/memory_candidates`
- `POST /v1/memory_confirmations/confirm`
- `POST /v1/memory_confirmations/reject`
- `GET /v1/health`

Run focused boundary checks:

```bash
$PY -m pytest tests/test_memory_runtime_integration.py::test_create_remote_service_store_uses_unavailable_adapter_without_local_fallback -q
$PY -m pytest tests/test_memory_runtime_integration.py::test_create_remote_service_store_uses_http_adapter_only_when_explicitly_configured -q
$PY -m pytest tests/test_memory_server_remote_mapping.py::test_http_remote_service_adapter_posts_lifecycle_requests_and_rebinds_identity -q
```

Do not use `remote_service` as a synonym for dual-core query augmentation. Use
`dual_core` when local lifecycle ownership must remain available.

## Observability

Memory status is visible in three places:

- Snapshot: `storage.core_status`
- Metrics: `core_status`
- Runtime debug metadata: `request.metadata["memory_core_status"]`

The status object is prompt-safe. It may contain stable error codes, store class
names, and mode names. It must not contain Memory Server URLs, raw exception
messages, credentials, raw provider payloads, base64/media bodies, or memory
content.

Remote failure history is visible through audit events:

- `memory_remote_degraded`: dual-core remote query failed and local results were
  still used.
- `memory_remote_lifecycle_failed`: `remote_service` lifecycle operation failed
  without local lifecycle fallback.

These events are also summarized in metrics counters. Event metadata contains
stable operation names, store names, mode names, and error codes only.

## Backout

To back out from dual-core retrieval to local-only SQLite:

```bash
unset MULTIMODAL_AGENT_MEMORY_REMOTE_ENABLED
unset MEMORY_SERVER_BASE_URL
export MULTIMODAL_AGENT_MEMORY_BACKEND=sqlite
export MULTIMODAL_AGENT_MEMORY_PATH=.local/memory/long_term_memories.sqlite3
```

Restart the local server and verify:

```bash
$PY scripts/check_env.py
curl "http://127.0.0.1:8000/memory/users/u1/metrics"
```

Expected status after restart:

- `core_status.mode == "local_only"`
- `core_status.remote_query_enabled == false`

If local SQLite state needs backup, restore, integrity check, or index rebuild,
use `docs/development/memory-sqlite-operator-runbook.md`.

## Validation

Focused validation:

```bash
$PY scripts/smoke_memory_dual_core.py --offline-only
$PY scripts/run_evals.py --suite memory_quality
$PY -m pytest tests/test_memory_runtime_integration.py tests/test_memory_snapshot_api.py tests/test_memory_audit_api.py tests/test_memory_dual_core_runbook.py -q
$PY -m pytest tests/test_memory_lifecycle.py tests/test_memory_server_remote_mapping.py tests/test_memory_media_ingestion.py -q
```

Broader offline validation:

```bash
$PY scripts/check_env.py
$PY -m pytest -m fast -q
```
