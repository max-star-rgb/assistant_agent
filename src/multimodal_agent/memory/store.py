"""Local memory store implementations."""

from collections import defaultdict
from typing import Protocol

from multimodal_agent.schemas.memory import MemoryItem, MemoryQuery, MemorySearchResult


class MemoryStore(Protocol):
    """Storage contract for memory items."""

    def save(self, item: MemoryItem) -> MemoryItem:
        """Persist a memory item."""

    def search(self, query: MemoryQuery) -> MemorySearchResult:
        """Search memory items for a user."""

    def get(self, user_id: str, memory_id: str) -> MemoryItem | None:
        """Return one memory item for a user."""

    def delete(self, user_id: str, memory_id: str) -> bool:
        """Delete one memory item for a user."""

    def delete_by_session(self, user_id: str, session_id: str) -> int:
        """Delete memory items for one user session."""

    def list_by_user(self, user_id: str) -> list[MemoryItem]:
        """Return all memory items for a user."""

    def clear_user(self, user_id: str) -> None:
        """Delete all memory items for a user."""


class InMemoryStore:
    """In-memory memory store isolated by user_id."""

    def __init__(self) -> None:
        self._items_by_user: dict[str, dict[str, MemoryItem]] = defaultdict(dict)

    def save(self, item: MemoryItem) -> MemoryItem:
        self._items_by_user[item.user_id][item.memory_id] = item
        return item

    def search(self, query: MemoryQuery) -> MemorySearchResult:
        from multimodal_agent.memory.retrieval import MemoryRetrievalStrategy, format_memory_context

        items = MemoryRetrievalStrategy(self).retrieve(query)
        return MemorySearchResult(
            items=items,
            query_used=query,
            total=len(items),
            ranking_reason="keyword_match_type_priority_recency",
            memory_context=format_memory_context(items, max_chars=query.max_context_chars),
        )

    def get(self, user_id: str, memory_id: str) -> MemoryItem | None:
        return self._items_by_user.get(user_id, {}).get(memory_id)

    def delete(self, user_id: str, memory_id: str) -> bool:
        user_items = self._items_by_user.get(user_id)
        if not user_items or memory_id not in user_items:
            return False
        del user_items[memory_id]
        if not user_items:
            self._items_by_user.pop(user_id, None)
        return True

    def delete_by_session(self, user_id: str, session_id: str) -> int:
        user_items = self._items_by_user.get(user_id, {})
        memory_ids = [
            item.memory_id
            for item in user_items.values()
            if (item.session_id or item.content.get("session_id")) == session_id
        ]
        for memory_id in memory_ids:
            del user_items[memory_id]
        if not user_items:
            self._items_by_user.pop(user_id, None)
        return len(memory_ids)

    def list_by_user(self, user_id: str) -> list[MemoryItem]:
        items = list(self._items_by_user.get(user_id, {}).values())
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def clear_user(self, user_id: str) -> None:
        self._items_by_user.pop(user_id, None)
