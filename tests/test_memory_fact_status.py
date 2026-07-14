from datetime import datetime, timezone

from assistant_agent.memory.facts import is_active_memory_fact, memory_fact_status
from assistant_agent.memory.manager import MemoryManager
from assistant_agent.memory.profile import UserProfileMemory
from assistant_agent.memory.retrieval import MemoryRetrievalStrategy
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.memory import MemoryItem, MemoryQuery
from assistant_agent.schemas.memory_intelligence import MemoryFact, MemoryFactStatus


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def _fact_item(
    memory_id: str,
    *,
    value: str,
    status: MemoryFactStatus = "active",
    superseded_by_memory_id: str | None = None,
    project_id: str | None = None,
) -> MemoryItem:
    fact = MemoryFact(
        fact_key="user:preference:style",
        subject="user",
        predicate="preference.style",
        value=value,
        status=status,
        provenance="imported",
        conflict_policy="confirm",
        observed_at=NOW,
        superseded_by_memory_id=superseded_by_memory_id,
    )
    return MemoryItem(
        memory_id=memory_id,
        user_id="u1",
        project_id=project_id,
        session_id="s1",
        memory_type="preference",
        summary=f"用户喜欢{value}风格。",
        content={"fact": fact.model_dump(mode="json")},
        source="imported",
        created_at=NOW,
    )


def test_memory_fact_status_prefers_typed_state_and_supports_legacy() -> None:
    disputed = _fact_item("disputed", value="深色", status="disputed")
    legacy = MemoryItem(
        memory_id="legacy",
        user_id="u1",
        memory_type="preference",
        summary="旧偏好",
        content={"superseded_by_memory_id": "new"},
        created_at=NOW,
    )

    assert memory_fact_status(disputed) == "disputed"
    assert is_active_memory_fact(disputed) is False
    assert memory_fact_status(legacy) == "superseded"


def test_retrieval_excludes_inactive_typed_facts_by_default() -> None:
    store = InMemoryStore()
    for item in (
        _fact_item("active", value="当前"),
        _fact_item("superseded", value="旧", status="superseded", superseded_by_memory_id="active"),
        _fact_item("disputed", value="待确认", status="disputed"),
        _fact_item("retracted", value="已撤回", status="retracted"),
    ):
        store.save(item)

    default_items = MemoryRetrievalStrategy(store).retrieve(MemoryQuery(user_id="u1"))
    debug_items = MemoryRetrievalStrategy(store).retrieve(
        MemoryQuery(user_id="u1", include_superseded=True)
    )

    assert [item.memory_id for item in default_items] == ["active"]
    assert {item.memory_id for item in debug_items} == {"active", "superseded"}


def test_user_profile_merge_rejects_inactive_fact() -> None:
    profile = UserProfileMemory.empty("u1", now=NOW)

    changed = profile.merge_memory(_fact_item("disputed", value="待确认", status="disputed"))

    assert changed is False
    assert profile.preferences == []
    assert profile.source_memory_ids == []


def test_profile_rebuild_uses_only_active_unscoped_facts() -> None:
    store = InMemoryStore()
    store.save(_fact_item("active", value="当前"))
    store.save(
        _fact_item("superseded", value="旧", status="superseded", superseded_by_memory_id="active")
    )
    store.save(_fact_item("disputed", value="待确认", status="disputed"))
    store.save(_fact_item("project", value="项目偏好", project_id="p1"))
    manager = MemoryManager(store)

    status = manager.rebuild_user_profile_for_identity(
        RequestIdentity.for_user(user_id="u1", session_id="s1"),
        dry_run=True,
    )

    assert status.expected_source_memory_ids == ["active"]
    assert status.superseded_source_memory_ids == ["superseded"]
    assert status.expected_summary == "用户画像：偏好：用户喜欢当前风格。"


def test_profile_status_reports_unresolved_typed_fact_conflict() -> None:
    store = InMemoryStore()
    store.save(_fact_item("style_light", value="浅色"))
    store.save(_fact_item("style_dark", value="深色"))
    manager = MemoryManager(store)

    status = manager.rebuild_user_profile_for_identity(
        RequestIdentity.for_user(user_id="u1", session_id="s1"),
        dry_run=True,
    )

    assert status.profile_conflicts == [
        {
            "fact_key": "user:preference:style",
            "preference_key": "style",
            "active_memory_id": "style_light",
            "active_memory_ids": ["style_dark", "style_light"],
            "superseded_memory_ids": [],
            "disputed_memory_ids": [],
            "unresolved": True,
        }
    ]
    assert "profile_unresolved_conflicts" in status.issues
