"""External request contract for optional multi-agent routing."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.providers.provider_errors import (
    sanitize_error_detail,
    sanitize_error_message,
)
from assistant_agent.runtime.citations import UrlCitationAnnotation
from assistant_agent.runtime.requests import UserRequest


PROTOCOL_VERSION = "v1"
ERROR_CODE_MAP = {
    "intent_unclear": "INTENT_UNCLEAR",
    "missing_required_input": "INVALID_REQUEST",
    "tool_not_found": "TOOL_NOT_FOUND",
    "tool_input_invalid": "TOOL_INPUT_INVALID",
    "provider_unconfigured": "PROVIDER_UNCONFIGURED",
    "provider_missing_api_key": "PROVIDER_UNCONFIGURED",
    "provider_missing_base_url": "PROVIDER_UNCONFIGURED",
    "provider_invalid_config": "PROVIDER_UNCONFIGURED",
    "provider_request_invalid": "INVALID_REQUEST",
    "provider_request_too_large": "INVALID_REQUEST",
    "provider_unsupported_input": "INVALID_REQUEST",
    "provider_unsupported_format": "INVALID_REQUEST",
    "provider_timeout": "PROVIDER_TIMEOUT",
    "provider_network_error": "PROVIDER_UNAVAILABLE",
    "provider_unavailable": "PROVIDER_UNAVAILABLE",
    "provider_bad_gateway": "PROVIDER_UNAVAILABLE",
    "provider_auth_failed": "PROVIDER_AUTH_FAILED",
    "provider_permission_denied": "PROVIDER_AUTH_FAILED",
    "provider_rate_limited": "PROVIDER_RATE_LIMITED",
    "provider_bad_response": "TASK_FAILED",
    "provider_empty_response": "TASK_FAILED",
    "provider_schema_mismatch": "TASK_FAILED",
    "provider_execution_failed": "TASK_FAILED",
    "provider_cancelled": "TASK_FAILED",
    "provider_unknown_error": "TASK_FAILED",
    "memory_unavailable": "MEMORY_UNAVAILABLE",
    "agent_not_found": "AGENT_NOT_FOUND",
    "agent_disabled": "AGENT_DISABLED",
    "agent_route_failed": "AGENT_ROUTE_FAILED",
    "agent_route_ambiguous": "AGENT_ROUTE_AMBIGUOUS",
    "agent_capability_not_found": "AGENT_NOT_FOUND",
    "agent_runtime_not_found": "AGENT_NOT_FOUND",
    "agent_transport_unavailable": "AGENT_TRANSPORT_UNAVAILABLE",
    "agent_transport_failed": "AGENT_TRANSPORT_FAILED",
    "agent_delegation_depth_exceeded": "AGENT_DELEGATION_DEPTH_EXCEEDED",
    "agent_run_cancelled": "AGENT_RUN_CANCELLED",
    "task_cancelled": "AGENT_RUN_CANCELLED",
    "unknown_error": "INTERNAL_ERROR",
}
RECOVERABLE_CODES = {
    "INTENT_UNCLEAR",
    "INVALID_REQUEST",
    "PROVIDER_TIMEOUT",
    "PROVIDER_UNAVAILABLE",
    "PROVIDER_RATE_LIMITED",
    "AGENT_NOT_FOUND",
    "AGENT_DISABLED",
    "AGENT_ROUTE_FAILED",
    "AGENT_ROUTE_AMBIGUOUS",
    "AGENT_TRANSPORT_UNAVAILABLE",
}


class ApiError(BaseModel):
    """Stable error object exposed by optional multi-agent protocols."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    detail: dict[str, Any] = Field(default_factory=dict)
    recoverable: bool = False


class AgentRunResponse(BaseModel):
    """Stable response wrapper used by the optional local agent router."""

    protocol_version: str = PROTOCOL_VERSION
    run_id: str
    trace_id: str
    status: str
    response_text: str
    annotations: list[UrlCitationAnnotation] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    react_steps: list[dict[str, Any]] = Field(default_factory=list)
    decision_trace: list[dict[str, Any]] = Field(default_factory=list)
    runtime_info: dict[str, Any] = Field(default_factory=dict)
    current_stage: str | None = None
    blocked_reason: str | None = None
    errors: list[ApiError] = Field(default_factory=list)


def api_error(
    code: str,
    message: str,
    detail: dict[str, Any] | None = None,
    recoverable: bool | None = None,
) -> ApiError:
    """Create a stable multi-agent protocol error."""

    normalized = normalize_error_code(code)
    return ApiError(
        code=normalized,
        message=sanitize_error_message(message),
        detail=sanitize_error_detail(detail or {}),
        recoverable=(normalized in RECOVERABLE_CODES) if recoverable is None else recoverable,
    )


def normalize_error_code(code: str) -> str:
    """Normalize internal lower-case error codes to protocol codes."""

    if code.isupper():
        return code
    return ERROR_CODE_MAP.get(code, "TASK_FAILED")


AgentCollaborationMode = Literal["single", "controller_delegate"]
AgentRouteReason = Literal[
    "explicit_target_agent_id",
    "capability_match",
    "routing_table",
    "controller_delegate_default",
    "default_agent",
]
AgentRouteStatus = Literal["routed", "failed"]


class AgentRouteDelegatedTaskSummary(BaseModel):
    """Public summary for one delegated child task."""

    task_id: str | None = None
    target_agent_id: str | None = None
    status: str | None = None
    run_id: str | None = None
    trace_id: str | None = None
    artifact_count: int = 0
    error_codes: list[str] = Field(default_factory=list)


class AgentRouteDecision(BaseModel):
    """Deterministic route decision exposed by the agent router control plane."""

    selected_agent_id: str | None = None
    requested_target_agent_id: str | None = None
    requested_capability: str | None = None
    collaboration_mode: AgentCollaborationMode
    reason: AgentRouteReason
    status: AgentRouteStatus
    delegation_enabled: bool = False
    error_code: str | None = None
    error_message: str | None = None


class AgentRouteMetadata(BaseModel):
    """Stable router metadata embedded in AgentRunResponse data/runtime_info."""

    route_decision: AgentRouteDecision
    delegated_tasks: list[AgentRouteDelegatedTaskSummary] = Field(default_factory=list)
    route: dict[str, Any] | None = None

    def public_payload(self) -> dict[str, Any]:
        """Return metadata with flat keys retained for compatibility."""

        payload = self.model_dump(mode="json")
        decision = self.route_decision
        payload.update(
            {
                "agent_id": decision.selected_agent_id,
                "collaboration_mode": decision.collaboration_mode,
                "target_agent_id": decision.requested_target_agent_id,
                "capability": decision.requested_capability,
                "delegation_enabled": decision.delegation_enabled,
            }
        )
        return payload


class AgentRouteRequest(UserRequest):
    """Request accepted by the optional `/agents/run` route entrypoint."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "user_id": "demo_user",
                    "session_id": "demo_session",
                    "text": "Summarize this with the worker agent.",
                    "target_agent_id": "agent.worker",
                    "collaboration_mode": "single",
                },
                {
                    "user_id": "demo_user",
                    "session_id": "demo_session",
                    "text": "Coordinate this task and delegate if useful.",
                    "collaboration_mode": "controller_delegate",
                },
            ]
        }
    )

    target_agent_id: str | None = None
    capability: str | None = None
    collaboration_mode: AgentCollaborationMode = "single"
    mode: AgentCollaborationMode | None = Field(
        default=None,
        description="Compatibility alias for collaboration_mode.",
    )

    def effective_collaboration_mode(self) -> AgentCollaborationMode:
        return self.mode or self.collaboration_mode

    def to_user_request(self, *, metadata: dict[str, Any] | None = None) -> UserRequest:
        """Drop router-only fields before entering an agent invocation endpoint."""

        return UserRequest(
            user_id=self.user_id,
            session_id=self.session_id,
            text=self.text,
            image_ids=list(self.image_ids),
            video_ids=list(self.video_ids),
            audio_id=self.audio_id,
            metadata=dict(self.metadata if metadata is None else metadata),
        )
