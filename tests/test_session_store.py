from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.sessions import SessionCreate
from assistant_agent.services import session_store as session_store_module
from assistant_agent.services.session_store import InMemorySessionStore, JsonlSessionStore, create_session_store


def test_in_memory_session_store_treats_session_id_as_thread_id() -> None:
    store = InMemorySessionStore()

    record = store.create(SessionCreate(user_id="u1", title="购物"))

    assert record.session_id.startswith("session_")
    assert record.thread_id == record.session_id
    assert store.get("u1", record.session_id) == record


def test_session_store_touches_run_metadata() -> None:
    store = InMemorySessionStore()

    record = store.touch_run(
        user_id="u1",
        session_id="s1",
        run_id="run_1",
        trace_id="trace_1",
        message_preview="我喜欢白色低帮运动鞋",
        status="completed",
    )

    assert record.thread_id == "s1"
    assert record.run_count == 1
    assert record.last_run_id == "run_1"
    assert record.last_trace_id == "trace_1"
    assert record.last_status == "completed"
    assert record.title == "我喜欢白色低帮运动鞋"


def test_jsonl_session_store_persists_records(tmp_path) -> None:
    path = tmp_path / "sessions.jsonl"
    first_store = JsonlSessionStore(path)
    first_store.touch_run(
        user_id="u1",
        session_id="s1",
        run_id="run_1",
        trace_id="trace_1",
        message_preview="第一轮",
        status="completed",
    )

    restarted_store = JsonlSessionStore(path)
    record = restarted_store.get("u1", "s1")

    assert record is not None
    assert record.thread_id == "s1"
    assert record.last_message_preview == "第一轮"


def test_jsonl_session_store_skips_invalid_records(tmp_path, caplog) -> None:
    path = tmp_path / "sessions.jsonl"
    path.write_text('{"user_id": "u1"\n', encoding="utf-8")
    store = JsonlSessionStore(path)

    record = store.create(SessionCreate(user_id="u2"), session_id="s2")

    assert record.session_id == "s2"
    assert store.list_by_user("u1") == []
    assert store.list_by_user("u2") == [record]
    assert "Skipping invalid session record" in caplog.text


def test_session_store_delete_by_user_is_scoped(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / "sessions.jsonl")
    store.create(SessionCreate(user_id="u1"), session_id="s1")
    store.create(SessionCreate(user_id="u2"), session_id="s1")

    assert store.delete_by_user("u1") == 1
    assert store.list_by_user("u1") == []
    assert store.list_by_user("u2")


def test_create_session_store_uses_jsonl_next_to_conversation_history(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(session_store_module, "REPO_ROOT", tmp_path)
    store = create_session_store(
        ProviderConfig(
            conversation_history_backend="jsonl",
            conversation_history_path="relative/conversation_history.jsonl",
        )
    )

    assert isinstance(store, JsonlSessionStore)
    store.create(SessionCreate(user_id="u1"), session_id="s1")
    assert (tmp_path / "relative" / "sessions.jsonl").exists()
