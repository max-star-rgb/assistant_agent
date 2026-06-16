"""Stable external API protocol schemas."""

from typing import Any

from pydantic import BaseModel, Field

from multimodal_agent.agent.state import AgentError, AgentState
from multimodal_agent.services.provider_errors import sanitize_error_detail, sanitize_error_message


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
    "provider_budget_exceeded": "PROVIDER_BUDGET_EXCEEDED",
    "provider_call_limit_exceeded": "PROVIDER_BUDGET_EXCEEDED",
    "provider_input_size_exceeded": "PROVIDER_BUDGET_EXCEEDED",
    "memory_unavailable": "MEMORY_UNAVAILABLE",
    "task_cancelled": "TASK_FAILED",
    "unknown_error": "INTERNAL_ERROR",
}

RECOVERABLE_CODES = {
    "INTENT_UNCLEAR",
    "INVALID_REQUEST",
    "PROVIDER_TIMEOUT",
    "PROVIDER_UNAVAILABLE",
    "PROVIDER_RATE_LIMITED",
}


class ApiError(BaseModel):
    """Stable error object exposed by HTTP and WebSocket APIs."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    detail: dict[str, Any] = Field(default_factory=dict)
    recoverable: bool = False


class AgentRunResponse(BaseModel):
    """Stable HTTP response wrapper for agent runs."""

    protocol_version: str = PROTOCOL_VERSION
    run_id: str
    trace_id: str
    status: str
    intent: str | None
    response_text: str
    data: dict[str, Any] = Field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[ApiError] = Field(default_factory=list)


def api_error_from_agent_error(error: AgentError) -> ApiError:
    """Convert internal AgentError to stable external ApiError."""

    internal_code = str(error.details.get("code", "unknown_error"))
    code = normalize_error_code(internal_code)
    return ApiError(
        code=code,
        message=sanitize_error_message(error.message),
        detail=_public_detail(error),
        recoverable=bool(error.details.get("retryable", False)) or code in RECOVERABLE_CODES,
    )


def api_error(
    code: str,
    message: str,
    detail: dict[str, Any] | None = None,
    recoverable: bool | None = None,
) -> ApiError:
    """Create a stable external ApiError."""

    normalized = normalize_error_code(code)
    return ApiError(
        code=normalized,
        message=sanitize_error_message(message),
        detail=sanitize_error_detail(detail or {}),
        recoverable=(normalized in RECOVERABLE_CODES) if recoverable is None else recoverable,
    )


def agent_run_response_from_state(state: AgentState) -> AgentRunResponse:
    """Convert AgentState into the stable HTTP response shape."""

    return AgentRunResponse(
        protocol_version=PROTOCOL_VERSION,
        run_id=state.run_id,
        trace_id=state.trace_id,
        status=state.status,
        intent=state.intent.intent if state.intent else None,
        response_text=state.response.message if state.response else "",
        data=state.response.data if state.response and state.response.data else {},
        tool_calls=[call.model_dump(mode="json") for call in state.tool_calls],
        tool_results=[result.model_dump(mode="json") for result in state.tool_results],
        errors=[api_error_from_agent_error(error) for error in state.errors],
    )


def normalize_error_code(code: str) -> str:
    """Normalize internal lower-case error codes to external API codes."""

    if code.isupper():
        return code
    return ERROR_CODE_MAP.get(code, "TASK_FAILED")


def _public_detail(error: AgentError) -> dict[str, Any]:
    allowed_keys = {"source", "step_id", "recovery_action", "optional_step", "retryable", "call_id"}
    detail = sanitize_error_detail({key: value for key, value in error.details.items() if key in allowed_keys})
    if error.source is not None:
        detail.setdefault("source", error.source)
    return detail
