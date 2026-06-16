"""JSONL-backed persistent memory store."""

import json
from pathlib import Path

from multimodal_agent.schemas.memory import MemoryItem, MemoryQuery, MemorySearchResult


class JsonlMemoryStore:
    """Local JSONL memory store isolated by user_id."""

    def __init__(self, path: Path | str = ".data/memories.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, item: MemoryItem) -> MemoryItem:
        items = [
            memory
            for memory in self._read_all()
            if not (memory.user_id == item.user_id and memory.memory_id == item.memory_id)
        ]
        items.append(item)
        self._write_all(items)
        return item

    def search(
        self,
        query: MemoryQuery | str | None = None,
        *,
        user_id: str | None = None,
        limit: int = 10,
        **legacy: str,
    ) -> MemorySearchResult | list[MemoryItem]:
        """Search memories through the unified interface.

        The keyword-only form is kept for older unit tests and call sites.
        """

        from multimodal_agent.memory.retrieval import MemoryRetrievalStrategy, format_memory_context

        if query is None or isinstance(query, str):
            if user_id is None:
                raise ValueError("user_id is required for legacy memory search")
            legacy_query = query if isinstance(query, str) else legacy.get("query", "")
            legacy_result = MemoryRetrievalStrategy(self).retrieve(
                MemoryQuery(user_id=user_id, query=legacy_query, top_k=limit)
            )
            return legacy_result

        items = MemoryRetrievalStrategy(self).retrieve(query)
        return MemorySearchResult(
            items=items,
            query_used=query,
            total=len(items),
            ranking_reason="keyword_match_type_priority_recency",
            memory_context=format_memory_context(items, max_chars=query.max_context_chars),
        )

    def get(self, user_id: str, memory_id: str) -> MemoryItem | None:
        for item in self.list_by_user(user_id):
            if item.memory_id == memory_id:
                return item
        return None

    def delete(self, user_id: str, memory_id: str) -> bool:
        items = self._read_all()
        remaining = [item for item in items if not (item.user_id == user_id and item.memory_id == memory_id)]
        if len(remaining) == len(items):
            return False
        self._write_all(remaining)
        return True

    def delete_by_session(self, user_id: str, session_id: str) -> int:
        items = self._read_all()
        remaining = [
            item
            for item in items
            if not (item.user_id == user_id and (item.session_id or item.content.get("session_id")) == session_id)
        ]
        deleted_count = len(items) - len(remaining)
        if deleted_count:
            self._write_all(remaining)
        return deleted_count

    def list_by_user(self, user_id: str) -> list[MemoryItem]:
        items = [item for item in self._read_all() if item.user_id == user_id]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def clear_user(self, user_id: str) -> None:
        self._write_all([item for item in self._read_all() if item.user_id != user_id])

    def _read_all(self) -> list[MemoryItem]:
        if not self.path.exists():
            return []
        items: list[MemoryItem] = []
        with self.path.open("r", encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    items.append(MemoryItem.model_validate_json(line))
        return items

    def _write_all(self, items: list[MemoryItem]) -> None:
        with self.path.open("w", encoding="utf-8") as file:
            for item in items:
                file.write(json.dumps(item.model_dump(mode="json"), ensure_ascii=False) + "\n")
