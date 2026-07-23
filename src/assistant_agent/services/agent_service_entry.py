"""Pure trust predicate for Agent-Service Gateway requests."""

from collections.abc import Mapping
from typing import Any, Protocol


AGENT_SERVICE_PROFILE_TOOL_NAMES = (
    "web_search",
    "weather",
    "shopping_search",
    "memory_search",
    "vision_understanding",
)


class RequestWithMetadata(Protocol):
    """Structural request type accepted by the trust predicate."""

    metadata: Mapping[str, Any]


def is_trusted_agent_service_request(
    request_or_metadata: RequestWithMetadata | Mapping[str, Any],
) -> bool:
    """Return whether transport and Gateway session profile prove Agent-Service entry."""

    metadata = (
        request_or_metadata
        if isinstance(request_or_metadata, Mapping)
        else request_or_metadata.metadata
    )
    if metadata.get("transport") != "agent_service_websocket":
        return False
    gateway = metadata.get("gateway")
    session_config = gateway.get("session_config") if isinstance(gateway, Mapping) else None
    return bool(
        isinstance(session_config, Mapping)
        and session_config.get("entry_profile") == "agent_service"
    )


def agent_service_tool_visibility() -> dict[str, Any]:
    """Return the trusted, structured tool boundary for Agent-Service turns."""

    names = list(AGENT_SERVICE_PROFILE_TOOL_NAMES)
    return {
        "profile": "agent_service",
        "allowed_tools": names,
        "configured_tools": names,
    }
