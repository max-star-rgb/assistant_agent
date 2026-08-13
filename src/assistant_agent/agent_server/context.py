"""Strict JSON context accepted by the deployed Assistant Graph."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentServerRunContext(BaseModel):
    """Trusted, serializable facts supplied to one native Agent Server run."""

    model_config = ConfigDict(extra="forbid", strict=True)

    user_id: str = Field(min_length=1, max_length=512)
    tenant_id: str = Field(min_length=1, max_length=512)
    assistant_mode: Literal["standard", "deep_research"] = "standard"
    entry_profile: str = Field(default="agent_server", min_length=1, max_length=160)
    media_capabilities: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("media_capabilities", mode="before")
    @classmethod
    def _normalize_json_array(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value


__all__ = ["AgentServerRunContext"]
