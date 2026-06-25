"""Memory audit service."""

from collections import Counter, defaultdict
from datetime import datetime, timezone

from multimodal_agent.memory.manager import MemoryManager
from multimodal_agent.memory.profile import USER_PROFILE_MEMORY_ID
from multimodal_agent.schemas.memory import MemoryItem, MemoryType
from multimodal_agent.schemas.memory_audit import (
    MemoryAuditItem,
    MemoryAuditList,
    MemoryAuditReport,
    MemoryDeleteResult,
    MemoryDuplicateGroup,
)


class MemoryAuditService:
    """User-scoped memory inspection and deletion helpers."""

    def __init__(self, memory_manager: MemoryManager) -> None:
        self.memory_manager = memory_manager

    def list_items(
        self,
        *,
        user_id: str,
        memory_type: MemoryType | None = None,
        include_content: bool = False,
    ) -> MemoryAuditList:
        items = self._filtered_items(user_id=user_id, memory_type=memory_type)
        return MemoryAuditList(
            user_id=user_id,
            total=len(items),
            items=[MemoryAuditItem.from_memory(item, include_content=include_content) for item in items],
        )

    def get_item(self, *, user_id: str, memory_id: str, include_content: bool = True) -> MemoryAuditItem | None:
        item = self.memory_manager.get(user_id, memory_id)
        if item is None:
            return None
        return MemoryAuditItem.from_memory(item, include_content=include_content)

    def delete_item(self, *, user_id: str, memory_id: str) -> MemoryDeleteResult:
        deleted = 1 if self.memory_manager.delete(user_id, memory_id) else 0
        return MemoryDeleteResult(user_id=user_id, deleted={"memory_items": deleted})

    def delete_session(self, *, user_id: str, session_id: str) -> MemoryDeleteResult:
        deleted = self.memory_manager.delete_by_session(user_id, session_id)
        return MemoryDeleteResult(user_id=user_id, deleted={"memory_items": deleted})

    def audit(self, *, user_id: str) -> MemoryAuditReport:
        items = self.memory_manager.list_by_user(user_id)
        by_type = Counter(item.memory_type for item in items)
        by_source = Counter(item.source for item in items)
        duplicate_groups = _duplicate_groups(items)
        expired_count = _expired_count(items)
        warnings: list[str] = []
        if duplicate_groups:
            warnings.append("potential_duplicate_memories")
        if expired_count:
            warnings.append("expired_memories_present")
        profile_present = any(item.memory_id == USER_PROFILE_MEMORY_ID and item.source == "user_profile" for item in items)
        if any(item.memory_type == "preference" for item in items) and not profile_present:
            warnings.append("user_profile_missing")

        return MemoryAuditReport(
            user_id=user_id,
            total=len(items),
            by_type=dict(by_type),
            by_source=dict(by_source),
            sensitive_count=sum(1 for item in items if item.sensitivity == "sensitive"),
            expired_count=expired_count,
            duplicate_groups=duplicate_groups,
            profile_present=profile_present,
            profile_memory_id=USER_PROFILE_MEMORY_ID if profile_present else None,
            warnings=warnings,
        )

    def _filtered_items(self, *, user_id: str, memory_type: MemoryType | None) -> list[MemoryItem]:
        items = self.memory_manager.list_by_user(user_id)
        if memory_type is not None:
            items = [item for item in items if item.memory_type == memory_type]
        return items


def _duplicate_groups(items: list[MemoryItem]) -> list[MemoryDuplicateGroup]:
    grouped: dict[tuple[MemoryType, str], list[MemoryItem]] = defaultdict(list)
    for item in items:
        if item.source == "user_profile":
            continue
        key = _dedupe_key(item)
        if not key:
            continue
        grouped[(item.memory_type, key)].append(item)

    duplicates: list[MemoryDuplicateGroup] = []
    for (memory_type, key), group_items in grouped.items():
        if len(group_items) < 2:
            continue
        duplicates.append(
            MemoryDuplicateGroup(
                key=key,
                memory_type=memory_type,
                memory_ids=[item.memory_id for item in group_items],
                summary=group_items[0].summary,
            )
        )
    return sorted(duplicates, key=lambda group: (group.memory_type, group.key))


def _expired_count(items: list[MemoryItem]) -> int:
    count = 0
    for item in items:
        if item.expires_at is None:
            continue
        now = datetime.now(tz=item.expires_at.tzinfo or timezone.utc)
        if item.expires_at < now:
            count += 1
    return count


def _dedupe_key(item: MemoryItem) -> str:
    return "".join(ch for ch in item.summary.strip().lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
