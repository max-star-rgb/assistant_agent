import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from multimodal_agent.memory.jsonl_store import JsonlMemoryStore
from multimodal_agent.memory.manager import MemoryManager
from multimodal_agent.memory.sqlite_store import SCHEMA_VERSION, SQLiteMemoryStore
from multimodal_agent.memory.store import InMemoryStore, MemoryStore
from multimodal_agent.schemas.identity import RequestIdentity
from multimodal_agent.schemas.memory import MemoryItem, MemoryQuery, MemorySearchResult
from multimodal_agent.schemas.memory_audit import MemoryAuditEvent


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


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


@contextmanager
def _sqlite_connection(path: Path):
    connection = sqlite3.connect(path)
    try:
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
