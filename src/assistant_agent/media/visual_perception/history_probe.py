"""Narrow availability probe for session-owned visual observation history."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)


class VisualObservationHistoryProbe(Protocol):
    """Answer whether one trusted session has searchable visual observations."""

    def has_searchable_observations(
        self,
        *,
        user_id: str,
        session_id: str,
        as_of_sequence: int | None,
    ) -> bool: ...


@dataclass(frozen=True)
class PoolVisualObservationHistoryProbe:
    """Read availability without exposing the semantic store to Agent middleware."""

    pool: SessionVisualSemanticStorePool

    def has_searchable_observations(
        self,
        *,
        user_id: str,
        session_id: str,
        as_of_sequence: int | None,
    ) -> bool:
        store = self.pool.peek(user_id, session_id)
        return bool(
            store is not None
            and store.has_searchable_history(as_of_sequence=as_of_sequence)
        )


__all__ = [
    "PoolVisualObservationHistoryProbe",
    "VisualObservationHistoryProbe",
]
