"""Local memory retrieval strategy and formatting."""

from datetime import datetime, timezone

from assistant_agent.memory.facts import memory_fact_status
from assistant_agent.memory.retriever import KeywordMemoryRetriever
from assistant_agent.memory.store import MemoryStore
from assistant_agent.schemas.memory import MemoryItem, MemoryQuery, memory_item_matches_query_scope


TYPE_PRIORITY = {
    "preference": 0,
    "product": 1,
    "generation": 2,
    "image": 3,
    "video": 4,
    "render": 5,
    "artifact": 6,
    "task": 7,
    "conversation": 8,
}

CAPABILITY_TYPE_PRIORITY = {
    "image_generation": {
        "preference": 0,
        "artifact": 1,
        "image": 2,
        "product": 3,
        "conversation": 4,
    },
    "product_search": {
        "preference": 0,
        "product": 1,
        "conversation": 2,
    },
    "render_3d": {
        "preference": 0,
        "product": 1,
        "artifact": 2,
        "render": 3,
        "image": 4,
    },
    "direct_chat": {
        "conversation": 0,
        "preference": 1,
    },
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
            if not items and _allows_recent_context_fallback(query.query):
                items = self.store.list_by_user(query.user_id)
                if memory_types is not None:
                    items = [item for item in items if item.memory_type in memory_types]
        else:
            items = self.store.list_by_user(query.user_id)
            if memory_types is not None:
                items = [item for item in items if item.memory_type in memory_types]

        filtered = []
        for item in items:
            fact_status = memory_fact_status(item)
            if fact_status in {"disputed", "retracted"}:
                continue
            if fact_status == "superseded" and not query.include_superseded:
                continue
            if not memory_item_matches_query_scope(item, query):
                continue
            item_session_id = item.session_id or item.content.get("session_id")
            if query.session_id is not None and item_session_id != query.session_id:
                continue
            if query.tags and not set(query.tags).issubset(set(item.tags)):
                continue
            if query.since is not None and item.created_at < query.since:
                continue
            if not query.include_expired and item.expires_at is not None:
                now = datetime.now(tz=item.expires_at.tzinfo or timezone.utc)
                if item.expires_at < now:
                    continue
            filtered.append(item)

        deduped = _dedupe(filtered)
        deduped.sort(
            key=lambda item: (
                item.relevance if item.relevance is not None else 0.0,
                -_type_priority(item, query.capability),
                _artifact_score(item),
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
        ref_text = f" 引用：{item.artifact_refs[0]}" if item.artifact_refs else ""
        line = f"{index}. [{item.memory_type}] {item.summary}{ref_text}"
        candidate = "\n".join(lines + [line])
        if len(candidate) > max_chars:
            break
        lines.append(line)

    context = "\n".join(lines)
    return context[:max_chars]


def _type_priority(item: MemoryItem, capability: str | None) -> int:
    if capability:
        priorities = CAPABILITY_TYPE_PRIORITY.get(capability, {})
        if item.memory_type in priorities:
            return priorities[item.memory_type]
    return TYPE_PRIORITY.get(item.memory_type, 99)


def _artifact_score(item: MemoryItem) -> float:
    return 0.1 if item.artifact_refs else 0.0


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


def _allows_recent_context_fallback(query: str) -> bool:
    """Allow recent-memory fallback only for explicit contextual follow-ups."""

    normalized = query.strip().lower()
    if not normalized:
        return False
    markers = (
        "继续",
        "接着",
        "上次",
        "刚才",
        "之前",
        "前面",
        "上一",
        "这个",
        "那个",
        "这些",
        "那些",
        "它",
        "它们",
        "这种",
        "那种",
        "该",
        "同款",
        "相似款",
    )
    return any(marker in normalized for marker in markers)
