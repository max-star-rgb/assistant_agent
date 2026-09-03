"""Token budget policy shared by visual-context projections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContextWindowDecision:
    """Preflight decision for one visual-context projection."""

    input_tokens: int
    effective_input_limit: int
    usage_ratio: float
    target_tokens: int
    triggered: bool
    hard: bool


@dataclass(frozen=True)
class ContextWindowPolicy:
    """Token thresholds with hysteresis for visual-context compaction."""

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


def normalize_provider_token_usage(usage: dict[str, Any]) -> dict[str, int]:
    """Return only safe Provider token counters from a usage payload."""

    prompt_tokens = _usage_int(
        usage,
        ("prompt_tokens", "input_tokens", "input_token_count"),
    )
    completion_tokens = _usage_int(
        usage,
        ("completion_tokens", "output_tokens", "output_token_count"),
    )
    total_tokens = _usage_int(usage, ("total_tokens", "total_token_count"))
    if total_tokens <= 0 and (prompt_tokens > 0 or completion_tokens > 0):
        total_tokens = prompt_tokens + completion_tokens
    result = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
    return {key: value for key, value in result.items() if value > 0}


def _usage_int(usage: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return 0
