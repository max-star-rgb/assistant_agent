"""Local memory retrieval strategy and formatting."""

from multimodal_agent.memory.retriever import KeywordMemoryRetriever
from multimodal_agent.memory.store import MemoryStore
from multimodal_agent.schemas.memory import MemoryItem, MemoryQuery


TYPE_PRIORITY = {
    "preference": 0,
    "product": 1,
    "generation": 2,
    "render": 3,
    "task": 4,
    "conversation": 5,
    "video": 6,
}


class MemoryRetrievalStrategy:
    """Retrieve and format bounded local memory context."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def retrieve(self, query: MemoryQuery) -> list[MemoryItem]:
        memory_types = set(query.memory_types) if query.memory_types else None
        if query.query.strip():
            items = KeywordMemoryRetriever(self.store).search(
                user_id=query.user_id,
                query=query.query,
                limit=max(query.top_k * 4, query.top_k),
                memory_types=memory_types,
            )
            if not items:
                items = self.store.list_by_user(query.user_id)
                if memory_types is not None:
                    items = [item for item in items if item.memory_type in memory_types]
        else:
            items = self.store.list_by_user(query.user_id)
            if memory_types is not None:
                items = [item for item in items if item.memory_type in memory_types]

        filtered = []
        for item in items:
            if query.session_id is not None and item.content.get("session_id") != query.session_id:
                continue
            if query.since is not None and item.created_at < query.since:
                continue
            filtered.append(item)

        deduped = _dedupe(filtered)
        deduped.sort(
            key=lambda item: (
                item.relevance if item.relevance is not None else 0.0,
                -TYPE_PRIORITY.get(item.memory_type, 99),
                item.created_at,
            ),
            reverse=True,
        )
        return deduped[: query.top_k]


def format_memory_context(items: list[MemoryItem], max_chars: int = 500) -> str:
    """Format memory items into a bounded user-readable context block."""

    if not items:
        return ""

    lines = ["相关历史："]
    for index, item in enumerate(items, start=1):
        line = f"{index}. [{item.memory_type}] {item.summary}"
        candidate = "\n".join(lines + [line])
        if len(candidate) > max_chars:
            break
        lines.append(line)

    context = "\n".join(lines)
    return context[:max_chars]


def _dedupe(items: list[MemoryItem]) -> list[MemoryItem]:
    seen: set[str] = set()
    deduped: list[MemoryItem] = []
    for item in items:
        key = f"{item.memory_type}:{item.summary}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped
