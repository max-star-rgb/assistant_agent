# Memory SQLite Operator Runbook

This runbook covers local SQLite operations for the memory kernel. It applies when `MULTIMODAL_AGENT_MEMORY_BACKEND=sqlite` or `ProviderConfig(memory_backend="sqlite")` is used.

Reference docs:

- `docs/memory-service-architecture.md`

## Scope

SQLite memory backup and restore covers:

- `memory_items`
- `memory_audit_events`
- `memory_confirmations`
- `memory_schema_version`
- SQLite indexes for memory retrieval, audit-event queries, and confirmation queues

It does not cover external object storage, cloud backup policies, PostgreSQL, vector indexes, or real provider artifacts. Raw provider responses, base64/media bodies, API keys, and secrets must not be placed in memory backups.

Runtime durability defaults are unchanged: normal SQLite stores use `synchronous=NORMAL`, a long `busy_timeout`, and WAL for newly created databases. Focused tests may explicitly pass validated fast pragmas such as `journal_mode="MEMORY"` and `synchronous="OFF"` to avoid local temp-filesystem fsync delays; do not use those fast settings for operator backup/restore runs.

## Default Paths

Default SQLite memory path:

```text
.local/memory/long_term_memories.sqlite3
```

Relevant environment variables:

```text
MULTIMODAL_AGENT_MEMORY_BACKEND=sqlite
MULTIMODAL_AGENT_MEMORY_PATH=.local/memory/long_term_memories.sqlite3
```

Use the project Python:

```bash
PY=/home/lenovo1/miniconda3/envs/hello_agent/bin/python
```

## Preflight

Check the environment:

```bash
$PY scripts/check_env.py
```

Verify SQLite integrity:

```bash
$PY - <<'PY'
from pathlib import Path
from assistant_agent.memory.sqlite_store import SQLiteMemoryStore

path = Path(".local/memory/long_term_memories.sqlite3")
print(SQLiteMemoryStore(path).integrity_check())
PY
```

Expected result:

```text
['ok']
```

## Backup

Create a consistent local backup:

```bash
$PY - <<'PY'
from datetime import datetime
from pathlib import Path
from assistant_agent.memory.sqlite_store import SQLiteMemoryStore

source = Path(".local/memory/long_term_memories.sqlite3")
timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = Path(".local/memory/backups") / f"long_term_memories-{timestamp}.sqlite3"

SQLiteMemoryStore(source).backup_to(backup)
print(backup)
PY
```

The backup uses SQLite's backup API and includes durable memories, audit events, and pending/resolved memory confirmations.

If the backup path already exists, the operation fails unless `overwrite=True` is passed. Avoid overwriting backups during incident response.

## Restore

Stop the local server or any process that may write to the target database before restore.

Create a safety backup of the current target first:

```bash
$PY - <<'PY'
from datetime import datetime
from pathlib import Path
from assistant_agent.memory.sqlite_store import SQLiteMemoryStore

target = Path(".local/memory/long_term_memories.sqlite3")
timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
safety = Path(".local/memory/backups") / f"pre-restore-{timestamp}.sqlite3"

SQLiteMemoryStore(target).backup_to(safety)
print(safety)
PY
```

Restore from a selected backup:

```bash
$PY - <<'PY'
from pathlib import Path
from assistant_agent.memory.sqlite_store import SQLiteMemoryStore

backup = Path(".local/memory/backups/long_term_memories-YYYYMMDD-HHMMSS.sqlite3")
target = Path(".local/memory/long_term_memories.sqlite3")

SQLiteMemoryStore.restore_backup(backup, target, overwrite=True)
print(SQLiteMemoryStore(target).integrity_check())
PY
```

`restore_backup(...)` validates source integrity and schema support before replacing the target. It writes a staging database first, so a failed restore should leave the current target unchanged.

## Rebuild Indexes

Rebuild local indexes after manual repair or suspected index damage:

```bash
$PY - <<'PY'
from pathlib import Path
from assistant_agent.memory.sqlite_store import SQLiteMemoryStore

path = Path(".local/memory/long_term_memories.sqlite3")
store = SQLiteMemoryStore(path)
store.rebuild_indexes()
print(store.integrity_check())
PY
```

Expected result:

```text
['ok']
```

## Migration Rollback

Before running code that may migrate SQLite memory schema:

1. Stop local writers.
2. Create a backup with `backup_to(...)`.
3. Start the new code and let `SQLiteMemoryStore` initialize.
4. Run focused memory tests or a local smoke.

If migration fails:

1. Keep the failed database file for diagnosis.
2. Restore the pre-migration backup to the target path.
3. Run `integrity_check()`.
4. Run focused memory validation.

The current store rejects newer schema versions before mutating them. Older v0/v1 stores migrate through schema v2, which adds `memory_audit_events`; v3 adds `memory_confirmations` for durable pending/resolved memory confirmation state.

## Corruption Response

If `integrity_check()` returns anything other than `['ok']` or SQLite raises `DatabaseError`:

1. Stop local writers.
2. Copy the damaged database and any `-wal` / `-shm` sidecars for diagnosis.
3. Restore the latest known-good backup.
4. Rebuild indexes if the restored database is valid but query behavior is suspect.
5. Run validation commands below.

Do not manually edit raw memory rows unless there is no valid backup and the repair is reviewed.

## Validation

Focused local validation:

```bash
$PY -m pytest tests/test_memory_store_boundary.py tests/test_memory_lifecycle.py tests/test_memory_audit_api.py tests/test_memory_runtime_integration.py
$PY scripts/run_evals.py --suite memory
```

Broader offline validation:

```bash
$PY scripts/check_env.py
$PY -m compileall -q src/assistant_agent scripts
$PY scripts/run_evals.py
```

## Limits

- Backup files are local files. Move/copy them according to the deployment's data policy.
- Restore should be done while writers are stopped.
- This runbook does not provide encryption, remote retention, or multi-tenant production backup policy.
- SQLite backup contains redacted memory payloads, audit events, and memory confirmations, but it is still user data and should be handled as sensitive local data.
