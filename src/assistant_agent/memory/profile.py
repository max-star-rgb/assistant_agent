"""User profile memory helpers."""

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from assistant_agent.memory.facts import is_active_memory_fact
from assistant_agent.schemas.memory import MemoryItem


USER_PROFILE_MEMORY_ID = "user_profile"


class UserProfileMemory(BaseModel):
    """Compact semantic profile derived from explicit user memories."""

    user_id: str = Field(min_length=1)
    preferences: list[str] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)
    source_memory_ids: list[str] = Field(default_factory=list)
    updated_at: datetime

    @classmethod
    def empty(cls, user_id: str, now: datetime | None = None) -> "UserProfileMemory":
        return cls(user_id=user_id, updated_at=now or datetime.now(timezone.utc))

    @classmethod
    def from_memory_item(cls, item: MemoryItem) -> "UserProfileMemory":
        content = item.content
        preferences = _string_list(content.get("preferences"))
        facts = _string_list(content.get("facts"))
        source_memory_ids = _string_list(content.get("source_memory_ids"))
        return cls(
            user_id=item.user_id,
            preferences=preferences,
            facts=facts,
            source_memory_ids=source_memory_ids,
            updated_at=item.updated_at or item.created_at,
        )

    def merge_memory(self, item: MemoryItem, now: datetime | None = None) -> bool:
        """Merge a semantic memory into the profile and return whether it changed."""

        changed = False
        if not is_active_memory_fact(item):
            return False
        entry = item.summary.strip()
        if not entry:
            return False
        if item.memory_type == "preference":
            changed = _append_unique(self.preferences, entry) or changed
        elif item.memory_type in {"product", "task"}:
            changed = _append_unique(self.facts, entry) or changed
        else:
            return False
        changed = _append_unique(self.source_memory_ids, item.memory_id) or changed
        if changed:
            self.updated_at = now or datetime.now(timezone.utc)
        return changed

    def to_memory_item(self, *, session_id: str | None = None) -> MemoryItem:
        summary = _profile_summary(self)
        return MemoryItem(
            memory_id=USER_PROFILE_MEMORY_ID,
            user_id=self.user_id,
            session_id=session_id,
            memory_type="preference",
            content={
                "profile_version": 1,
                "preferences": self.preferences,
                "facts": self.facts,
                "source_memory_ids": self.source_memory_ids,
            },
            summary=summary,
            tags=["user_profile", "preference", "semantic"],
            source="user_profile",
            created_at=self.updated_at,
            updated_at=self.updated_at,
        )


def _profile_summary(profile: UserProfileMemory) -> str:
    parts: list[str] = []
    if profile.preferences:
        parts.append("偏好：" + "；".join(profile.preferences[:5]))
    if profile.facts:
        parts.append("事实：" + "；".join(profile.facts[:5]))
    return "用户画像：" + "；".join(parts) if parts else "用户画像：暂无稳定偏好。"


def _append_unique(values: list[str], value: str) -> bool:
    normalized = _normalize(value)
    if not normalized:
        return False
    if any(_normalize(existing) == normalized for existing in values):
        return False
    values.append(value)
    return True


def _normalize(value: str) -> str:
    return "".join(ch for ch in value.strip().lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]
