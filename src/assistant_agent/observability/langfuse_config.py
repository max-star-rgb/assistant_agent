"""Shared defaults for the repository's local Langfuse development stack."""

from __future__ import annotations

import base64
from collections.abc import Mapping


DEFAULT_LOCAL_LANGFUSE_HOST = "http://localhost:3000"
DEFAULT_LOCAL_LANGFUSE_OTLP_PATH = "/api/public/otel/v1/traces"
LANGFUSE_HOST_ENV = "LANGFUSE_HOST"
LANGFUSE_PUBLIC_KEY_ENV = "LANGFUSE_PUBLIC_KEY"
LANGFUSE_SECRET_KEY_ENV = "LANGFUSE_SECRET_KEY"
ASSISTANT_AGENT_LANGFUSE_PUBLIC_KEY_ENV = "ASSISTANT_AGENT_LANGFUSE_PUBLIC_KEY"
ASSISTANT_AGENT_LANGFUSE_SECRET_KEY_ENV = "ASSISTANT_AGENT_LANGFUSE_SECRET_KEY"


def langfuse_host_from_env(values: Mapping[str, str]) -> str:
    """Resolve an optional host override, otherwise use the local stack."""

    return _first_non_empty(values, LANGFUSE_HOST_ENV) or DEFAULT_LOCAL_LANGFUSE_HOST


def langfuse_credentials_from_env(
    values: Mapping[str, str],
) -> tuple[str | None, str | None]:
    """Resolve shared Langfuse credentials without requiring duplicate variables."""

    return (
        _first_non_empty(
            values,
            ASSISTANT_AGENT_LANGFUSE_PUBLIC_KEY_ENV,
            LANGFUSE_PUBLIC_KEY_ENV,
        ),
        _first_non_empty(
            values,
            ASSISTANT_AGENT_LANGFUSE_SECRET_KEY_ENV,
            LANGFUSE_SECRET_KEY_ENV,
        ),
    )


def langfuse_authorization_headers(values: Mapping[str, str]) -> dict[str, str]:
    """Build the local OTLP Basic authorization header from Langfuse keys."""

    public_key, secret_key = langfuse_credentials_from_env(values)
    if not public_key or not secret_key:
        return {}
    credentials = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode("ascii")
    return {"Authorization": f"Basic {credentials}"}


def default_langfuse_trace_endpoint(values: Mapping[str, str]) -> str:
    """Return the Langfuse OTLP trace endpoint for the resolved host."""

    return f"{langfuse_host_from_env(values).rstrip('/')}{DEFAULT_LOCAL_LANGFUSE_OTLP_PATH}"


def _first_non_empty(values: Mapping[str, str], *keys: str) -> str | None:
    for key in keys:
        value = values.get(key)
        if value is not None and value.strip():
            return value.strip()
    return None
