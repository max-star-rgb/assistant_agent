"""Local memory store implementations."""

from collections import defaultdict
from typing import Protocol

from multimodal_agent.schemas.memory import MemoryItem, MemoryQuery, MemorySearchResult
from multimodal_agent.schemas.memory_audit import MemoryPendingConfirmation


class MemoryStore(Protocol):
    """Storage contract for memory items and confirmation workflow state."""

    def save(self, item: MemoryItem) -> MemoryItem:
        """Persist a memory item."""

    def search(self, query: MemoryQuery) -> MemorySearchResult:
        """Search memory items for a user."""

    def get(self, user_id: str, memory_id: str) -> MemoryItem | None:
        """Return one memory item for a user."""

    def delete(self, user_id: str, memory_id: str) -> bool:
        """Delete one memory item for a user."""

    def hard_delete(self, user_id: str, memory_id: str) -> bool:
        """Permanently delete one memory item for a user."""

    def delete_by_session(self, user_id: str, session_id: str) -> int:
        """Delete memory items for one user session."""

    def list_by_user(self, user_id: str) -> list[MemoryItem]:
        """Return all memory items for a user."""

    def clear_user(self, user_id: str) -> None:
        """Delete all memory items for a user."""

    def save_confirmation(self, confirmation: MemoryPendingConfirmation) -> MemoryPendingConfirmation:
        """Persist a pending or resolved memory confirmation."""

    def get_confirmation(self, user_id: str, confirmation_id: str) -> MemoryPendingConfirmation | None:
        """Return one memory confirmation for a user."""

    def list_confirmations(
        self,
        *,
        user_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        include_resolved: bool = True,
        limit: int = 1000,
    ) -> list[MemoryPendingConfirmation]:
        """Return visible memory confirmations for a user."""

    def delete_confirmation(self, user_id: str, confirmation_id: str) -> bool:
        """Delete one memory confirmation for a user."""


class InMemoryStore:
    """In-memory memory store isolated by user_id."""

    def __init__(self) -> None:
        self._items_by_user: dict[str, dict[str, MemoryItem]] = defaultdict(dict)
        self._confirmations_by_user: dict[str, dict[str, MemoryPendingConfirmation]] = defaultdict(dict)

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

    def hard_delete(self, user_id: str, memory_id: str) -> bool:
        return self.delete(user_id, memory_id)

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
        self._confirmations_by_user.pop(user_id, None)

    def save_confirmation(self, confirmation: MemoryPendingConfirmation) -> MemoryPendingConfirmation:
        self._confirmations_by_user[confirmation.user_id][confirmation.confirmation_id] = confirmation
        return confirmation

    def get_confirmation(self, user_id: str, confirmation_id: str) -> MemoryPendingConfirmation | None:
        return self._confirmations_by_user.get(user_id, {}).get(confirmation_id)

    def list_confirmations(
        self,
        *,
        user_id: str,
        tenant_id: str | None = None,
        project_id: str | None = None,
        include_resolved: bool = True,
        limit: int = 1000,
    ) -> list[MemoryPendingConfirmation]:
        confirmations = [
            confirmation
            for confirmation in self._confirmations_by_user.get(user_id, {}).values()
            if (confirmation.tenant_id is None or confirmation.tenant_id == tenant_id)
            and (confirmation.project_id is None or confirmation.project_id == project_id)
            and (include_resolved or confirmation.status == "pending")
        ]
        return sorted(confirmations, key=lambda item: item.created_at, reverse=True)[: max(1, limit)]

    def delete_confirmation(self, user_id: str, confirmation_id: str) -> bool:
        user_confirmations = self._confirmations_by_user.get(user_id)
        if not user_confirmations or confirmation_id not in user_confirmations:
            return False
        del user_confirmations[confirmation_id]
        if not user_confirmations:
            self._confirmations_by_user.pop(user_id, None)
        return True
