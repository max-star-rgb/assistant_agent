"""Shared opt-in policy for local trace and tool-history content."""

from __future__ import annotations

import os
from collections.abc import Mapping


LOCAL_TRACE_CONTENT_ENV = "MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT"


def local_trace_content_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether explicit local trace-content recording is enabled."""

    values = os.environ if env is None else env
    return values.get(LOCAL_TRACE_CONTENT_ENV) == "1"
