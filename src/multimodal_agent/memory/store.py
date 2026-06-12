"""Local memory store implementations."""

from collections import defaultdict
from typing import Protocol

from multimodal_agent.schemas.memory import MemoryItem


class MemoryStore(Protocol):
    """Storage contract for memory items."""

    def save(self, item: MemoryItem) -> MemoryItem:
        """Persist a memory item."""

    def get(self, user_id: str, memory_id: str) -> MemoryItem | None:
        """Return one memory item for a user."""

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

    def get(self, user_id: str, memory_id: str) -> MemoryItem | None:
        return self._items_by_user.get(user_id, {}).get(memory_id)

    def list_by_user(self, user_id: str) -> list[MemoryItem]:
        items = list(self._items_by_user.get(user_id, {}).values())
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def clear_user(self, user_id: str) -> None:
        self._items_by_user.pop(user_id, None)
