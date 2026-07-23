"""Authentication dependency boundary for API routes.

Default local/offline behavior remains unauthenticated. Header-derived identity
is an explicit pilot mode and is disabled unless the matching environment flag
is enabled.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal, Protocol

from fastapi import Request, WebSocket

from assistant_agent.services.api_identity import AuthContext

AUTH_MODE_ENV = "MULTIMODAL_AGENT_AUTH_MODE"
AUTH_HEADER_ENABLED_ENV = "MULTIMODAL_AGENT_AUTH_HEADER_ENABLED"
AUTH_REQUIRE_BOUND_IDENTITY_ENV = "MULTIMODAL_AGENT_REQUIRE_AUTH_BOUND_IDENTITY"
AUTH_USER_ID_HEADER = "X-Multimodal-Agent-User-Id"
AUTH_SESSION_ID_HEADER = "X-Multimodal-Agent-Session-Id"
AUTH_TENANT_ID_HEADER = "X-Multimodal-Agent-Tenant-Id"
AUTH_PROJECT_ID_HEADER = "X-Multimodal-Agent-Project-Id"
AuthMode = Literal["anonymous", "header_pilot", "trusted_header", "jwt", "session"]

_TRUE_VALUES = {"1", "true", "yes", "on"}
_AUTH_MODE_ALIASES = {
    "anonymous_local": "anonymous",
    "local": "anonymous",
}
_AUTH_MODES: set[str] = {"anonymous", "header_pilot", "trusted_header", "jwt", "session"}


class AuthProvider(Protocol):
    """Boundary for translating request metadata into trusted AuthContext."""

    mode: AuthMode

    def resolve(self, headers: Mapping[str, str]) -> AuthContext:
        """Return an auth context for one inbound request."""


class AnonymousAuthProvider:
    """Default local/offline provider that trusts no inbound auth metadata."""

    mode: AuthMode = "anonymous"

    def resolve(self, headers: Mapping[str, str]) -> AuthContext:
        _ = headers
        return AuthContext.anonymous()


class HeaderAuthProvider:
    """Header-based provider for explicit pilot or trusted reverse-proxy modes."""

    def __init__(self, *, mode: Literal["header_pilot", "trusted_header"]) -> None:
        self.mode: AuthMode = mode

    def resolve(self, headers: Mapping[str, str]) -> AuthContext:
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
        )


class DeferredAuthProvider:
    """Placeholder for future JWT/session providers; fails closed when auth is required."""

    def __init__(self, *, mode: Literal["jwt", "session"]) -> None:
        self.mode: AuthMode = mode

    def resolve(self, headers: Mapping[str, str]) -> AuthContext:
        _ = headers
        return AuthContext.anonymous()


def resolve_auth_mode(env: Mapping[str, str] | None = None) -> AuthMode:
    """Resolve the configured auth mode without enabling unsafe defaults."""

    source = os.environ if env is None else env
    raw_mode = str(source.get(AUTH_MODE_ENV, "")).strip().lower()
    raw_mode = _AUTH_MODE_ALIASES.get(raw_mode, raw_mode)
    if raw_mode in _AUTH_MODES:
        return raw_mode  # type: ignore[return-value]
    if _env_flag(source, AUTH_HEADER_ENABLED_ENV):
        return "header_pilot"
    return "anonymous"


def header_auth_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether the local header-auth pilot is explicitly enabled."""

    return resolve_auth_mode(env) == "header_pilot"


def require_auth_bound_identity(env: Mapping[str, str] | None = None) -> bool:
    """Return whether inbound routes must use an auth-bound identity."""

    source = os.environ if env is None else env
    return _env_flag(source, AUTH_REQUIRE_BOUND_IDENTITY_ENV)


def auth_provider_from_env(env: Mapping[str, str] | None = None) -> AuthProvider:
    """Return the auth provider configured for this process/request."""

    mode = resolve_auth_mode(env)
    if mode in {"header_pilot", "trusted_header"}:
        return HeaderAuthProvider(mode=mode)
    if mode in {"jwt", "session"}:
        return DeferredAuthProvider(mode=mode)
    return AnonymousAuthProvider()


def auth_context_from_headers(
    headers: Mapping[str, str],
    *,
    env: Mapping[str, str] | None = None,
) -> AuthContext:
    """Create an auth context from controlled headers when the pilot is enabled."""

    return auth_provider_from_env(env).resolve(headers)


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


def _env_flag(env: Mapping[str, str], name: str) -> bool:
    return str(env.get(name, "")).strip().lower() in _TRUE_VALUES
