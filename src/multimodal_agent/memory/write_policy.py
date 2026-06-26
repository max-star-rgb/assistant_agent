"""Memory write policy and lifecycle helpers."""

from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field

from multimodal_agent.schemas.memory import MemoryItem, MemoryType
from multimodal_agent.services.provider_errors import sanitize_error_message


class MemoryWritePolicy(BaseModel):
    """Local-first policy controlling what may be written to memory."""

    auto_save_preferences: bool = True
    auto_save_artifacts: bool = True
    auto_save_task_summary: bool = True
    auto_save_raw_user_text: bool = False
    auto_save_media_raw: bool = False
    require_explicit_save_for_sensitive: bool = True
    ttl_days_by_type: dict[MemoryType, int | None] = Field(
        default_factory=lambda: {
            "conversation": 30,
            "task": 90,
            "preference": None,
            "artifact": 90,
            "product": 90,
            "image": 90,
            "video": 90,
            "generation": 90,
            "render": 90,
        }
    )

    def expires_at_for(self, memory_type: MemoryType, now: datetime | None = None) -> datetime | None:
        days = self.ttl_days_by_type.get(memory_type)
        if days is None:
            return None
        base = now or datetime.now(timezone.utc)
        return base + timedelta(days=days)


def build_task_summary_memory_item(
    *,
    memory_id: str,
    user_id: str,
    session_id: str,
    summary: str,
    intent: str | None,
    selected_tools: list[str],
    output_refs: list[str] | None = None,
    policy: MemoryWritePolicy | None = None,
    created_at: datetime | None = None,
) -> MemoryItem | None:
    """Build an auto-saved task summary without raw request/provider payloads."""

    resolved_policy = policy or MemoryWritePolicy()
    if not resolved_policy.auto_save_task_summary:
        return None
    redacted_summary = sanitize_error_message(summary)
    if resolved_policy.require_explicit_save_for_sensitive and redacted_summary != summary:
        return None
    now = created_at or datetime.now(timezone.utc)
    refs = output_refs or []
    content: dict[str, Any] = {
        "intent": intent,
        "selected_tools": selected_tools,
        "output_refs": refs,
    }
    return MemoryItem(
        memory_id=memory_id,
        user_id=user_id,
        session_id=session_id,
        memory_type="task",
        summary=redacted_summary,
        content=content,
        tags=["task_summary", *(selected_tools[:5])],
        source="agent_task_summary",
        artifact_refs=refs if resolved_policy.auto_save_artifacts else [],
        created_at=now,
        expires_at=resolved_policy.expires_at_for("task", now),
    )


def build_explicit_memory_item(
    *,
    memory_id: str,
    user_id: str,
    session_id: str,
    text: str,
    content: dict[str, Any] | None = None,
    policy: MemoryWritePolicy | None = None,
    created_at: datetime | None = None,
) -> MemoryItem:
    """Build a memory item for an explicit user 'remember this' request."""

    resolved_policy = policy or MemoryWritePolicy()
    now = created_at or datetime.now(timezone.utc)
    memory_type = _explicit_memory_type(text, content or {})
    summary = _explicit_summary(text, content or {})
    redacted_summary = sanitize_error_message(summary)
    safe_content: dict[str, Any] = {"explicit": True}
    for key in ("summary", "style", "budget", "product_ref", "product_id", "item", "output_ref"):
        value = (content or {}).get(key)
        if value:
            safe_content[key] = value
    if resolved_policy.auto_save_raw_user_text:
        safe_content["text"] = text
    output_ref = safe_content.get("output_ref")
    artifact_refs = [str(output_ref)] if output_ref and resolved_policy.auto_save_artifacts else []
    return MemoryItem(
        memory_id=memory_id,
        user_id=user_id,
        session_id=session_id,
        memory_type=memory_type,
        summary=redacted_summary,
        content=safe_content,
        tags=["explicit_remember", memory_type],
        source="explicit_user_request",
        artifact_refs=artifact_refs,
        created_at=now,
        expires_at=resolved_policy.expires_at_for(memory_type, now),
        sensitivity="sensitive" if redacted_summary != summary else "normal",
    )


def _explicit_memory_type(text: str, content: dict[str, Any]) -> MemoryType:
    joined = f"{text} {' '.join(str(value) for value in content.values())}"
    if any(
        keyword in joined
        for keyword in (
            "喜欢",
            "我爱",
            "最爱",
            "热爱",
            "爱好",
            "偏爱",
            "中意",
            "偏好",
            "风格",
            "预算",
            "以后",
            "优先",
            "常买",
            "常用",
            "关注",
            "收藏",
            "想要",
            "不喜欢",
            "不要",
        )
    ):
        return "preference"
    if any(
        keyword in joined.lower()
        for keyword in ("商品", "鞋", "包", "椅子", "产品", "玉桂狗", "cinnamoroll", "三丽鸥")
    ):
        return "product"
    return "task"


def _explicit_summary(text: str, content: dict[str, Any]) -> str:
    summary = content.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    cleaned = text.strip()
    for prefix in ("记住", "帮我记住", "保存"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip(" ：:")
            break
    return cleaned or "用户显式保存了一条记忆。"
