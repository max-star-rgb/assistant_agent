"""Public Assistant configuration and private run facts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from langgraph.runtime import Runtime
from pydantic import BaseModel, ConfigDict, Field


class AuthenticatedUserRequired(PermissionError):
    """A production operation requires Agent Server authenticated identity."""


class AssistantRunContext(BaseModel):
    """User-facing Assistant configuration supplied through Runtime.context."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    system_prompt: str = Field(
        default="",
        max_length=12_000,
        description=(
            "Assistant-specific identity, persona, and task preferences. "
            "Core safety and tool-governance rules remain authoritative."
        ),
        json_schema_extra={
            "langgraph_type": "prompt",
            "langgraph_nodes": ["fast_agent", "planning_agent"],
        },
    )
    assistant_execution_mode: Literal["planning"] | None = None


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
