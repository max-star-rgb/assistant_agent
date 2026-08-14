"""Trusted, serializable facts supplied to one native assistant run."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AssistantRunContext(BaseModel):
    """Identity and entry facts that are safe to expose through Runtime.context."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    user_id: str = Field(min_length=1, max_length=512)
    tenant_id: str = Field(min_length=1, max_length=512)
    entry_profile: str = Field(default="agent_server", min_length=1, max_length=160)
    media_capabilities: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("media_capabilities", mode="before")
    @classmethod
    def _normalize_media_capabilities(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value


__all__ = ["AssistantRunContext"]
