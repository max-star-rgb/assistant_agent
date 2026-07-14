"""Deterministic conflict decisions for structured local memory facts."""

from datetime import datetime, timezone

from assistant_agent.memory.facts import fact_from_item
from assistant_agent.schemas.memory import MemoryItem, memory_scope_for_item
from assistant_agent.schemas.memory_intelligence import MemoryConflictDecision, MemoryFact


class MemoryConflictResolver:
    """Resolve one candidate against visible items without mutating storage."""

    def __init__(self, *, now: datetime | None = None) -> None:
        self.now = now

    def resolve(
        self,
        candidate: MemoryItem,
        existing: list[MemoryItem],
    ) -> MemoryConflictDecision:
        candidate_fact = fact_from_item(candidate)
        if candidate_fact is None:
            return MemoryConflictDecision(action="append", reason="no_structured_fact")

        matching = [
            item
            for item in existing
            if self._is_active_same_slot(item, candidate, candidate_fact)
        ]
        if not matching:
            return MemoryConflictDecision(
                action="append",
                reason="no_active_fact_conflict",
                fact_key=candidate_fact.fact_key,
            )

        matching_ids = [item.memory_id for item in matching]
        same_value = [
            item
            for item in matching
            if (fact := fact_from_item(item)) is not None
            and _normalize_value(fact.value) == _normalize_value(candidate_fact.value)
        ]
        conflicting = [item for item in matching if item not in same_value]
        if same_value and not conflicting:
            return MemoryConflictDecision(
                action="merge",
                reason="same_fact_value",
                fact_key=candidate_fact.fact_key,
                matching_memory_ids=matching_ids,
            )

        if candidate_fact.conflict_policy == "coexist":
            return MemoryConflictDecision(
                action="coexist",
                reason="coexist_same_fact_key",
                fact_key=candidate_fact.fact_key,
                matching_memory_ids=matching_ids,
            )

        if _allows_automatic_replace(candidate_fact):
            superseded_ids = [item.memory_id for item in conflicting]
            return MemoryConflictDecision(
                action="supersede",
                reason="replace_same_fact_key",
                fact_key=candidate_fact.fact_key,
                matching_memory_ids=matching_ids,
                superseded_memory_ids=superseded_ids,
            )

        return MemoryConflictDecision(
            action="confirm",
            reason="confirmation_required_same_fact_key",
            fact_key=candidate_fact.fact_key,
            matching_memory_ids=matching_ids,
            requires_confirmation=True,
        )

    def _is_active_same_slot(
        self,
        existing: MemoryItem,
        candidate: MemoryItem,
        candidate_fact: MemoryFact,
    ) -> bool:
        if existing.memory_id == candidate.memory_id or existing.source == "user_profile":
            return False
        if not _same_governance_scope(existing, candidate):
            return False
        if _is_expired(existing, now=self.now):
            return False
        existing_fact = fact_from_item(existing)
        return (
            existing_fact is not None
            and existing_fact.status == "active"
            and existing_fact.fact_key == candidate_fact.fact_key
        )


def _allows_automatic_replace(fact: MemoryFact) -> bool:
    if fact.provenance == "user_confirmed":
        return True
    return fact.conflict_policy == "replace" and fact.predicate.startswith("preference.")


def _same_governance_scope(left: MemoryItem, right: MemoryItem) -> bool:
    return (
        left.user_id == right.user_id
        and left.tenant_id == right.tenant_id
        and left.project_id == right.project_id
        and memory_scope_for_item(left) == memory_scope_for_item(right)
    )


def _is_expired(item: MemoryItem, *, now: datetime | None) -> bool:
    if item.expires_at is None:
        return False
    resolved_now = now or datetime.now(tz=item.expires_at.tzinfo or timezone.utc)
    if resolved_now.tzinfo is None and item.expires_at.tzinfo is not None:
        resolved_now = resolved_now.replace(tzinfo=item.expires_at.tzinfo)
    return item.expires_at < resolved_now


def _normalize_value(value: str) -> str:
    return "".join(
        character
        for character in value.strip().lower()
        if character.isalnum() or "\u4e00" <= character <= "\u9fff"
    )
