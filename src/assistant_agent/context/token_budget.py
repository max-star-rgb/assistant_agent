"""Optional token-aware context budget reporting."""

from __future__ import annotations

import json
from dataclasses import dataclass
from math import ceil
from typing import Any

from pydantic import BaseModel, Field

from assistant_agent.runtime.requests import UserRequest


@dataclass(frozen=True)
class ContextWindowDecision:
    """Preflight decision for one fully compiled Provider request."""

    input_tokens: int
    effective_input_limit: int
    usage_ratio: float
    target_tokens: int
    triggered: bool
    hard: bool


@dataclass(frozen=True)
class ContextWindowPolicy:
    """Token thresholds with hysteresis for rolling context compaction."""

    input_token_limit: int
    trigger_ratio: float = 0.70
    target_ratio: float = 0.40
    hard_ratio: float = 0.85
    safety_margin_tokens: int = 0
    summary_max_tokens: int = 32_768

    def evaluate(
        self,
        input_tokens: int,
        *,
        reserved_output_tokens: int = 0,
    ) -> ContextWindowDecision:
        effective_limit = max(
            1,
            self.input_token_limit
            - max(0, self.safety_margin_tokens)
            - max(0, reserved_output_tokens),
        )
        ratio = max(0, input_tokens) / effective_limit
        return ContextWindowDecision(
            input_tokens=max(0, input_tokens),
            effective_input_limit=effective_limit,
            usage_ratio=ratio,
            target_tokens=max(1, int(effective_limit * self.target_ratio)),
            triggered=ratio >= self.trigger_ratio,
            hard=ratio >= self.hard_ratio,
        )


CONTEXT_TOKEN_ESTIMATE_METADATA_KEYS = (
    "context_budget_estimate_tokens",
    "estimate_context_tokens",
    "context_token_estimation_enabled",
)
CONTEXT_TOKEN_MAX_METADATA_KEYS = (
    "context_budget_max_tokens",
    "context_token_budget_max_tokens",
)
CONTEXT_TOKEN_USAGE_METADATA_KEYS = (
    "context_token_usage",
    "provider_token_usage",
    "last_chat_usage",
)


class TokenBudgetEstimate(BaseModel):
    """Token budget fields merged into `ContextBudgetReport`."""

    request_tokens: int = Field(default=0, ge=0)
    conversation_tokens: int = Field(default=0, ge=0)
    memory_tokens: int = Field(default=0, ge=0)
    realtime_video_context_tokens: int = Field(default=0, ge=0)
    durable_task_state_tokens: int = Field(default=0, ge=0)
    plan_tokens: int = Field(default=0, ge=0)
    observations_tokens: int = Field(default=0, ge=0)
    tool_spec_tokens: int = Field(default=0, ge=0)
    owner_persona_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    max_tokens: int = Field(default=0, ge=0)
    token_usage_ratio: float = Field(default=0.0, ge=0.0)
    token_budget_source: str = "none"
    provider_prompt_tokens: int = Field(default=0, ge=0)
    provider_completion_tokens: int = Field(default=0, ge=0)
    provider_total_tokens: int = Field(default=0, ge=0)


class TokenBudgetReporter:
    """Estimate context tokens or normalize provider token usage metadata."""

    def __init__(self, *, chars_per_token: float = 4.0) -> None:
        self.chars_per_token = max(chars_per_token, 1.0)

    def report(
        self,
        *,
        request: UserRequest,
        sections: dict[str, Any],
    ) -> TokenBudgetEstimate:
        """Return optional token budget data without changing char budget behavior."""

        max_tokens = _metadata_int(request, CONTEXT_TOKEN_MAX_METADATA_KEYS)
        usage = _provider_usage(request)
        if usage:
            return self._provider_usage_report(usage, max_tokens=max_tokens)
        if not _estimation_enabled(request) and max_tokens <= 0:
            return TokenBudgetEstimate()
        section_tokens = {
            f"{name}_tokens": self.estimate(value)
            for name, value in sections.items()
        }
        total_tokens = sum(section_tokens.values())
        return TokenBudgetEstimate(
            **section_tokens,
            total_tokens=total_tokens,
            max_tokens=max_tokens,
            token_usage_ratio=total_tokens / max_tokens if max_tokens > 0 else 0.0,
            token_budget_source="estimated",
        )

    def estimate(self, value: Any) -> int:
        """Return a deterministic local token estimate for prompt material."""

        text = _stringify(value)
        if not text:
            return 0
        cjk_chars = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
        other_chars = len(text) - cjk_chars
        return cjk_chars + ceil(other_chars / self.chars_per_token)

    def _provider_usage_report(self, usage: dict[str, Any], *, max_tokens: int) -> TokenBudgetEstimate:
        normalized = normalize_provider_token_usage(usage)
        prompt_tokens = normalized.get("prompt_tokens", 0)
        completion_tokens = normalized.get("completion_tokens", 0)
        total_usage_tokens = normalized.get("total_tokens", 0)
        return TokenBudgetEstimate(
            total_tokens=0,
            max_tokens=max_tokens,
            token_usage_ratio=0.0,
            token_budget_source="previous_provider_usage",
            provider_prompt_tokens=prompt_tokens,
            provider_completion_tokens=completion_tokens,
            provider_total_tokens=total_usage_tokens,
        )


def token_budget_reporter_from_request(request: UserRequest) -> TokenBudgetReporter | None:
    """Return a reporter only when token reporting is explicitly useful."""

    if _provider_usage(request) or _estimation_enabled(request) or _metadata_int(request, CONTEXT_TOKEN_MAX_METADATA_KEYS) > 0:
        return TokenBudgetReporter()
    return None


def normalize_provider_token_usage(usage: dict[str, Any]) -> dict[str, int]:
    """Return only safe provider token counters from an arbitrary usage payload."""

    prompt_tokens = _usage_int(usage, ("prompt_tokens", "input_tokens", "input_token_count"))
    completion_tokens = _usage_int(usage, ("completion_tokens", "output_tokens", "output_token_count"))
    total_tokens = _usage_int(usage, ("total_tokens", "total_token_count"))
    if total_tokens <= 0 and (prompt_tokens > 0 or completion_tokens > 0):
        total_tokens = prompt_tokens + completion_tokens
    result = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
    return {key: value for key, value in result.items() if value > 0}


def _provider_usage(request: UserRequest) -> dict[str, Any]:
    for key in CONTEXT_TOKEN_USAGE_METADATA_KEYS:
        value = request.metadata.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _estimation_enabled(request: UserRequest) -> bool:
    return any(request.metadata.get(key) is True for key in CONTEXT_TOKEN_ESTIMATE_METADATA_KEYS)


def _metadata_int(request: UserRequest, keys: tuple[str, ...]) -> int:
    for key in keys:
        value = request.metadata.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return 0


def _usage_int(usage: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return 0


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, default=str)
