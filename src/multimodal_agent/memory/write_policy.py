"""Memory write policy and lifecycle helpers."""

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from multimodal_agent.schemas.memory import MemoryItem, MemoryType
from multimodal_agent.services.provider_errors import sanitize_error_message


MemoryPromotionKind = Literal["episodic_memory", "long_term_memory"]


class MemoryPromotionCandidate(BaseModel):
    """A proposed memory write that still needs policy approval."""

    user_id: str
    session_id: str
    summary: str
    memory_type: MemoryType = "task"
    kind: MemoryPromotionKind = "episodic_memory"
    content: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    source: str = "memory_promotion_candidate"
    reason: str = ""
    user_intent_explicit: bool = False
    rejected_reason: str | None = None

    @model_validator(mode="after")
    def validate_candidate_payload(self) -> "MemoryPromotionCandidate":
        self.summary = sanitize_error_message(self.summary.strip())
        self.reason = sanitize_error_message(self.reason.strip())
        self.tags = [sanitize_error_message(tag) for tag in self.tags]
        unsafe_reason = _unsafe_payload_reason(
            {"summary": self.summary, "content": self.content, "tags": self.tags, "reason": self.reason}
        )
        if unsafe_reason and self.rejected_reason is None:
            self.rejected_reason = unsafe_reason
        return self


class MemoryWriteDecision(BaseModel):
    """Policy decision for a memory promotion candidate."""

    allowed: bool
    reason: str
    candidate: MemoryPromotionCandidate


class MemoryWritePolicy(BaseModel):
    """Local-first policy controlling what may be written to memory."""

    allow_session_summary_write: bool = True
    allow_long_term_promotion: bool = False
    require_user_intent_for_profile_memory: bool = True
    allow_auto_write: bool = False
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

    def evaluate_promotion_candidate(self, candidate: MemoryPromotionCandidate) -> MemoryWriteDecision:
        """Decide whether a generated candidate may become durable memory."""

        if candidate.rejected_reason:
            return MemoryWriteDecision(
                allowed=False,
                reason=candidate.rejected_reason,
                candidate=candidate,
            )
        if candidate.user_intent_explicit:
            return MemoryWriteDecision(
                allowed=True,
                reason="用户明确要求记住，允许写入长期记忆。",
                candidate=candidate,
            )
        if candidate.kind == "long_term_memory" and not self.allow_long_term_promotion:
            return MemoryWriteDecision(
                allowed=False,
                reason="默认禁止自动 long-term memory promotion。",
                candidate=candidate,
            )
        if candidate.memory_type == "preference" and self.require_user_intent_for_profile_memory:
            return MemoryWriteDecision(
                allowed=False,
                reason="profile/preference memory 需要用户明确意图。",
                candidate=candidate,
            )
        if not self.allow_auto_write:
            return MemoryWriteDecision(
                allowed=False,
                reason="默认禁止自动 memory write；候选仅供审计或显式确认。",
                candidate=candidate,
            )
        return MemoryWriteDecision(
            allowed=True,
            reason="policy 允许自动写入该候选。",
            candidate=candidate,
        )


def build_memory_promotion_candidate(
    *,
    user_id: str,
    session_id: str,
    summary: str,
    memory_type: MemoryType = "task",
    kind: MemoryPromotionKind = "episodic_memory",
    content: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    reason: str = "",
    user_intent_explicit: bool = False,
) -> MemoryPromotionCandidate:
    """Build a policy-gated candidate without writing it to a store."""

    return MemoryPromotionCandidate(
        user_id=user_id,
        session_id=session_id,
        summary=summary,
        memory_type=memory_type,
        kind=kind,
        content=content or {},
        tags=tags or [],
        reason=reason,
        user_intent_explicit=user_intent_explicit,
    )


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
    if not summary:
        raise ValueError("explicit memory requires non-empty text or summary")
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
    return cleaned


def _unsafe_payload_reason(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).lower()
            if normalized in {"api_key", "apikey", "authorization", "bearer", "cookie", "password", "secret", "token"}:
                return f"candidate contains sensitive key: {key}"
            if normalized in {
                "base64",
                "image_base64",
                "video_base64",
                "audio_base64",
                "raw",
                "raw_image",
                "raw_video",
                "raw_audio",
                "raw_media",
                "provider_response",
                "raw_provider_response",
            }:
                return f"candidate contains raw media/provider payload key: {key}"
            nested_reason = _unsafe_payload_reason(nested)
            if nested_reason:
                return nested_reason
        return None
    if isinstance(value, list):
        for nested in value:
            nested_reason = _unsafe_payload_reason(nested)
            if nested_reason:
                return nested_reason
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized.startswith(("data:image/", "data:video/", "data:audio/")):
            return "candidate contains inline media data"
        if "sk-" in normalized or "bearer " in normalized:
            return "candidate contains secret-like text"
    return None
