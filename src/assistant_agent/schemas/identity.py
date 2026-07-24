"""Request identity contracts used at service boundaries."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from assistant_agent.schemas.agent_communication import DEFAULT_AGENT_ID


class RequestIdentity(BaseModel):
    """Minimal trusted identity shared by runtime and Mem0."""

    user_id: str = Field(min_length=1)
    agent_id: str = Field(default=DEFAULT_AGENT_ID, min_length=1)
    session_id: str | None = None

    @classmethod
    def for_user(
        cls,
        *,
        user_id: str,
        agent_id: str = DEFAULT_AGENT_ID,
        session_id: str | None = None,
    ) -> "RequestIdentity":
        """Build an identity from trusted local/API path parameters."""

        return cls(
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
        )

    @classmethod
    def from_user_request(
        cls,
        request: Any,
        *,
        agent_id: str = DEFAULT_AGENT_ID,
    ) -> "RequestIdentity":
        """Build an identity from the local mock/offline UserRequest boundary."""

        session_id = getattr(request, "session_id", None)
        return cls.for_user(
            user_id=str(getattr(request, "user_id")),
            agent_id=agent_id,
            session_id=str(session_id) if session_id else None,
        )

    @field_validator("session_id", mode="before")
    @classmethod
    def _empty_optional_strings_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value
