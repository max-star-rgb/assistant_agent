"""Authentication dependency boundary for API routes.

Default local/offline behavior remains unauthenticated. Header-derived identity
is an explicit pilot mode and is disabled unless the matching environment flag
is enabled.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from fastapi import Request, WebSocket

from multimodal_agent.services.api_identity import AuthContext

AUTH_HEADER_ENABLED_ENV = "MULTIMODAL_AGENT_AUTH_HEADER_ENABLED"
AUTH_USER_ID_HEADER = "X-Multimodal-Agent-User-Id"
AUTH_SESSION_ID_HEADER = "X-Multimodal-Agent-Session-Id"
AUTH_TENANT_ID_HEADER = "X-Multimodal-Agent-Tenant-Id"
AUTH_PROJECT_ID_HEADER = "X-Multimodal-Agent-Project-Id"
AUTH_SCOPES_HEADER = "X-Multimodal-Agent-Scopes"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_SCOPE_SPLIT_RE = re.compile(r"[\s,;]+")


def header_auth_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether the local header-auth pilot is explicitly enabled."""

    source = os.environ if env is None else env
    return str(source.get(AUTH_HEADER_ENABLED_ENV, "")).strip().lower() in _TRUE_VALUES


def auth_context_from_headers(
    headers: Mapping[str, str],
    *,
    env: Mapping[str, str] | None = None,
) -> AuthContext:
    """Create an auth context from controlled headers when the pilot is enabled."""

    if not header_auth_enabled(env):
        return AuthContext.anonymous()

    user_id = _header_value(headers, AUTH_USER_ID_HEADER)
    if not user_id:
        return AuthContext.anonymous()

    return AuthContext(
        authenticated=True,
        source="header",
        user_id=user_id,
        session_id=_header_value(headers, AUTH_SESSION_ID_HEADER),
        tenant_id=_header_value(headers, AUTH_TENANT_ID_HEADER),
        project_id=_header_value(headers, AUTH_PROJECT_ID_HEADER),
        allowed_scopes=_parse_scopes(_header_value(headers, AUTH_SCOPES_HEADER)),
    )


def get_auth_context(request: Request) -> AuthContext:
    """Return the current trusted auth context.

    In the default local/offline profile this returns anonymous context and
    ignores auth-like headers. Header binding is only active in explicit pilot
    mode through ``MULTIMODAL_AGENT_AUTH_HEADER_ENABLED``.
    """

    return auth_context_from_headers(request.headers)


def get_websocket_auth_context(websocket: WebSocket) -> AuthContext:
    """Return the trusted auth context for WebSocket routes."""

    return auth_context_from_headers(websocket.headers)


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    raw_value = headers.get(name)
    if raw_value is None:
        lower_name = name.lower()
        for key, value in headers.items():
            if str(key).lower() == lower_name:
                raw_value = value
                break
    if raw_value is None:
        return None
    cleaned = str(raw_value).strip()
    return cleaned or None


def _parse_scopes(value: str | None) -> list[str] | None:
    if not value:
        return None
    scopes = [scope for scope in (_clean_scope(part) for part in _SCOPE_SPLIT_RE.split(value)) if scope]
    return scopes or None


def _clean_scope(value: str) -> str | None:
    cleaned = value.strip()
    return cleaned or None
