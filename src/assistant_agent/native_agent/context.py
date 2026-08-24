"""Non-identity runtime configuration for the native assistant graph."""

from __future__ import annotations

from typing import Literal

from langgraph.runtime import Runtime
from pydantic import BaseModel, ConfigDict, Field, field_validator


class AuthenticatedUserRequired(PermissionError):
    """A production operation requires Agent Server authenticated identity."""


class AssistantRunContext(BaseModel):
    """Static run configuration supplied through LangGraph Runtime.context."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    entry_profile: str = Field(default="agent_server", min_length=1, max_length=160)
    media_capabilities: tuple[str, ...] = Field(default=(), max_length=32)
    realtime_media_mode: Literal["none", "video"] = "none"
    visual_capability_token: str | None = Field(default=None, min_length=1, max_length=64)
    assistant_execution_mode: Literal["planning"] | None = None

    @field_validator("media_capabilities", mode="before")
    @classmethod
    def _normalize_media_capabilities(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value


def authenticated_user_identity(runtime: Runtime[object]) -> str:
    """Read the sole user identity from LangGraph ServerInfo."""

    server_info = runtime.server_info
    user = server_info.user if server_info is not None else None
    identity = str(getattr(user, "identity", "")).strip()
    if not identity:
        raise AuthenticatedUserRequired(
            "Agent Server authenticated user identity is required."
        )
    return identity


__all__ = [
    "AssistantRunContext",
    "AuthenticatedUserRequired",
    "authenticated_user_identity",
]
