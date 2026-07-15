from datetime import datetime, timedelta, timezone

import pytest

from assistant_agent.memory.facts import fact_from_item
from assistant_agent.memory.manager import MemoryConfirmationRequired, MemoryManager
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.memory_intelligence import MemoryConflictPolicy, MemoryFact


NOW = datetime.now(timezone.utc)


def _fact_payload(
    fact_key: str,
    value: str,
    policy: MemoryConflictPolicy,
    *,
    predicate: str,
) -> dict[str, object]:
    return MemoryFact(
        fact_key=fact_key,
        subject="user",
        predicate=predicate,
        value=value,
        provenance="user_explicit",
        conflict_policy=policy,
        observed_at=NOW,
    ).model_dump(mode="json")


def _save(
    manager: MemoryManager,
    *,
    memory_id: str,
    text: str,
    fact_key: str,
    predicate: str,
    value: str,
    policy: MemoryConflictPolicy,
    at: datetime,
):
    return manager.save_explicit(
        user_id="u1",
        session_id="s1",
        text=text,
        content={
            "summary": text.removeprefix("记住"),
            "fact": _fact_payload(fact_key, value, policy, predicate=predicate),
        },
        memory_id=memory_id,
        created_at=at,
    )


def test_memory_manager_supersedes_allowed_preference_fact() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)
    old = _save(
        manager,
        memory_id="spice_old",
        text="记住我喜欢微辣",
        fact_key="user:preference:spice",
        predicate="preference.spice",
        value="mild",
        policy="replace",
        at=NOW,
    )
    new = _save(
        manager,
        memory_id="spice_new",
        text="记住我现在喜欢重辣",
        fact_key="user:preference:spice",
        predicate="preference.spice",
        value="hot",
        policy="replace",
        at=NOW + timedelta(minutes=1),
    )

    old_stored = store.get("u1", old.memory_id)
    assert old_stored is not None
    assert old_stored.content["fact"]["status"] == "superseded"
    assert old_stored.content["fact"]["superseded_by_memory_id"] == new.memory_id
    assert new.content["fact"]["supersedes_memory_ids"] == [old.memory_id]
    assert new.content["supersedes_memory_ids"] == [old.memory_id]


def test_memory_manager_merges_same_fact_value_with_different_summary() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)
    first = _save(
        manager,
        memory_id="spice_first",
        text="记住我喜欢重辣",
        fact_key="user:preference:spice",
        predicate="preference.spice",
        value="hot",
        policy="replace",
        at=NOW,
    )
    second = _save(
        manager,
        memory_id="spice_second",
        text="记住我现在仍然偏爱重辣口味",
        fact_key="user:preference:spice",
        predicate="preference.spice",
        value="hot",
        policy="replace",
        at=NOW + timedelta(minutes=1),
    )

    assert second.memory_id == first.memory_id
    assert second.content["observation_count"] == 2
    assert store.get("u1", "spice_second") is None


def test_memory_manager_keeps_coexisting_fact_values_active() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)
    first = _save(
        manager,
        memory_id="city_shanghai",
        text="记住我常去上海",
        fact_key="user:travel:city",
        predicate="travel.city",
        value="上海",
        policy="coexist",
        at=NOW,
    )
    second = _save(
        manager,
        memory_id="city_hangzhou",
        text="记住我也常去杭州",
        fact_key="user:travel:city",
        predicate="travel.city",
        value="杭州",
        policy="coexist",
        at=NOW + timedelta(minutes=1),
    )

    assert fact_from_item(first).status == "active"
    assert fact_from_item(second).status == "active"
    assert store.get("u1", first.memory_id) is not None
    assert store.get("u1", second.memory_id) is not None


def test_generic_fact_conflict_requires_confirmation_before_write() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)
    _save(
        manager,
        memory_id="company_a",
        text="记住我在 A 公司工作",
        fact_key="user:employment:company",
        predicate="employment.company",
        value="A",
        policy="replace",
        at=NOW,
    )

    with pytest.raises(MemoryConfirmationRequired) as caught:
        _save(
            manager,
            memory_id="company_b",
            text="记住我现在在 B 公司工作",
            fact_key="user:employment:company",
            predicate="employment.company",
            value="B",
            policy="replace",
            at=NOW + timedelta(minutes=1),
        )

    confirmation = caught.value.confirmation
    assert confirmation.confirmation_kind == "fact_conflict"
    assert confirmation.fact_key == "user:employment:company"
    assert confirmation.conflict_memory_ids == ["company_a"]
    assert store.get("u1", "company_b") is None


def test_confirming_fact_conflict_recomputes_and_supersedes() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)
    _save(
        manager,
        memory_id="company_a",
        text="记住我在 A 公司工作",
        fact_key="user:employment:company",
        predicate="employment.company",
        value="A",
        policy="confirm",
        at=NOW,
    )
    with pytest.raises(MemoryConfirmationRequired) as caught:
        _save(
            manager,
            memory_id="company_b",
            text="记住我现在在 B 公司工作",
            fact_key="user:employment:company",
            predicate="employment.company",
            value="B",
            policy="confirm",
            at=NOW + timedelta(minutes=1),
        )

    confirmed = manager.confirm_memory_for_identity(
        RequestIdentity.for_user(user_id="u1", session_id="s1"),
        caught.value.confirmation.confirmation_id,
        created_at=NOW + timedelta(minutes=2),
    )

    assert confirmed is not None
    assert confirmed.status == "confirmed"
    new_item = store.get("u1", "company_b")
    old_item = store.get("u1", "company_a")
    assert new_item is not None
    assert old_item is not None
    assert fact_from_item(new_item).provenance == "user_confirmed"
    assert fact_from_item(new_item).supersedes_memory_ids == ["company_a"]
    assert fact_from_item(old_item).superseded_by_memory_id == "company_b"


def test_conflict_audit_contains_ids_but_not_old_fact_values() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)
    identity = RequestIdentity.for_user(user_id="u1", session_id="s1")
    _save(
        manager,
        memory_id="company_legacy_old",
        text="记住我在 Legacy Company Alpha 工作",
        fact_key="user:employment:company",
        predicate="employment.company",
        value="Legacy Company Alpha",
        policy="confirm",
        at=NOW,
    )
    with pytest.raises(MemoryConfirmationRequired):
        _save(
            manager,
            memory_id="company_new",
            text="记住我现在在 New Company 工作",
            fact_key="user:employment:company",
            predicate="employment.company",
            value="New Company",
            policy="confirm",
            at=NOW + timedelta(minutes=1),
        )

    event = manager.list_audit_events_for_identity(identity)[0]
    assert event.event_type == "memory_confirmation_created"
    assert event.metadata["fact_key"] == "user:employment:company"
    assert event.metadata["matching_memory_ids"] == ["company_legacy_old"]
    assert "Legacy Company Alpha" not in str(event.metadata)
