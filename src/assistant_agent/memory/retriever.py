"""Keyword-based memory retrieval."""

import re
from typing import Any

from assistant_agent.memory.store import MemoryStore
from assistant_agent.schemas.memory import MemoryItem


_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]+")


class KeywordMemoryRetriever:
    """Retrieve user-isolated memories with simple keyword matching."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def search(
        self,
        user_id: str,
        query: str,
        limit: int = 10,
        memory_types: set[str] | None = None,
    ) -> list[MemoryItem]:
        candidate_search = getattr(self.store, "search_candidates", None)
        if callable(candidate_search):
            return candidate_search(
                user_id=user_id,
                query=query,
                limit=limit,
                memory_types=memory_types,
            )

        keywords = self._keywords(query)
        if not keywords:
            return []

        scored: list[tuple[float, MemoryItem]] = []
        for item in self.store.list_by_user(user_id):
            if memory_types is not None and item.memory_type not in memory_types:
                continue
            score = self._score(item, keywords)
            if score > 0:
                scored.append((score, item.model_copy(update={"relevance": min(score, 1.0)})))

        scored.sort(key=lambda pair: (pair[0], pair[1].created_at), reverse=True)
        return [item for _, item in scored[:limit]]

    def _score(self, item: MemoryItem, keywords: set[str]) -> float:
        searchable = self._searchable_text(item)
        hits = sum(1 for keyword in keywords if keyword in searchable)
        if hits == 0:
            return 0.0
        return hits / len(keywords)

    def _searchable_text(self, item: MemoryItem) -> str:
        parts = [
            item.memory_id,
            item.user_id,
            item.memory_type,
            item.summary,
            item.reason or "",
            " ".join(item.tags),
            " ".join(item.artifact_refs),
            self._flatten_content(item.content),
        ]
        return " ".join(parts).lower()

    def _flatten_content(self, value: Any) -> str:
        if isinstance(value, dict):
            return " ".join(f"{key} {self._flatten_content(item)}" for key, item in value.items())
        if isinstance(value, list):
            return " ".join(self._flatten_content(item) for item in value)
        return str(value)

    def _keywords(self, query: str) -> set[str]:
        normalized = query.strip().lower()
        if not normalized:
            return set()
        tokens = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", normalized))
        for segment in _CHINESE_RE.findall(normalized):
            tokens.update(_chinese_ngrams(segment))
        tokens.add(normalized)
        return tokens


def _chinese_ngrams(segment: str) -> set[str]:
    """Return short Chinese phrase candidates for deterministic local recall."""

    if len(segment) < 2:
        return set()
    grams: set[str] = set()
    max_size = min(4, len(segment))
    for size in range(2, max_size + 1):
        for index in range(0, len(segment) - size + 1):
            grams.add(segment[index : index + size])
    return grams
