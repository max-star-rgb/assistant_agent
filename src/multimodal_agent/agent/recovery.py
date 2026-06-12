"""Failure recovery policy for tool execution."""

from typing import Literal

from pydantic import BaseModel

from multimodal_agent.schemas.planning import TaskStep
from multimodal_agent.schemas.tools import ToolResult


RecoveryAction = Literal[
    "ask_followup",
    "skip_step",
    "retry_step",
    "fallback_to_mock",
    "fallback_to_text_response",
    "stop_with_error",
    "continue_with_partial_result",
]


class RecoveryDecision(BaseModel):
    """Structured decision produced for a failed tool execution."""

    error_code: str
    message: str
    action: RecoveryAction
    optional_step: bool = False
    retryable: bool = False


class RecoveryPolicy(BaseModel):
    """Decide whether a tool failure should stop or continue an agent run."""

    max_retries: int = 1
    allow_skip_optional_steps: bool = True
    allow_partial_response: bool = True
    fallback_to_mock: bool = False

    def decide(self, result: ToolResult, step: TaskStep | None = None) -> RecoveryDecision:
        """Return a recovery decision for a failed tool result."""

        optional_step = bool(step.optional) if step is not None else False
        error = result.error or "工具执行失败"
        error_code = classify_error(error)
        sanitized = sanitize_error_message(error)

        if optional_step and self.allow_skip_optional_steps and self.allow_partial_response:
            return RecoveryDecision(
                error_code=error_code,
                message=sanitized,
                action="continue_with_partial_result",
                optional_step=True,
                retryable=error_code in {"provider_timeout", "provider_rate_limited"},
            )

        return RecoveryDecision(
            error_code=error_code,
            message=sanitized,
            action="stop_with_error",
            optional_step=optional_step,
            retryable=error_code in {"provider_timeout", "provider_rate_limited"},
        )


def classify_error(error: str) -> str:
    """Map raw tool error text to a stable recovery error code."""

    normalized = error.strip().lower()
    if normalized.startswith("provider_unconfigured:"):
        return "provider_unconfigured"
    if "timeout" in normalized or "timed out" in normalized:
        return "provider_timeout"
    if "rate limit" in normalized or "rate_limited" in normalized:
        return "provider_rate_limited"
    if normalized.startswith("invalid input:") or "缺少" in normalized or "missing" in normalized:
        return "tool_input_invalid"
    if "not registered" in normalized:
        return "tool_not_found"
    if normalized.startswith("memory_unavailable:"):
        return "memory_unavailable"
    if normalized.startswith("provider_bad_response:"):
        return "provider_bad_response"
    return "unknown_error"


def sanitize_error_message(error: str) -> str:
    """Keep useful failure context while removing obvious secret material."""

    message = " ".join(error.strip().split())
    secret_markers = ("api_key", "apikey", "authorization", "bearer", "secret", "token", "password")
    parts = []
    redact_next = False
    for word in message.split(" "):
        lowered = word.lower()
        if redact_next or lowered.startswith(("sk-", "pk-")) or any(marker in lowered for marker in secret_markers):
            parts.append("[redacted]")
            redact_next = lowered in {"bearer", "authorization", "token", "password"}
        else:
            parts.append(word)
            redact_next = False
    sanitized = " ".join(parts)
    if len(sanitized) > 300:
        return f"{sanitized[:297]}..."
    return sanitized or "工具执行失败"
