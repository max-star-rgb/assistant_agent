"""Failure recovery policy for tool execution."""

from typing import Literal

from pydantic import BaseModel, Field

from multimodal_agent.schemas.planning import TaskStep
from multimodal_agent.schemas.tools import ToolResult
from multimodal_agent.services.provider_errors import normalize_provider_error_code, sanitize_error_message
from multimodal_agent.services.provider_policy import FallbackPolicy, RetryPolicy


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
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    fallback_policy: FallbackPolicy = Field(default_factory=FallbackPolicy)

    def decide(self, result: ToolResult, step: TaskStep | None = None) -> RecoveryDecision:
        """Return a recovery decision for a failed tool result."""

        optional_step = bool(step.optional) if step is not None else False
        error = result.error or "工具执行失败"
        error_code = classify_error(error)
        sanitized = sanitize_error_message(error)
        retryable = self.retry_policy.is_retryable(error_code)

        if optional_step and self.allow_skip_optional_steps and self.allow_partial_response:
            return RecoveryDecision(
                error_code=error_code,
                message=sanitized,
                action="continue_with_partial_result",
                optional_step=True,
                retryable=retryable,
            )

        return RecoveryDecision(
            error_code=error_code,
            message=sanitized,
            action="stop_with_error",
            optional_step=optional_step,
            retryable=retryable,
        )


def classify_error(error: str) -> str:
    """Map raw tool error text to a stable recovery error code."""

    normalized = error.strip().lower()
    prefix = normalized.split(":", maxsplit=1)[0]
    provider_code = normalize_provider_error_code(prefix)
    if provider_code.startswith("provider_") and provider_code != "provider_unknown_error":
        return provider_code
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
