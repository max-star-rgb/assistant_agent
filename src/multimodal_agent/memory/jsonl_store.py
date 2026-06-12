"""JSONL-backed persistent memory store."""

import json
from pathlib import Path

from multimodal_agent.memory.retriever import KeywordMemoryRetriever
from multimodal_agent.schemas.memory import MemoryItem


class JsonlMemoryStore:
    """Local JSONL memory store isolated by user_id."""

    def __init__(self, path: Path | str = ".data/memories.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, item: MemoryItem) -> MemoryItem:
        items = [memory for memory in self._read_all() if memory.memory_id != item.memory_id]
        items.append(item)
        self._write_all(items)
        return item

    def get(self, user_id: str, memory_id: str) -> MemoryItem | None:
        for item in self.list_by_user(user_id):
            if item.memory_id == memory_id:
                return item
        return None

    def list_by_user(self, user_id: str) -> list[MemoryItem]:
        items = [item for item in self._read_all() if item.user_id == user_id]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def clear_user(self, user_id: str) -> None:
        self._write_all([item for item in self._read_all() if item.user_id != user_id])

    def search(self, user_id: str, query: str, limit: int = 10) -> list[MemoryItem]:
        return KeywordMemoryRetriever(self).search(user_id=user_id, query=query, limit=limit)

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
