from datetime import datetime, timedelta, timezone

import pytest

from assistant_agent.memory.facts import (
    fact_content,
    fact_from_item,
    mark_fact_superseded,
    normalize_fact_key,
)
from assistant_agent.schemas.memory import MemoryItem
from assistant_agent.schemas.memory_intelligence import MemoryFact


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def _memory_item(*, content: dict[str, object], source: str = "explicit_user_request") -> MemoryItem:
    return MemoryItem(
        memory_id="m1",
        user_id="u1",
        session_id="s1",
        memory_type="preference",
        summary="用户喜欢深色极简海报。",
        content=content,
        source=source,
        created_at=NOW,
    )


def test_memory_fact_rejects_invalid_validity_interval() -> None:
    with pytest.raises(ValueError, match="valid_to must be after valid_from"):
        MemoryFact(
            fact_key="user:preference:style",
            subject="user",
            predicate="preference.style",
            value="深色极简",
            provenance="user_explicit",
            observed_at=NOW,
            valid_from=NOW,
            valid_to=NOW,
        )


def test_memory_fact_rejects_active_superseded_state() -> None:
    with pytest.raises(ValueError, match="active fact cannot be superseded"):
        MemoryFact(
            fact_key="user:preference:style",
            subject="user",
            predicate="preference.style",
            value="深色极简",
            provenance="user_explicit",
            observed_at=NOW,
            superseded_by_memory_id="m2",
        )


def test_fact_from_item_maps_legacy_preference_key() -> None:
    fact = fact_from_item(
        _memory_item(content={"preference_key": "style", "style": "深色极简"})
    )

    assert fact is not None
    assert fact.fact_key == "user:preference:style"
    assert fact.predicate == "preference.style"
    assert fact.value == "深色极简"
    assert fact.status == "active"
    assert fact.provenance == "user_explicit"
    assert fact.conflict_policy == "replace"


def test_fact_from_item_maps_legacy_supersede_chain() -> None:
    fact = fact_from_item(
        _memory_item(
            content={
                "preference_key": "style",
                "style": "浅色日系",
                "superseded_by_memory_id": "m2",
                "conflict_reason": "newer_explicit_preference_for_same_key",
            }
        )
    )

    assert fact is not None
    assert fact.status == "superseded"
    assert fact.superseded_by_memory_id == "m2"


def test_fact_from_item_prefers_typed_fact_envelope() -> None:
    fact = MemoryFact(
        fact_key="user:employment:company",
        subject="user",
        predicate="employment.company",
        value="Acme",
        provenance="user_explicit",
        conflict_policy="confirm",
        observed_at=NOW,
    )
    item = _memory_item(content=fact_content(fact))

    parsed = fact_from_item(item)

    assert parsed == fact


def test_fact_from_item_does_not_infer_generic_fact_from_prose() -> None:
    assert fact_from_item(_memory_item(content={"summary": "用户在 Acme 工作"})) is None


def test_normalize_fact_key_is_stable_for_common_separators() -> None:
    assert normalize_fact_key(" User / Preference / Style ") == "user:preference:style"
    assert normalize_fact_key("user.preference--style") == "user:preference:style"


def test_mark_fact_superseded_updates_typed_and_legacy_fields() -> None:
    fact = MemoryFact(
        fact_key="user:preference:style",
        subject="user",
        predicate="preference.style",
        value="浅色日系",
        provenance="user_explicit",
        conflict_policy="replace",
        observed_at=NOW,
    )
    item = _memory_item(content=fact_content(fact))

    updated = mark_fact_superseded(
        item,
        by_memory_id="m2",
        at=NOW + timedelta(minutes=1),
        reason="replace_same_fact_key",
    )

    parsed = fact_from_item(updated)
    assert parsed is not None
    assert parsed.status == "superseded"
    assert parsed.superseded_by_memory_id == "m2"
    assert parsed.revision == 2
    assert updated.content["superseded_by_memory_id"] == "m2"
    assert updated.updated_at == NOW + timedelta(minutes=1)
