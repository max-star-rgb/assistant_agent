"""Shared policy for trace, provider, and tool-history content."""

from __future__ import annotations

import os
from collections.abc import Mapping


LOCAL_TRACE_CONTENT_ENV = "MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT"
LOCAL_PROVIDER_PROTOCOL_CAPTURE_ENV = "MULTIMODAL_AGENT_LOCAL_PROVIDER_PROTOCOL_CAPTURE"
LOCAL_MEMORY_TRACE_CONTENT_ENV = "MULTIMODAL_AGENT_LOCAL_MEMORY_TRACE_CONTENT"
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def local_trace_content_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether trace-content recording is enabled.

    Content capture is enabled by default for this local-first project. Set the
    environment value to ``0`` to request the reduced-content compatibility
    mode.
    """

    values = os.environ if env is None else env
    return values.get(LOCAL_TRACE_CONTENT_ENV) != "0"


def local_provider_protocol_capture_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether semantic Provider response capture is enabled."""

    values = os.environ if env is None else env
    return (
        local_trace_content_enabled(values)
        and values.get(LOCAL_PROVIDER_PROTOCOL_CAPTURE_ENV) != "0"
    )


def local_memory_trace_content_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether sensitive Mem0 changes may enter the local overlay."""

    values = os.environ if env is None else env
    value = values.get(LOCAL_MEMORY_TRACE_CONTENT_ENV)
    return (
        value is not None
        and value.strip().lower() in _TRUTHY_ENV_VALUES
    )
