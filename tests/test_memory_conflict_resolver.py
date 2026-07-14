from datetime import datetime, timedelta, timezone

import pytest

from assistant_agent.memory.conflict_resolver import MemoryConflictResolver
from assistant_agent.memory.facts import fact_content
from assistant_agent.schemas.memory import MemoryItem
from assistant_agent.schemas.memory_intelligence import MemoryConflictPolicy, MemoryFact


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def _fact_item(
    memory_id: str,
    *,
    value: str,
    policy: MemoryConflictPolicy = "replace",
    fact_key: str = "user:preference:style",
    predicate: str = "preference.style",
    summary: str | None = None,
    project_id: str | None = None,
    status: str = "active",
    provenance: str = "user_explicit",
    expires_at: datetime | None = None,
) -> MemoryItem:
    fact = MemoryFact(
        fact_key=fact_key,
        subject="user",
        predicate=predicate,
        value=value,
        status=status,
        provenance=provenance,
        conflict_policy=policy,
        observed_at=NOW,
    )
    return MemoryItem(
        memory_id=memory_id,
        user_id="u1",
        project_id=project_id,
        session_id="s1",
        memory_type="preference",
        summary=summary or f"用户偏好 {value}",
        content=fact_content(fact),
        source="explicit_user_request",
        created_at=NOW,
        expires_at=expires_at,
    )


@pytest.mark.parametrize(
    ("policy", "expected_action"),
    [
        ("replace", "supersede"),
        ("coexist", "coexist"),
        ("confirm", "confirm"),
    ],
)
def test_same_preference_fact_key_uses_declared_policy(
    policy: MemoryConflictPolicy,
    expected_action: str,
) -> None:
    existing = _fact_item("old", value="浅色日系", policy=policy)
    candidate = _fact_item("new", value="深色极简", policy=policy)

    decision = MemoryConflictResolver().resolve(candidate, [existing])

    assert decision.action == expected_action
    assert decision.matching_memory_ids == ["old"]
    assert decision.superseded_memory_ids == (["old"] if expected_action == "supersede" else [])
    assert decision.requires_confirmation is (expected_action == "confirm")


def test_generic_model_supplied_replace_policy_requires_confirmation() -> None:
    existing = _fact_item(
        "old",
        fact_key="user:employment:company",
        predicate="employment.company",
        value="Acme",
        policy="replace",
    )
    candidate = _fact_item(
        "new",
        fact_key="user:employment:company",
        predicate="employment.company",
        value="Example Corp",
        policy="replace",
    )

    decision = MemoryConflictResolver().resolve(candidate, [existing])

    assert decision.action == "confirm"
    assert decision.reason == "confirmation_required_same_fact_key"
    assert decision.requires_confirmation is True


def test_user_confirmed_generic_replace_supersedes() -> None:
    existing = _fact_item(
        "old",
        fact_key="user:employment:company",
        predicate="employment.company",
        value="Acme",
        policy="confirm",
    )
    candidate = _fact_item(
        "new",
        fact_key="user:employment:company",
        predicate="employment.company",
        value="Example Corp",
        policy="replace",
        provenance="user_confirmed",
    )

    decision = MemoryConflictResolver().resolve(candidate, [existing])

    assert decision.action == "supersede"
    assert decision.superseded_memory_ids == ["old"]


def test_same_fact_value_merges_even_when_summary_wording_differs() -> None:
    decision = MemoryConflictResolver().resolve(
        _fact_item("new", value="深色极简", summary="现在偏爱深色极简"),
        [_fact_item("old", value="深色极简", summary="用户喜欢深色极简海报")],
    )

    assert decision.action == "merge"
    assert decision.reason == "same_fact_value"
    assert decision.matching_memory_ids == ["old"]


def test_different_governance_scope_never_conflicts() -> None:
    decision = MemoryConflictResolver().resolve(
        _fact_item("new", value="深色", project_id="p2"),
        [_fact_item("old", value="浅色", project_id="p1")],
    )

    assert decision.action == "append"
    assert decision.reason == "no_active_fact_conflict"


def test_unstructured_candidate_appends_for_legacy_dedupe_handling() -> None:
    candidate = MemoryItem(
        memory_id="new",
        user_id="u1",
        session_id="s1",
        memory_type="task",
        summary="继续整理方案",
        source="explicit_user_request",
        created_at=NOW,
    )

    decision = MemoryConflictResolver().resolve(candidate, [])

    assert decision.action == "append"
    assert decision.reason == "no_structured_fact"


def test_inactive_and_expired_facts_do_not_conflict() -> None:
    superseded = _fact_item("superseded", value="浅色", status="superseded")
    expired = _fact_item("expired", value="蓝色", expires_at=NOW - timedelta(days=1))
    candidate = _fact_item("new", value="深色")

    decision = MemoryConflictResolver(now=NOW).resolve(candidate, [superseded, expired])

    assert decision.action == "append"
    assert decision.matching_memory_ids == []


def test_decision_ids_are_sorted_for_deterministic_audit() -> None:
    candidate = _fact_item("new", value="深色")

    decision = MemoryConflictResolver().resolve(
        candidate,
        [_fact_item("z-old", value="浅色"), _fact_item("a-old", value="蓝色")],
    )

    assert decision.matching_memory_ids == ["a-old", "z-old"]
    assert decision.superseded_memory_ids == ["a-old", "z-old"]
