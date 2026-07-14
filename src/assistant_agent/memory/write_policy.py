"""Memory write policy and lifecycle helpers."""

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from assistant_agent.schemas.memory import MemoryItem, MemoryScope, MemoryType
from assistant_agent.services.provider_errors import sanitize_error_message


MemoryPromotionKind = Literal["episodic_memory", "long_term_memory"]
MemorySaveSourceIntent = Literal["user_explicit", "assistant_candidate", "user_confirmed"]
MemoryWriteDestination = Literal[
    "reject",
    "session_summary",
    "task_checkpoint",
    "project_memory",
    "user_profile",
    "video_memory",
    "product_memory",
]
MemoryWriteDecisionSensitivity = Literal["low", "medium", "high", "secret"]


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
        original_payload = {
            "summary": self.summary.strip(),
            "content": self.content,
            "tags": self.tags,
            "reason": self.reason.strip(),
            "source": self.source,
        }
        self.summary = sanitize_error_message(original_payload["summary"])
        self.reason = sanitize_error_message(original_payload["reason"])
        self.source = sanitize_error_message(self.source)
        self.tags = [sanitize_error_message(tag) for tag in self.tags]
        if not self.summary and self.rejected_reason is None:
            self.rejected_reason = "candidate requires non-empty summary"
        if _is_session_summary_candidate(self) and self.rejected_reason is None:
            self.rejected_reason = "session context_summary is session-scoped and must not be auto-promoted"
        unsafe_reason = _unsafe_payload_reason(original_payload)
        if unsafe_reason and self.rejected_reason is None:
            self.rejected_reason = unsafe_reason
        if (
            self.summary != original_payload["summary"]
            and original_payload["summary"]
            and self.rejected_reason is None
        ):
            self.rejected_reason = "candidate contains secret-like text"
        return self


class MemoryWriteDecision(BaseModel):
    """Policy decision for a proposed memory write."""

    allowed: bool
    destination: MemoryWriteDestination = "reject"
    reason: str
    require_user_confirmation: bool = False
    sensitivity: MemoryWriteDecisionSensitivity = "low"
    ttl_days: int | None = None
    redacted_payload: dict[str, Any] = Field(default_factory=dict)
    candidate: MemoryPromotionCandidate | None = None


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

        destination = _destination_for(memory_type=candidate.memory_type)
        redacted_payload = _redacted_candidate_payload(candidate, proposed_destination=destination)
        if candidate.rejected_reason:
            return MemoryWriteDecision(
                allowed=False,
                destination="reject",
                reason=candidate.rejected_reason,
                sensitivity=_sensitivity_for_rejection(candidate.rejected_reason),
                redacted_payload=redacted_payload,
                candidate=candidate,
            )
        if candidate.user_intent_explicit:
            return MemoryWriteDecision(
                allowed=True,
                destination=destination,
                reason="用户明确要求记住，允许写入长期记忆。",
                ttl_days=self.ttl_days_by_type.get(candidate.memory_type),
                redacted_payload=redacted_payload,
                candidate=candidate,
            )
        if candidate.kind == "long_term_memory" and not self.allow_long_term_promotion:
            return MemoryWriteDecision(
                allowed=False,
                destination="reject",
                reason="默认禁止自动 long-term memory promotion。",
                redacted_payload=redacted_payload,
                candidate=candidate,
            )
        if candidate.memory_type == "preference" and self.require_user_intent_for_profile_memory:
            return MemoryWriteDecision(
                allowed=False,
                destination="reject",
                reason="profile/preference memory 需要用户明确意图。",
                redacted_payload=redacted_payload,
                candidate=candidate,
            )
        if not self.allow_auto_write:
            return MemoryWriteDecision(
                allowed=False,
                destination="reject",
                reason="默认禁止自动 memory write；候选仅供审计或显式确认。",
                redacted_payload=redacted_payload,
                candidate=candidate,
            )
        return MemoryWriteDecision(
            allowed=True,
            destination=destination,
            reason="policy 允许自动写入该候选。",
            ttl_days=self.ttl_days_by_type.get(candidate.memory_type),
            redacted_payload=redacted_payload,
            candidate=candidate,
        )

    def evaluate_explicit_save(
        self,
        *,
        text: str,
        content: dict[str, Any] | None = None,
        scope: MemoryScope | None = None,
    ) -> MemoryWriteDecision:
        """Decide whether an explicit user save may become memory."""

        payload = content or {}
        memory_type = _explicit_memory_type(text, payload)
        summary = _explicit_summary(text, payload)
        destination = _destination_for(memory_type=memory_type, scope=scope)
        redacted_payload = _redacted_explicit_payload(
            summary=summary,
            memory_type=memory_type,
            content=payload,
            destination=destination,
            scope=scope,
            raw_text_stored=self.auto_save_raw_user_text,
        )
        if not summary:
            return MemoryWriteDecision(
                allowed=False,
                destination="reject",
                reason="explicit memory requires non-empty text or summary",
                redacted_payload=redacted_payload,
            )

        unsafe_reason = _unsafe_payload_reason({"text": text, "summary": summary, "content": payload})
        if unsafe_reason:
            return MemoryWriteDecision(
                allowed=False,
                destination="reject",
                reason=unsafe_reason,
                sensitivity="secret",
                redacted_payload=redacted_payload,
            )

        redacted_summary = sanitize_error_message(summary)
        if self.require_explicit_save_for_sensitive and redacted_summary != summary:
            return MemoryWriteDecision(
                allowed=False,
                destination="reject",
                reason="sensitive explicit memory requires user confirmation",
                require_user_confirmation=True,
                sensitivity="high",
                redacted_payload=redacted_payload,
            )

        return MemoryWriteDecision(
            allowed=True,
            destination=destination,
            reason="用户明确要求记住，允许写入。",
            ttl_days=self.ttl_days_by_type.get(memory_type),
            redacted_payload=redacted_payload,
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
    source: str = "memory_promotion_candidate",
    rejected_reason: str | None = None,
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
        source=source,
        reason=reason,
        user_intent_explicit=user_intent_explicit,
        rejected_reason=rejected_reason,
    )


def build_run_summary_promotion_candidate(
    *,
    user_id: str,
    session_id: str,
    summary: str,
    intent: str | None,
    selected_tools: list[str],
    output_refs: list[str] | None = None,
    policy: MemoryWritePolicy | None = None,
) -> MemoryPromotionCandidate | None:
    """Build a completed-run memory candidate without performing a write."""

    resolved_policy = policy or MemoryWritePolicy()
    if not resolved_policy.auto_save_task_summary:
        return None
    refs = [str(ref) for ref in output_refs or [] if str(ref).strip()]
    redacted_summary = sanitize_error_message(summary)
    rejected_reason = None
    if resolved_policy.require_explicit_save_for_sensitive and redacted_summary != summary:
        rejected_reason = "candidate contains secret-like text"
    return build_memory_promotion_candidate(
        user_id=user_id,
        session_id=session_id,
        summary=redacted_summary,
        memory_type="task",
        kind="episodic_memory",
        content={
            "intent": intent,
            "selected_tools": selected_tools[:8],
            "output_refs": refs,
        },
        tags=["run_summary", *selected_tools[:5]],
        source="agent_run_summary_candidate",
        reason="completed run summary candidate",
        rejected_reason=rejected_reason,
    )


def build_memory_item_from_promotion_candidate(
    *,
    memory_id: str,
    candidate: MemoryPromotionCandidate,
    policy: MemoryWritePolicy | None = None,
    created_at: datetime | None = None,
) -> MemoryItem | None:
    """Convert an allowed promotion candidate into a durable memory item."""

    resolved_policy = policy or MemoryWritePolicy()
    decision = resolved_policy.evaluate_promotion_candidate(candidate)
    if not decision.allowed:
        return None
    now = created_at or datetime.now(timezone.utc)
    output_refs = candidate.content.get("output_refs")
    artifact_refs = (
        [str(ref) for ref in output_refs if str(ref).strip()]
        if isinstance(output_refs, list) and resolved_policy.auto_save_artifacts
        else []
    )
    return MemoryItem(
        memory_id=memory_id,
        user_id=candidate.user_id,
        session_id=candidate.session_id,
        memory_type=candidate.memory_type,
        summary=candidate.summary,
        content={
            **candidate.content,
            "promotion_kind": candidate.kind,
        },
        tags=_unique(["memory_promotion", *candidate.tags]),
        source=candidate.source,
        artifact_refs=artifact_refs,
        reason=candidate.reason or None,
        created_at=now,
        expires_at=resolved_policy.expires_at_for(candidate.memory_type, now),
    )


def promotion_decision_audit_record(
    decision: MemoryWriteDecision,
    *,
    written_memory_id: str | None = None,
) -> dict[str, Any]:
    """Return a redacted audit record for traces/metadata."""

    candidate = decision.candidate
    if candidate is None:
        return {
            "allowed": decision.allowed,
            "destination": decision.destination,
            "written": written_memory_id is not None,
            "written_memory_id": written_memory_id,
            "reason": _clip(sanitize_error_message(decision.reason), 240),
            "require_user_confirmation": decision.require_user_confirmation,
            "sensitivity": decision.sensitivity,
            "ttl_days": decision.ttl_days,
            "redacted_payload": decision.redacted_payload,
        }
    return {
        "kind": candidate.kind,
        "memory_type": candidate.memory_type,
        "source": _clip(sanitize_error_message(candidate.source), 120),
        "summary": _clip(sanitize_error_message(candidate.summary), 200),
        "tags": [_clip(sanitize_error_message(tag), 80) for tag in candidate.tags[:8]],
        "allowed": decision.allowed,
        "destination": decision.destination,
        "written": written_memory_id is not None,
        "written_memory_id": written_memory_id,
        "reason": _clip(sanitize_error_message(decision.reason), 240),
        "user_intent_explicit": candidate.user_intent_explicit,
        "require_user_confirmation": decision.require_user_confirmation,
        "sensitivity": decision.sensitivity,
        "ttl_days": decision.ttl_days,
        "redacted_payload": decision.redacted_payload,
    }


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
    scope: MemoryScope | None = None,
    policy: MemoryWritePolicy | None = None,
    created_at: datetime | None = None,
) -> MemoryItem:
    """Build a memory item for an explicit user 'remember this' request."""

    resolved_policy = policy or MemoryWritePolicy()
    now = created_at or datetime.now(timezone.utc)
    payload = content or {}
    decision = resolved_policy.evaluate_explicit_save(text=text, content=payload, scope=scope)
    if not decision.allowed:
        raise ValueError(decision.reason)
    memory_type = _explicit_memory_type(text, payload)
    summary = _explicit_summary(text, payload)
    redacted_summary = sanitize_error_message(summary)
    safe_content: dict[str, Any] = {"explicit": True}
    for key in (
        "summary",
        "preference_key",
        "style",
        "budget",
        "product_ref",
        "product_id",
        "item",
        "output_ref",
        "consent",
        "confirmation_id",
        "fact",
        "fact_key",
        "fact_value",
        "subject",
        "predicate",
        "conflict_policy",
        "confidence",
    ):
        value = payload.get(key)
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
        scope=scope,
        memory_type=memory_type,
        summary=redacted_summary,
        content=safe_content,
        tags=["explicit_remember", memory_type],
        source="explicit_user_request",
        artifact_refs=artifact_refs,
        created_at=now,
        expires_at=resolved_policy.expires_at_for(memory_type, now),
        sensitivity=_item_sensitivity_for_decision(decision),
    )


def _destination_for(
    *,
    memory_type: MemoryType,
    scope: MemoryScope | str | None = None,
) -> MemoryWriteDestination:
    if scope == "project":
        return "project_memory"
    if memory_type == "conversation":
        return "session_summary"
    if memory_type == "preference":
        return "user_profile"
    if memory_type == "video":
        return "video_memory"
    if memory_type == "product":
        return "product_memory"
    return "task_checkpoint"


def _redacted_candidate_payload(
    candidate: MemoryPromotionCandidate,
    *,
    proposed_destination: MemoryWriteDestination,
) -> dict[str, Any]:
    return {
        "summary": _clip(sanitize_error_message(candidate.summary), 200),
        "memory_type": candidate.memory_type,
        "kind": candidate.kind,
        "source": _clip(sanitize_error_message(candidate.source), 120),
        "tags": [_clip(sanitize_error_message(tag), 80) for tag in candidate.tags[:8]],
        "proposed_destination": proposed_destination,
        "has_content": bool(candidate.content),
        "user_intent_explicit": candidate.user_intent_explicit,
    }


def _redacted_explicit_payload(
    *,
    summary: str,
    memory_type: MemoryType,
    content: dict[str, Any],
    destination: MemoryWriteDestination,
    scope: MemoryScope | str | None,
    raw_text_stored: bool,
) -> dict[str, Any]:
    safe_keys = {
        "summary",
        "preference_key",
        "style",
        "budget",
        "product_ref",
        "product_id",
        "item",
        "output_ref",
        "consent",
        "confirmation_id",
    }
    redacted_summary = sanitize_error_message(summary) if summary.strip() else ""
    return {
        "summary": _clip(redacted_summary, 200),
        "memory_type": memory_type,
        "destination": destination,
        "scope": scope,
        "content_keys": sorted(str(key) for key in content if str(key) in safe_keys),
        "raw_text_stored": raw_text_stored,
    }


def _item_sensitivity_for_decision(decision: MemoryWriteDecision) -> Literal["normal", "private", "sensitive"]:
    if decision.sensitivity == "low":
        return "normal"
    if decision.sensitivity == "medium":
        return "private"
    return "sensitive"


def _sensitivity_for_rejection(reason: str) -> MemoryWriteDecisionSensitivity:
    lowered = reason.lower()
    if (
        "secret" in lowered
        or "sensitive key" in lowered
        or "raw media/provider" in lowered
        or "inline media" in lowered
    ):
        return "secret"
    return "high"


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
                "raw_payload",
                "raw_html",
                "provider_response",
                "raw_provider_response",
                "raw_provider_payload",
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


def _is_session_summary_candidate(candidate: MemoryPromotionCandidate) -> bool:
    if candidate.source in {"context_summary", "session_context_summary"}:
        return True
    return any(str(key).lower() == "context_summary" for key in candidate.content)


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _clip(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return f"{value[: max_chars - 1]}…"
