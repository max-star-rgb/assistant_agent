"""Request identity contracts used at service boundaries."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


DEFAULT_MEMORY_SCOPES = ("session", "task", "project", "user_profile", "video", "product")


class RequestIdentity(BaseModel):
    """Authenticated or request-derived identity for scoped service access."""

    tenant_id: str | None = None
    user_id: str = Field(min_length=1)
    project_id: str | None = None
    session_id: str | None = None
    allowed_scopes: list[str] = Field(default_factory=lambda: list(DEFAULT_MEMORY_SCOPES))

    @classmethod
    def for_user(
        cls,
        *,
        user_id: str,
        session_id: str | None = None,
        tenant_id: str | None = None,
        project_id: str | None = None,
        allowed_scopes: list[str] | None = None,
    ) -> "RequestIdentity":
        """Build an identity from trusted local/API path parameters."""

        return cls(
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            allowed_scopes=allowed_scopes or list(DEFAULT_MEMORY_SCOPES),
        )

    @classmethod
    def from_user_request(
        cls,
        request: Any,
        *,
        allowed_scopes: list[str] | None = None,
    ) -> "RequestIdentity":
        """Build an identity from the local mock/offline UserRequest boundary."""

        metadata = getattr(request, "metadata", {}) or {}
        project_id = metadata.get("project_id") if isinstance(metadata, dict) else None
        tenant_id = metadata.get("tenant_id") if isinstance(metadata, dict) else None
        session_id = getattr(request, "session_id", None)
        return cls.for_user(
            tenant_id=str(tenant_id) if tenant_id else None,
            user_id=str(getattr(request, "user_id")),
            project_id=str(project_id) if project_id else None,
            session_id=str(session_id) if session_id else None,
            allowed_scopes=allowed_scopes,
        )

    @field_validator("tenant_id", "project_id", "session_id", mode="before")
    @classmethod
    def _empty_optional_strings_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("allowed_scopes")
    @classmethod
    def _dedupe_allowed_scopes(cls, value: list[str]) -> list[str]:
        scopes = [scope.strip() for scope in value if scope.strip()]
        return list(dict.fromkeys(scopes))
