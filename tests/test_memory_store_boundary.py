import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from multimodal_agent.memory.jsonl_store import JsonlMemoryStore
from multimodal_agent.memory.manager import MemoryConfirmationRequired, MemoryManager
from multimodal_agent.memory.sqlite_store import SCHEMA_VERSION, SQLiteMemoryStore as BaseSQLiteMemoryStore
from multimodal_agent.memory.store import InMemoryStore, MemoryStore
from multimodal_agent.schemas.identity import RequestIdentity
from multimodal_agent.schemas.memory import MemoryItem, MemoryQuery, MemorySearchResult
from multimodal_agent.schemas.memory_audit import MemoryAuditEvent, MemoryPendingConfirmation


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class SQLiteMemoryStore(BaseSQLiteMemoryStore):
    """Fast SQLite settings for store-boundary tests on slow local filesystems."""

    def __init__(self, path: Path | str = ".local/memory/long_term_memories.sqlite3", **kwargs) -> None:
        kwargs.setdefault("synchronous", "OFF")
        kwargs.setdefault("busy_timeout_ms", 1000)
        super().__init__(path, **kwargs)


def restore_sqlite_backup_for_test(
    source: Path,
    destination: Path,
    *,
    overwrite: bool = False,
) -> SQLiteMemoryStore:
    return SQLiteMemoryStore.restore_backup(
        source,
        destination,
        overwrite=overwrite,
        synchronous="OFF",
        busy_timeout_ms=1000,
    )


def make_memory(memory_id: str, user_id: str = "u1", summary: str = "用户喜欢白色运动鞋") -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        user_id=user_id,
        session_id="s1",
        memory_type="product",
        summary=summary,
        content={"name": "白色运动鞋", "output_ref": "mock://product/shoe-1"},
        tags=["shoe"],
        artifact_refs=["mock://product/shoe-1"],
        created_at=NOW,
    )


@pytest.mark.parametrize("store_backend", ["memory", "jsonl", "sqlite"])
def test_store_boundary_save_search_get_delete(store_backend: str, tmp_path) -> None:
    if store_backend == "memory":
        store: MemoryStore = InMemoryStore()
    elif store_backend == "jsonl":
        store = JsonlMemoryStore(tmp_path / "memories.jsonl")
    else:
        store = SQLiteMemoryStore(tmp_path / "memories.sqlite3")

    saved = store.save(make_memory("m1"))
    store.save(make_memory("m2", user_id="u2", summary="另一个用户喜欢白色运动鞋"))

    result = store.search(MemoryQuery(user_id="u1", query="白色运动鞋", tags=["shoe"], top_k=5))

    assert isinstance(result, MemorySearchResult)
    assert saved == store.get("u1", "m1")
    assert [item.memory_id for item in result.items] == ["m1"]
    assert result.total == 1
    assert "白色运动鞋" in result.memory_context
    assert store.delete("u1", "m1") is True
    assert store.get("u1", "m1") is None
    assert store.delete("u1", "missing") is False


def test_jsonl_store_keeps_legacy_search_call_compatible(tmp_path) -> None:
    store = JsonlMemoryStore(tmp_path / "memories.jsonl")
    store.save(make_memory("m1"))

    result = store.search(user_id="u1", query="白色运动鞋")

    assert [item.memory_id for item in result] == ["m1"]


@pytest.mark.parametrize("store_backend", ["memory", "jsonl", "sqlite"])
def test_store_boundary_confirmation_contract(store_backend: str, tmp_path) -> None:
    if store_backend == "memory":
        store: MemoryStore = InMemoryStore()
    elif store_backend == "jsonl":
        store = JsonlMemoryStore(tmp_path / "memories.jsonl")
    else:
        store = SQLiteMemoryStore(tmp_path / "memories.sqlite3")

    saved = store.save_confirmation(make_confirmation("confirmation_1"))
    store.save_confirmation(make_confirmation("confirmation_2", user_id="u2"))
    resolved = saved.model_copy(update={"status": "rejected", "decided_at": NOW})
    store.save_confirmation(resolved)

    assert store.get_confirmation("u1", "confirmation_1") == resolved
    assert store.list_confirmations(user_id="u1", include_resolved=False) == []
    assert [item.confirmation_id for item in store.list_confirmations(user_id="u1", include_resolved=True)] == [
        "confirmation_1"
    ]
    assert store.delete_confirmation("u1", "confirmation_1") is True
    assert store.delete_confirmation("u1", "confirmation_1") is False
    assert store.get_confirmation("u1", "confirmation_1") is None
    assert [item.confirmation_id for item in store.list_confirmations(user_id="u2", include_resolved=True)] == [
        "confirmation_2"
    ]


def test_jsonl_store_persists_memory_confirmations_across_instances(tmp_path) -> None:
    path = tmp_path / "memories.jsonl"
    identity = RequestIdentity.for_user(user_id="u1", session_id="s1")
    manager = MemoryManager(JsonlMemoryStore(path))

    with pytest.raises(MemoryConfirmationRequired) as raised:
        manager.save_explicit_for_identity(
            identity,
            text="记住我的项目路径是 /home/alice/private/project",
        )
    confirmation_id = raised.value.confirmation.confirmation_id

    reloaded = MemoryManager(JsonlMemoryStore(path))
    pending = reloaded.list_confirmations_for_identity(identity)
    confirmed = reloaded.confirm_memory_for_identity(identity, confirmation_id)

    assert (tmp_path / "memories.confirmations.jsonl").exists()
    assert [confirmation.confirmation_id for confirmation in pending] == [confirmation_id]
    assert confirmed is not None
    assert confirmed.status == "confirmed"

    final_store = JsonlMemoryStore(path)
    saved = final_store.get("u1", confirmed.confirmed_memory_id or "")
    resolved = final_store.get_confirmation("u1", confirmation_id)

    assert saved is not None
    assert saved.summary == "我的项目路径是 [redacted]"
    assert resolved is not None
    assert resolved.status == "confirmed"
    assert resolved.confirmed_memory_id == saved.memory_id


def test_sqlite_store_persists_across_instances(tmp_path) -> None:
    path = tmp_path / "memories.sqlite3"
    SQLiteMemoryStore(path).save(make_memory("m1"))

    result = SQLiteMemoryStore(path).search(MemoryQuery(user_id="u1", query="白色运动鞋"))

    assert [item.memory_id for item in result.items] == ["m1"]


def test_sqlite_store_delete_by_session_and_clear_user(tmp_path) -> None:
    store = SQLiteMemoryStore(tmp_path / "memories.sqlite3")
    store.save(make_memory("m1"))
    store.save(make_memory("m2", summary="用户喜欢黑色运动鞋"))
    store.save(make_memory("m3", user_id="u2"))

    assert store.delete_by_session("u1", "s1") == 2
    assert store.list_by_user("u1") == []
    assert [item.memory_id for item in store.list_by_user("u2")] == ["m3"]

    store.clear_user("u2")

    assert store.list_by_user("u2") == []


def test_sqlite_store_rejects_newer_schema_version(tmp_path) -> None:
    path = tmp_path / "memories.sqlite3"
    with _sqlite_connection(path) as connection:
        connection.execute("CREATE TABLE memory_schema_version (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO memory_schema_version(version) VALUES (?)", (SCHEMA_VERSION + 1,))

    with pytest.raises(RuntimeError, match="newer than supported"):
        SQLiteMemoryStore(path)


def test_sqlite_store_rejects_unsupported_pragma_options(tmp_path) -> None:
    with pytest.raises(ValueError, match="journal_mode"):
        BaseSQLiteMemoryStore(tmp_path / "bad_journal.sqlite3", journal_mode="WAL;DROP TABLE memory_items")
    with pytest.raises(ValueError, match="synchronous"):
        BaseSQLiteMemoryStore(tmp_path / "bad_sync.sqlite3", synchronous="NORMAL;DROP TABLE memory_items")


def test_sqlite_store_migrates_older_schema_version(tmp_path) -> None:
    path = tmp_path / "memories.sqlite3"
    with _sqlite_connection(path) as connection:
        connection.execute("CREATE TABLE memory_schema_version (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO memory_schema_version(version) VALUES (0)")

    store = SQLiteMemoryStore(path)
    store.save(make_memory("m1"))

    with _sqlite_connection(path) as connection:
        version = connection.execute("SELECT version FROM memory_schema_version LIMIT 1").fetchone()[0]

    assert version == SCHEMA_VERSION
    assert store.get("u1", "m1") is not None


def test_sqlite_store_migrates_v1_database_to_audit_log(tmp_path) -> None:
    path = tmp_path / "memories.sqlite3"
    with _sqlite_connection(path) as connection:
        connection.execute("CREATE TABLE memory_schema_version (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO memory_schema_version(version) VALUES (1)")

    store = SQLiteMemoryStore(path)
    store.save_audit_event(make_audit_event("event_v1"))

    with _sqlite_connection(path) as connection:
        version = connection.execute("SELECT version FROM memory_schema_version LIMIT 1").fetchone()[0]
        count = connection.execute("SELECT COUNT(*) FROM memory_audit_events").fetchone()[0]

    assert version == SCHEMA_VERSION
    assert count == 1


def test_sqlite_store_migrates_v2_database_to_confirmations(tmp_path) -> None:
    path = tmp_path / "memories.sqlite3"
    with _sqlite_connection(path) as connection:
        connection.execute("CREATE TABLE memory_schema_version (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO memory_schema_version(version) VALUES (2)")

    store = SQLiteMemoryStore(path)
    store.save_confirmation(make_confirmation("confirmation_v2"))

    with _sqlite_connection(path) as connection:
        version = connection.execute("SELECT version FROM memory_schema_version LIMIT 1").fetchone()[0]
        count = connection.execute("SELECT COUNT(*) FROM memory_confirmations").fetchone()[0]

    assert version == SCHEMA_VERSION
    assert count == 1


def test_sqlite_store_soft_delete_hides_and_save_restores_item(tmp_path) -> None:
    path = tmp_path / "memories.sqlite3"
    store = SQLiteMemoryStore(path)
    store.save(make_memory("m1"))

    assert store.delete("u1", "m1") is True
    assert store.get("u1", "m1") is None
    assert store.list_by_user("u1") == []
    assert store.search(MemoryQuery(user_id="u1", query="白色运动鞋")).items == []

    store.save(make_memory("m1", summary="用户喜欢白色运动鞋"))

    assert store.get("u1", "m1") is not None
    assert [item.memory_id for item in store.search(MemoryQuery(user_id="u1", query="白色运动鞋")).items] == ["m1"]
    with _sqlite_connection(path) as connection:
        deleted_at = connection.execute(
            "SELECT deleted_at FROM memory_items WHERE user_id = ? AND memory_id = ?",
            ("u1", "m1"),
        ).fetchone()[0]

    assert deleted_at is None


def test_sqlite_store_rolls_back_failed_transaction(tmp_path) -> None:
    path = tmp_path / "memories.sqlite3"
    store = SQLiteMemoryStore(path)
    with _sqlite_connection(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_memory_insert
            AFTER INSERT ON memory_items
            BEGIN
                SELECT RAISE(ABORT, 'forced rollback');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced rollback"):
        store.save(make_memory("m1"))

    with _sqlite_connection(path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM memory_items WHERE user_id = ?", ("u1",)).fetchone()[0]

    assert count == 0


def test_sqlite_store_rolls_back_failed_audit_event_transaction(tmp_path) -> None:
    path = tmp_path / "memories.sqlite3"
    store = SQLiteMemoryStore(path)
    with _sqlite_connection(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_audit_event_insert
            AFTER INSERT ON memory_audit_events
            BEGIN
                SELECT RAISE(ABORT, 'forced audit rollback');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced audit rollback"):
        store.save_audit_event(make_audit_event("event_fail"))

    with _sqlite_connection(path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM memory_audit_events WHERE user_id = ?", ("u1",)).fetchone()[0]

    assert count == 0


def test_sqlite_store_rolls_back_failed_confirmation_transaction(tmp_path) -> None:
    path = tmp_path / "memories.sqlite3"
    store = SQLiteMemoryStore(path)
    with _sqlite_connection(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_confirmation_insert
            AFTER INSERT ON memory_confirmations
            BEGIN
                SELECT RAISE(ABORT, 'forced confirmation rollback');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced confirmation rollback"):
        store.save_confirmation(make_confirmation("confirmation_fail"))

    with _sqlite_connection(path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM memory_confirmations WHERE user_id = ?", ("u1",)).fetchone()[0]

    assert count == 0


def test_sqlite_store_handles_concurrent_store_instances(tmp_path) -> None:
    path = tmp_path / "memories.sqlite3"
    SQLiteMemoryStore(path)

    def save_memory(index: int) -> None:
        store = SQLiteMemoryStore(path)
        store.save(make_memory(f"m{index}", summary=f"用户喜欢白色运动鞋 {index}"))

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(save_memory, range(12)))

    items = SQLiteMemoryStore(path).list_by_user("u1")

    assert len(items) == 12
    assert {item.memory_id for item in items} == {f"m{index}" for index in range(12)}


def test_sqlite_store_records_stable_content_hash_for_same_payload(tmp_path) -> None:
    path = tmp_path / "memories.sqlite3"
    store = SQLiteMemoryStore(path)
    store.save(make_memory("m1"))
    first_hash, first_version = _sqlite_memory_row(path, "u1", "m1")

    store.save(make_memory("m1"))
    second_hash, second_version = _sqlite_memory_row(path, "u1", "m1")

    assert first_hash
    assert second_hash == first_hash
    assert second_version == first_version + 1


def test_sqlite_store_persists_audit_events_across_instances(tmp_path) -> None:
    path = tmp_path / "memories.sqlite3"
    identity = RequestIdentity.for_user(user_id="u1", session_id="s1")
    manager = MemoryManager(SQLiteMemoryStore(path))

    saved = manager.save_explicit_for_identity(
        identity,
        text="记住我喜欢白色运动鞋",
        content={"summary": "用户喜欢白色运动鞋。", "style": "白色运动鞋"},
    )

    events = MemoryManager(SQLiteMemoryStore(path)).list_audit_events_for_identity(identity)

    assert any(
        event.event_type == "memory_explicit_saved" and event.memory_id == saved.memory_id
        for event in events
    )


def test_sqlite_store_persists_memory_confirmations_across_instances(tmp_path) -> None:
    path = tmp_path / "memories.sqlite3"
    identity = RequestIdentity.for_user(user_id="u1", session_id="s1")
    manager = MemoryManager(SQLiteMemoryStore(path))

    with pytest.raises(MemoryConfirmationRequired) as raised:
        manager.save_explicit_for_identity(
            identity,
            text="记住我的项目路径是 /home/alice/private/project",
        )
    confirmation_id = raised.value.confirmation.confirmation_id

    reloaded = MemoryManager(SQLiteMemoryStore(path))
    pending = reloaded.list_confirmations_for_identity(identity)
    confirmed = reloaded.confirm_memory_for_identity(identity, confirmation_id)

    assert [confirmation.confirmation_id for confirmation in pending] == [confirmation_id]
    assert confirmed is not None
    assert confirmed.status == "confirmed"

    final_store = SQLiteMemoryStore(path)
    saved = final_store.get("u1", confirmed.confirmed_memory_id or "")
    resolved = final_store.get_confirmation("u1", confirmation_id)

    assert saved is not None
    assert saved.summary == "我的项目路径是 [redacted]"
    assert resolved is not None
    assert resolved.status == "confirmed"
    assert resolved.confirmed_memory_id == saved.memory_id


def test_sqlite_store_backup_restore_includes_memories_audit_and_rebuilds_indexes(tmp_path) -> None:
    path = tmp_path / "memories.sqlite3"
    backup_path = tmp_path / "backup" / "memories-backup.sqlite3"
    restored_path = tmp_path / "restored.sqlite3"
    identity = RequestIdentity.for_user(user_id="u1", session_id="s1")
    store = SQLiteMemoryStore(path)
    manager = MemoryManager(store)

    saved = manager.save_explicit_for_identity(
        identity,
        text="记住我喜欢白色运动鞋",
        content={"summary": "用户喜欢白色运动鞋。", "style": "白色运动鞋"},
    )
    with pytest.raises(MemoryConfirmationRequired) as raised:
        manager.save_explicit_for_identity(
            identity,
            text="记住我的项目路径是 /home/alice/private/project",
        )
    confirmation_id = raised.value.confirmation.confirmation_id
    store.backup_to(backup_path)
    restored = restore_sqlite_backup_for_test(backup_path, restored_path)

    assert restored.integrity_check() == ["ok"]
    assert [item.memory_id for item in restored.list_by_user("u1") if item.memory_id == saved.memory_id] == [
        saved.memory_id
    ]
    assert any(
        event.event_type == "memory_explicit_saved" and event.memory_id == saved.memory_id
        for event in restored.list_audit_events(user_id="u1")
    )
    assert [confirmation.confirmation_id for confirmation in restored.list_confirmations(user_id="u1")] == [
        confirmation_id
    ]

    with _sqlite_connection(restored_path) as connection:
        connection.execute("DROP INDEX IF EXISTS idx_memory_items_user_created")
        connection.execute("DROP INDEX IF EXISTS idx_memory_audit_events_user_time")
        connection.execute("DROP INDEX IF EXISTS idx_memory_confirmations_user_status")
    assert "idx_memory_items_user_created" not in _sqlite_index_names(restored_path)
    assert "idx_memory_audit_events_user_time" not in _sqlite_index_names(restored_path)
    assert "idx_memory_confirmations_user_status" not in _sqlite_index_names(restored_path)

    restored.rebuild_indexes()

    assert "idx_memory_items_user_created" in _sqlite_index_names(restored_path)
    assert "idx_memory_audit_events_user_time" in _sqlite_index_names(restored_path)
    assert "idx_memory_confirmations_user_status" in _sqlite_index_names(restored_path)


def test_sqlite_store_backup_and_restore_overwrite_are_explicit(tmp_path) -> None:
    source_path = tmp_path / "source.sqlite3"
    backup_path = tmp_path / "backup.sqlite3"
    restore_path = tmp_path / "restore.sqlite3"
    source = SQLiteMemoryStore(source_path)
    source.save(make_memory("source_memory"))
    SQLiteMemoryStore(restore_path).save(make_memory("existing_memory"))
    backup_path.write_text("existing backup", encoding="utf-8")

    with pytest.raises(FileExistsError):
        source.backup_to(backup_path)
    source.backup_to(backup_path, overwrite=True)
    with pytest.raises(FileExistsError):
        restore_sqlite_backup_for_test(backup_path, restore_path)

    restored = restore_sqlite_backup_for_test(backup_path, restore_path, overwrite=True)

    assert [item.memory_id for item in restored.list_by_user("u1")] == ["source_memory"]


def test_sqlite_store_failed_restore_preserves_destination(tmp_path) -> None:
    source_path = tmp_path / "corrupt-source.sqlite3"
    destination_path = tmp_path / "destination.sqlite3"
    SQLiteMemoryStore(destination_path).save(make_memory("existing_memory"))
    source_path.write_text("not a sqlite database", encoding="utf-8")

    with pytest.raises(sqlite3.DatabaseError):
        restore_sqlite_backup_for_test(source_path, destination_path, overwrite=True)

    assert [item.memory_id for item in SQLiteMemoryStore(destination_path).list_by_user("u1")] == ["existing_memory"]


def test_sqlite_store_filters_persisted_audit_events_by_identity_scope(tmp_path) -> None:
    path = tmp_path / "memories.sqlite3"
    manager = MemoryManager(SQLiteMemoryStore(path))
    manager.record_audit_event(
        "memory_exported",
        user_id="u1",
        memory_id="global",
        summary="global event",
    )
    manager.record_audit_event(
        "memory_exported",
        user_id="u1",
        tenant_id="tenant_1",
        project_id="project_1",
        memory_id="scoped",
        summary="scoped event",
    )
    manager.record_audit_event(
        "memory_exported",
        user_id="u2",
        memory_id="other_user",
        summary="other user event",
    )
    reloaded = MemoryManager(SQLiteMemoryStore(path))

    unscoped_events = reloaded.list_audit_events_for_identity(RequestIdentity.for_user(user_id="u1"))
    scoped_events = reloaded.list_audit_events_for_identity(
        RequestIdentity.for_user(user_id="u1", tenant_id="tenant_1", project_id="project_1"),
        event_type="memory_exported",
    )

    assert {event.memory_id for event in unscoped_events} == {"global"}
    assert {event.memory_id for event in scoped_events} == {"global", "scoped"}


def test_sqlite_store_rejects_corrupt_database_file(tmp_path) -> None:
    path = tmp_path / "memories.sqlite3"
    path.write_text("not a sqlite database", encoding="utf-8")

    with pytest.raises(sqlite3.DatabaseError):
        SQLiteMemoryStore(path)


def _sqlite_memory_row(path, user_id: str, memory_id: str) -> tuple[str, int]:
    with _sqlite_connection(path) as connection:
        return connection.execute(
            "SELECT content_hash, version FROM memory_items WHERE user_id = ? AND memory_id = ?",
            (user_id, memory_id),
        ).fetchone()


def _sqlite_index_names(path: Path) -> set[str]:
    with _sqlite_connection(path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {str(row[0]) for row in rows}


@contextmanager
def _sqlite_connection(path: Path):
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=MEMORY")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA busy_timeout=1000")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def make_audit_event(event_id: str, user_id: str = "u1") -> MemoryAuditEvent:
    return MemoryAuditEvent(
        event_id=event_id,
        event_type="memory_exported",
        user_id=user_id,
        occurred_at=NOW,
        summary="audit event",
        counts={"memory_items": 1},
        metadata={"include_content": False},
    )


def make_confirmation(confirmation_id: str, user_id: str = "u1") -> MemoryPendingConfirmation:
    return MemoryPendingConfirmation(
        confirmation_id=confirmation_id,
        user_id=user_id,
        session_id="s1",
        memory_id="pending_memory",
        status="pending",
        memory_type="task",
        destination="task_checkpoint",
        sensitivity="high",
        reason="sensitive explicit memory requires user confirmation",
        summary="我的项目路径是 [redacted]",
        redacted_payload={"summary": "我的项目路径是 [redacted]", "memory_type": "task"},
        content_preview={"summary": "我的项目路径是 [redacted]"},
        created_at=NOW,
    )
