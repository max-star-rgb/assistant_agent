"""API request identity resolution helpers.

This module centralizes the current request-derived identity boundary without
turning local/demo routes into a real authentication system.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from assistant_agent.identity import RequestIdentity
from assistant_agent.api.trial_access import TrialAccessGate, TrialAccessStatus


ApiIdentitySource = Literal[
    "request_body",
    "path",
    "query",
    "websocket_query",
    "a2a_metadata",
    "auth_context",
    "local_context",
]
AuthContextSource = Literal["none", "test", "header", "jwt", "session"]
IdentityPolicyStatus = Literal["passed", "warning", "failed"]
IDENTITY_NOT_AUTH_BOUND_ERROR = "IDENTITY_NOT_AUTH_BOUND"


class AuthContext(BaseModel):
    """Trusted authentication context placeholder for API dependencies.

    The default dependency returns ``authenticated=False``. Future header/JWT
    integration should populate this model instead of changing route logic.
    """

    authenticated: bool = False
    source: AuthContextSource = "none"
    user_id: str | None = None
    session_id: str | None = None

    @classmethod
    def anonymous(cls) -> "AuthContext":
        """Return the default unauthenticated local/offline context."""

        return cls()


class ResolvedRequestIdentity(BaseModel):
    """Resolved request identity plus provenance for pilot-readiness checks."""

    identity: RequestIdentity
    source: ApiIdentitySource
    auth_bound: bool = False
    auth_context_source: str | None = None
    requested_user_id: str | None = None
    requested_session_id: str | None = None
    warnings: list[str] = Field(default_factory=list)

    def trial_access(self, gate: TrialAccessGate) -> TrialAccessStatus:
        """Check trial access against the resolved user id."""

        return gate.check(self.identity.user_id)

    def metadata(self) -> dict[str, object]:
        """Return safe provenance metadata for request traces/debug summaries."""

        return {
            "identity_source": self.source,
            "auth_bound_identity": self.auth_bound,
            "auth_context_source": self.auth_context_source,
            "requested_user_id": self.requested_user_id,
            "requested_session_id": self.requested_session_id,
            "warnings": list(self.warnings),
        }


class IdentityPolicyDecision(BaseModel):
    """Policy result for using a resolved identity in a runtime context."""

    status: IdentityPolicyStatus
    identity_source: str
    auth_bound_identity: bool
    production_required: bool = False
    local_bypass: bool = False
    warnings: list[str] = Field(default_factory=list)
    reason: str | None = None


class IdentityPolicyError(ValueError):
    """Stable identity policy failure for API protocol adapters."""

    def __init__(self, *, code: str, message: str, decision: IdentityPolicyDecision) -> None:
        super().__init__(message)
        self.code = code
        self.decision = decision

    def detail(self) -> dict[str, object]:
        """Return a redacted error detail suitable for HTTP/API responses."""

        return {
            "code": self.code,
            "message": str(self),
            "identity_policy": self.decision.model_dump(mode="json"),
        }


class IdentityPolicy:
    """Classify request-derived identity before pilot or production use."""

    def evaluate(
        self,
        resolution: ResolvedRequestIdentity | None = None,
        *,
        identity_source: str | None = None,
        auth_bound_identity: bool | None = None,
        production_required: bool = False,
        local_bypass: bool = False,
    ) -> IdentityPolicyDecision:
        source = identity_source or (resolution.source if resolution is not None else "unknown")
        auth_bound = bool(resolution.auth_bound if resolution is not None else auth_bound_identity)
        warnings = list(resolution.warnings if resolution is not None else [])
        if local_bypass:
            warnings.append("local_bypass")
        if auth_bound:
            return IdentityPolicyDecision(
                status="passed",
                identity_source=source,
                auth_bound_identity=True,
                production_required=production_required,
                local_bypass=local_bypass,
                warnings=warnings,
                reason="identity is bound to auth context",
            )
        if production_required:
            return IdentityPolicyDecision(
                status="failed",
                identity_source=source,
                auth_bound_identity=False,
                production_required=True,
                local_bypass=local_bypass,
                warnings=warnings or ["identity_not_auth_bound"],
                reason="production identity must come from auth context",
            )
        return IdentityPolicyDecision(
            status="warning",
            identity_source=source,
            auth_bound_identity=False,
            production_required=False,
            local_bypass=local_bypass,
            warnings=warnings or ["identity_not_auth_bound"],
            reason="identity is request-derived and acceptable only for local/offline or non-production pilot",
        )


def enforce_identity_policy(
    resolution: ResolvedRequestIdentity,
    *,
    production_required: bool = False,
    local_bypass: bool = False,
) -> IdentityPolicyDecision:
    """Raise when a resolved identity is not acceptable for the active policy."""

    decision = IdentityPolicy().evaluate(
        resolution,
        production_required=production_required,
        local_bypass=local_bypass,
    )
    if decision.status == "failed":
        raise IdentityPolicyError(
            code=IDENTITY_NOT_AUTH_BOUND_ERROR,
            message=decision.reason or "identity policy failed",
            decision=decision,
        )
    return decision


def resolve_request_identity(
    *,
    user_id: str,
    session_id: str | None = None,
    source: ApiIdentitySource,
    auth_context: AuthContext | None = None,
    auth_user_id: str | None = None,
    auth_session_id: str | None = None,
    strict_auth_match: bool = True,
) -> ResolvedRequestIdentity:
    """Resolve identity from request data or a future trusted auth context.

    Current local/offline API routes call this with request-derived user ids.
    Future authenticated routes can pass ``auth_user_id`` and keep the same
    service boundary.
    """

    auth_context_source = str(auth_context.source) if auth_context is not None else None
    if auth_context is not None and auth_context.authenticated:
        auth_user_id = auth_context.user_id
        auth_session_id = auth_context.session_id
    elif auth_user_id:
        auth_context_source = "test"

    requested_user_id = _clean_optional(user_id)
    requested_session_id = _clean_optional(session_id)
    trusted_user_id = _clean_optional(auth_user_id)
    trusted_session_id = _clean_optional(auth_session_id) or requested_session_id
    warnings: list[str] = []

    if trusted_user_id:
        if strict_auth_match and requested_user_id and requested_user_id != trusted_user_id:
            raise ValueError("request user_id does not match auth context")
        identity = RequestIdentity.for_user(
            user_id=trusted_user_id,
            session_id=trusted_session_id,
        )
        return ResolvedRequestIdentity(
            identity=identity,
            source="auth_context",
            auth_bound=True,
            auth_context_source=auth_context_source,
            requested_user_id=requested_user_id,
            requested_session_id=requested_session_id,
            warnings=warnings,
        )

    if not requested_user_id:
        raise ValueError("user_id is required")
    warnings.append("identity_not_auth_bound")
    identity = RequestIdentity.for_user(
        user_id=requested_user_id,
        session_id=requested_session_id,
    )
    return ResolvedRequestIdentity(
        identity=identity,
        source=source,
        auth_bound=False,
        auth_context_source=auth_context_source,
        requested_user_id=requested_user_id,
        requested_session_id=requested_session_id,
        warnings=warnings,
    )


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None
