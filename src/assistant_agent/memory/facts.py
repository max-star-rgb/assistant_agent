"""Pure helpers for typed facts stored in local memory items."""

from datetime import datetime
from typing import Any

from assistant_agent.schemas.memory import MemoryItem
from assistant_agent.schemas.memory_intelligence import (
    MemoryFact,
    MemoryFactProvenance,
    MemoryFactStatus,
    normalize_fact_key,
)


def fact_content(fact: MemoryFact) -> dict[str, Any]:
    """Return the canonical content envelope for a typed fact."""

    return {"fact": fact.model_dump(mode="json")}


def fact_from_item(item: MemoryItem) -> MemoryFact | None:
    """Parse a typed fact or map supported legacy preference fields."""

    payload = item.content.get("fact")
    if isinstance(payload, dict):
        return MemoryFact.model_validate(payload)

    explicit = _fact_from_explicit_fields(item)
    if explicit is not None:
        return explicit
    return _fact_from_legacy_preference(item)


def memory_fact_status(item: MemoryItem) -> MemoryFactStatus:
    """Return typed status with legacy supersede compatibility."""

    fact = fact_from_item(item)
    if fact is not None:
        return fact.status
    if _optional_string(item.content.get("superseded_by_memory_id")) is not None:
        return "superseded"
    return "active"


def is_active_memory_fact(item: MemoryItem) -> bool:
    """Return whether an item may contribute to active recall and profile state."""

    return memory_fact_status(item) == "active"


def mark_fact_superseded(
    item: MemoryItem,
    *,
    by_memory_id: str,
    at: datetime,
    reason: str,
) -> MemoryItem:
    """Return an item whose typed and legacy state points to its replacement."""

    fact = fact_from_item(item)
    if fact is None:
        raise ValueError("memory item does not contain a structured fact")
    updated_fact = fact.model_copy(
        update={
            "status": "superseded",
            "superseded_by_memory_id": by_memory_id,
            "conflict_reason": reason,
            "revision": fact.revision + 1,
        }
    )
    content = {
        **item.content,
        **fact_content(updated_fact),
        "superseded_by_memory_id": by_memory_id,
        "superseded_at": at.isoformat(),
        "conflict_reason": reason,
    }
    return item.model_copy(update={"content": content, "updated_at": at})


def _fact_from_explicit_fields(item: MemoryItem) -> MemoryFact | None:
    raw_key = item.content.get("fact_key")
    raw_value = item.content.get("fact_value", item.content.get("value"))
    if raw_key in (None, "", [], {}) or raw_value in (None, "", [], {}):
        return None
    fact_key = normalize_fact_key(str(raw_key))
    key_parts = fact_key.split(":")
    return MemoryFact(
        fact_key=fact_key,
        subject=str(item.content.get("subject") or key_parts[0]),
        predicate=str(item.content.get("predicate") or ".".join(key_parts[1:]) or key_parts[0]),
        value=str(raw_value),
        status=_legacy_status(item),
        provenance=_provenance_for_item(item),
        conflict_policy=str(item.content.get("conflict_policy") or "confirm"),
        observed_at=item.updated_at or item.created_at,
        confidence=float(item.content.get("confidence", 1.0)),
        supersedes_memory_ids=_string_list(item.content.get("supersedes_memory_ids")),
        superseded_by_memory_id=_optional_string(item.content.get("superseded_by_memory_id")),
        conflict_reason=_optional_string(item.content.get("conflict_reason")),
    )


def _fact_from_legacy_preference(item: MemoryItem) -> MemoryFact | None:
    if item.memory_type != "preference" or item.source == "user_profile":
        return None
    raw_key = item.content.get("preference_key") or item.content.get("conflict_key")
    if raw_key in (None, "", [], {}):
        raw_key = next(
            (key for key in ("style", "budget") if item.content.get(key) not in (None, "", [], {})),
            None,
        )
    if raw_key in (None, "", [], {}) and "预算" in item.summary:
        raw_key = "budget"
    if raw_key in (None, "", [], {}):
        return None
    preference_key = normalize_fact_key(str(raw_key))
    value = item.content.get(preference_key)
    if value in (None, "", [], {}):
        value = item.summary
    return MemoryFact(
        fact_key=f"user:preference:{preference_key}",
        subject="user",
        predicate=f"preference.{preference_key}",
        value=str(value),
        status=_legacy_status(item),
        provenance=_provenance_for_item(item),
        conflict_policy="replace",
        observed_at=item.updated_at or item.created_at,
        supersedes_memory_ids=_string_list(item.content.get("supersedes_memory_ids")),
        superseded_by_memory_id=_optional_string(item.content.get("superseded_by_memory_id")),
        conflict_reason=_optional_string(item.content.get("conflict_reason")),
    )


def _legacy_status(item: MemoryItem) -> str:
    if _optional_string(item.content.get("superseded_by_memory_id")) is not None:
        return "superseded"
    return str(item.content.get("fact_status") or "active")


def _provenance_for_item(item: MemoryItem) -> MemoryFactProvenance:
    if item.content.get("consent") == "explicit_confirmation":
        return "user_confirmed"
    if item.source == "explicit_user_request":
        return "user_explicit"
    if item.source in {"tool", "tool_result", "capability_output"}:
        return "tool_verified"
    if item.source == "agent":
        return "assistant_inferred"
    return "imported"


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _optional_string(value: object) -> str | None:
    if value in (None, "", [], {}):
        return None
    return str(value).strip() or None
