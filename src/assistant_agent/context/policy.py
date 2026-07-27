"""Context budget and compaction trigger policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from assistant_agent.context.models import ContextBudgetReport, ContextPolicy
from assistant_agent.runtime.requests import UserRequest


CONTEXT_BUDGET_METADATA_KEY = "context_budget_max_chars"
MIN_CONTEXT_BUDGET_MAX_CHARS = 500

COMPRESSION_STAGE_NONE = "none"
COMPRESSION_STAGE_COMPACTED = "compacted"
COMPRESSION_STAGE_BUDGET_TRIMMED = "budget_trimmed"

COMPRESSION_REASON_CONVERSATION_COMPACTED = "conversation_context_compacted"
COMPRESSION_REASON_OBSERVATION_COMPACTED = "observation_context_compacted"
COMPRESSION_REASON_CONTEXT_USAGE_HIGH = "context_usage_high"
COMPRESSION_REASON_CONTEXT_OVER_BUDGET = "context_over_budget"
COMPRESSION_REASON_CONTEXT_BUDGET_TRIMMED = "context_budget_trimmed"
COMPRESSION_REASON_TOOL_OBSERVATION_TOO_LARGE = "tool_observation_too_large"
COMPRESSION_REASON_PROVIDER_CONTEXT_OVERFLOW = "provider_context_overflow"
COMPRESSION_REASON_EXPLICIT_COMPACT = "explicit_compact"


@dataclass(frozen=True)
class CompactionDecision:
    """Result of evaluating whether a context pack should be compacted."""

    triggered: bool
    hard: bool
    reasons: list[str]


class CompactionPolicy:
    """Rule-based trigger for context compaction."""

    def evaluate(
        self,
        *,
        request: UserRequest,
        budget: ContextBudgetReport,
        observations: list[dict[str, Any]],
        policy: ContextPolicy,
    ) -> CompactionDecision:
        reasons: list[str] = []
        ratio = _usage_ratio(budget.total_chars, policy.max_context_chars)
        if ratio >= policy.compact_at_ratio:
            reasons.append(COMPRESSION_REASON_CONTEXT_USAGE_HIGH)
        if budget.total_chars > policy.max_context_chars:
            reasons.append(COMPRESSION_REASON_CONTEXT_OVER_BUDGET)
        if _has_large_tool_observation(observations, max_chars=policy.max_tool_result_chars):
            reasons.append(COMPRESSION_REASON_TOOL_OBSERVATION_TOO_LARGE)
        if _has_provider_context_overflow(request):
            reasons.append(COMPRESSION_REASON_PROVIDER_CONTEXT_OVERFLOW)
        if _has_explicit_compact_request(request):
            reasons.append(COMPRESSION_REASON_EXPLICIT_COMPACT)

        hard = (
            budget.total_chars > policy.max_context_chars
            or ratio >= policy.hard_compact_at_ratio
            or COMPRESSION_REASON_PROVIDER_CONTEXT_OVERFLOW in reasons
        )
        return CompactionDecision(
            triggered=bool(reasons),
            hard=hard,
            reasons=_unique(reasons),
        )


def context_policy_from_request(request: UserRequest) -> ContextPolicy:
    """Resolve context policy from request metadata with safe defaults."""

    max_chars = _metadata_int(request, CONTEXT_BUDGET_METADATA_KEY, default=ContextPolicy().max_context_chars)
    return ContextPolicy(max_context_chars=max(MIN_CONTEXT_BUDGET_MAX_CHARS, max_chars))


def _has_large_tool_observation(observations: list[dict[str, Any]], *, max_chars: int) -> bool:
    for observation in observations:
        compaction = observation.get("compaction")
        if isinstance(compaction, dict):
            original = compaction.get("original_chars")
            if isinstance(original, int) and original > max_chars:
                return True
        if len(str(observation)) > max_chars:
            return True
    return False


def _has_provider_context_overflow(request: UserRequest) -> bool:
    metadata = request.metadata
    if metadata.get("provider_context_overflow") is True or metadata.get("context_overflow") is True:
        return True
    overflow_codes = {
        "provider_context_overflow",
        "context_length_exceeded",
        "context_overflow",
        "input_too_large",
    }
    for key in ("provider_error_code", "error_code", "last_provider_error_code"):
        value = metadata.get(key)
        if isinstance(value, str) and value in overflow_codes:
            return True
    errors = metadata.get("provider_errors")
    if isinstance(errors, list):
        for error in errors:
            if isinstance(error, dict) and error.get("code") in overflow_codes:
                return True
    return False


def _has_explicit_compact_request(request: UserRequest) -> bool:
    metadata = request.metadata
    if metadata.get("compact_context") is True:
        return True
    for key in ("slash_command", "command"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip() == "/compact":
            return True
    return bool((request.text or "").strip() == "/compact")


def _metadata_int(request: UserRequest, key: str, *, default: int) -> int:
    value = request.metadata.get(key)
    return value if isinstance(value, int) and value >= 0 else default


def _usage_ratio(total_chars: int, max_chars: int) -> float:
    if max_chars <= 0:
        return 0.0
    return total_chars / max_chars


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
