"""Public Assistant configuration and private run facts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from langgraph.runtime import Runtime
from pydantic import BaseModel, ConfigDict, Field, model_validator


class AuthenticatedUserRequired(PermissionError):
    """A production operation requires Agent Server authenticated identity."""


class AssistantRunContext(BaseModel):
    """User-facing Assistant configuration supplied through Runtime.context."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    execution_mode: Literal["fast", "planning", "coding"] = Field(
        default="fast",
        description=(
            "Execution route for this Assistant or an overriding run context."
        ),
        json_schema_extra={"langgraph_nodes": ["execution_router"]},
    )
    enable_memory: bool = Field(
        default=True,
        description=(
            "Recall and refresh long-term memory for this Assistant or an "
            "overriding run context."
        ),
        json_schema_extra={
            "langgraph_nodes": ["memory_recall", "refresh_memory_extraction"]
        },
    )

ASSISTANT_RUNTIME_METADATA_KEY = "assistant_agent_runtime"


class AssistantRuntimeFacts(BaseModel):
    """Server-issued run facts that must not become Assistant configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    entry_profile: str = Field(default="agent_server", min_length=1, max_length=160)
    visual_capability_token: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    repository_snapshot_sha: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{40,64}$",
    )

    @model_validator(mode="after")
    def _async_worker_requires_snapshot(self) -> "AssistantRuntimeFacts":
        if self.entry_profile == "async_worker" and self.repository_snapshot_sha is None:
            raise ValueError("async worker requires repository snapshot sha")
        return self


def assistant_runtime_facts(config: Mapping[str, Any]) -> AssistantRuntimeFacts:
    """Read private runtime facts from namespaced RunnableConfig metadata."""

    metadata = config.get("metadata")
    payload = (
        metadata.get(ASSISTANT_RUNTIME_METADATA_KEY)
        if isinstance(metadata, Mapping)
        else None
    )
    if not isinstance(payload, Mapping):
        return AssistantRuntimeFacts()
    try:
        return AssistantRuntimeFacts.model_validate(dict(payload))
    except ValueError:
        return AssistantRuntimeFacts()


def assistant_runtime_metadata(
    facts: AssistantRuntimeFacts,
) -> dict[str, dict[str, object]]:
    """Build the namespaced metadata fragment used by trusted run adapters."""

    return {
        ASSISTANT_RUNTIME_METADATA_KEY: facts.model_dump(exclude_none=True),
    }


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
    "ASSISTANT_RUNTIME_METADATA_KEY",
    "AssistantRunContext",
    "AssistantRuntimeFacts",
    "AuthenticatedUserRequired",
    "assistant_runtime_facts",
    "assistant_runtime_metadata",
    "authenticated_user_identity",
]
