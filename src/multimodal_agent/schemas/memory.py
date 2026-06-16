"""Memory schemas."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from multimodal_agent.schemas.capability_output import CapabilityOutputContract
from multimodal_agent.services.provider_errors import sanitize_error_message


MemoryType = Literal[
    "conversation",
    "video",
    "image",
    "product",
    "preference",
    "artifact",
    "task",
    "generation",
    "render",
]

MemorySensitivity = Literal["normal", "private", "sensitive"]

_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "cookie",
    "password",
    "secret",
    "token",
}
_RAW_PAYLOAD_KEYS = {
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
}


class MemoryItem(BaseModel):
    """A retrievable memory item with an explainable match score."""

    memory_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    session_id: str | None = None
    memory_type: MemoryType
    content: dict[str, Any] = Field(default_factory=dict)
    summary: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    source: str = Field(default="agent", min_length=1)
    artifact_refs: list[str] = Field(default_factory=list)
    relevance: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    sensitivity: MemorySensitivity = "normal"

    @model_validator(mode="after")
    def validate_memory_payload(self) -> "MemoryItem":
        """Reject unsafe raw payloads; keep artifact bodies behind refs."""

        _reject_unsafe_payload(self.content)
        _reject_unsafe_payload({"tags": self.tags, "artifact_refs": self.artifact_refs, "summary": self.summary})
        self.summary = sanitize_error_message(self.summary)
        self.reason = sanitize_error_message(self.reason) if self.reason else None
        self.content = _sanitize_payload(self.content)
        self.tags = [sanitize_error_message(tag) for tag in self.tags]
        return self


class MemoryQuery(BaseModel):
    """Query options for local memory retrieval."""

    user_id: str = Field(min_length=1)
    session_id: str | None = None
    query: str = ""
    capability: str | None = None
    memory_types: list[MemoryType] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    top_k: int = Field(default=5, ge=1, le=50)
    max_context_chars: int = Field(default=500, ge=50, le=4000)
    since: datetime | None = None
    include_expired: bool = False


class MemorySearchResult(BaseModel):
    """Structured memory search response for tools, API, and planner context."""

    items: list[MemoryItem] = Field(default_factory=list)
    query_used: MemoryQuery
    total: int = Field(ge=0)
    ranking_reason: str = ""
    memory_context: str = ""
    errors: list[dict[str, Any]] = Field(default_factory=list)


def memory_item_from_capability_contract(
    *,
    memory_id: str,
    user_id: str,
    contract: CapabilityOutputContract | dict[str, Any],
    summary: str | None = None,
    session_id: str | None = None,
    tags: list[str] | None = None,
    created_at: datetime,
) -> MemoryItem:
    """Create a safe memory item from a public capability output contract."""

    payload = contract.model_dump(mode="json") if isinstance(contract, CapabilityOutputContract) else contract
    capability = str(payload.get("capability") or "unknown")
    status = str(payload.get("status") or "unknown")
    output_ref = payload.get("output_ref")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    artifact_refs = [str(output_ref)] if output_ref else []
    item_summary = summary or str(data.get("summary") or f"{capability} {status}")
    content = {
        "capability": capability,
        "status": status,
        "output_ref": output_ref,
        "summary": item_summary,
    }
    return MemoryItem(
        memory_id=memory_id,
        user_id=user_id,
        session_id=session_id,
        memory_type=_memory_type_from_capability(capability),
        content=content,
        summary=item_summary,
        tags=tags or [capability, status],
        source="capability_output",
        artifact_refs=artifact_refs,
        created_at=created_at,
    )


def _memory_type_from_capability(capability: str) -> MemoryType:
    if capability in {"image_generation", "image_understanding"}:
        return "image"
    if capability == "video_understanding":
        return "video"
    if capability in {"product_search", "price_compare"}:
        return "product"
    if capability == "render_3d":
        return "render"
    if capability == "memory_save":
        return "task"
    return "artifact"


def _reject_unsafe_payload(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).lower()
            if normalized in _SENSITIVE_KEYS:
                raise ValueError(f"memory payload contains sensitive key: {key}")
            if normalized in _RAW_PAYLOAD_KEYS:
                raise ValueError(f"memory payload contains raw media/provider payload key: {key}")
            _reject_unsafe_payload(nested)
        return
    if isinstance(value, list):
        for nested in value:
            _reject_unsafe_payload(nested)
        return
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized.startswith(("data:image/", "data:video/", "data:audio/")):
            raise ValueError("memory payload contains inline media data")


def _sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sanitize_payload(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [_sanitize_payload(nested) for nested in value]
    if isinstance(value, str):
        return sanitize_error_message(value)
    return value
