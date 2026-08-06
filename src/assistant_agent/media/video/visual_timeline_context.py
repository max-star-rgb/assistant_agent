"""Token-aware projection of retained VLM text into a Tool observation."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.context.token_budget import ContextWindowPolicy


class VisualTimelineItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    timestamp_ms: int = Field(ge=0)
    time_label: str | None = Field(default=None, max_length=120)
    text: str = Field(max_length=4_000)


class VisualTimelineCompaction(BaseModel):
    model_config = ConfigDict(frozen=True)

    summary: str = Field(min_length=1, max_length=32_000)
    relevant_observation_indexes: list[int] = Field(default_factory=list)
    provider_usage: dict[str, int] = Field(default_factory=dict)


class VisualTimelineCoverage(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_count: int = Field(ge=0)
    covered_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    digest: str = Field(min_length=16, max_length=16)


class VisualTimelineCompactionMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["not_needed", "succeeded", "failed_below_hard"]
    tokenizer_id: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    effective_input_limit: int = Field(ge=1)
    target_tokens: int = Field(ge=1)
    triggered: bool
    hard: bool
    attempts: int = Field(default=0, ge=0)
    target_reached: bool
    error_code: str | None = None
    provider_usage: dict[str, int] = Field(default_factory=dict)


class VisualTimelineProjection(BaseModel):
    model_config = ConfigDict(frozen=True)

    observations: list[VisualTimelineItem] = Field(default_factory=list)
    timeline_summary: str | None = None
    coverage: VisualTimelineCoverage | None = None
    compaction: VisualTimelineCompactionMetadata


class VisualTimelineTokenCounter(Protocol):
    tokenizer_id: str

    def count_text(self, value: str) -> int:
        """Count tokens in one exact Tool projection."""


class VisualTimelineCompactor(Protocol):
    def compact(
        self,
        *,
        query: str,
        observations: list[VisualTimelineItem],
        source_token_count: int,
        summary_max_tokens: int,
    ) -> VisualTimelineCompaction:
        """Summarize an old timeline prefix and select exact evidence indexes."""


class VisualTimelineCompactionError(ValueError):
    def __init__(
        self,
        code: str,
        *,
        provider_usage: dict[str, int] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.provider_usage = dict(provider_usage or {})


class VisualTimelineHardLimitError(VisualTimelineCompactionError):
    def __init__(self, detail_code: str) -> None:
        super().__init__("visual_memory_context_hard_limit")
        self.detail_code = detail_code


class VisualTimelineContextService:
    """Apply target/trigger/hard policy before visual history reaches the LLM."""

    def __init__(
        self,
        *,
        compactor: VisualTimelineCompactor,
        token_counter: VisualTimelineTokenCounter,
        window_policy: ContextWindowPolicy,
        keep_recent_observations: int = 4,
    ) -> None:
        if keep_recent_observations < 0:
            raise ValueError("visual timeline recent observation count must be non-negative")
        self.compactor = compactor
        self.token_counter = token_counter
        self.window_policy = window_policy
        self.keep_recent_observations = keep_recent_observations

    def prepare(
        self,
        *,
        query: str,
        observations: list[VisualTimelineItem],
    ) -> VisualTimelineProjection:
        source = [item.model_copy(deep=True) for item in observations]
        input_tokens = self._count_projection(source)
        decision = self.window_policy.evaluate(input_tokens)
        if not decision.triggered:
            return VisualTimelineProjection(
                observations=source,
                compaction=self._metadata(
                    status="not_needed",
                    decision=decision,
                    input_tokens=input_tokens,
                    output_tokens=input_tokens,
                    attempts=0,
                    provider_usage={},
                ),
            )

        recent_count = min(len(source), self.keep_recent_observations)
        compactable_count = len(source) - recent_count
        if compactable_count <= 0:
            return self._failed_or_raise(
                source=source,
                decision=decision,
                input_tokens=input_tokens,
                attempts=0,
                error_code="visual_timeline_no_compactable_prefix",
                provider_usage={},
            )

        compactable = source[:compactable_count]
        recent = source[compactable_count:]
        fixed_tokens = self._count_projection(recent)
        summary_budget = min(
            self.window_policy.summary_max_tokens,
            max(1, decision.target_tokens - fixed_tokens),
        )
        attempts = 2 if decision.hard else 1
        last_error = "visual_timeline_compaction_failed"
        provider_usage: dict[str, int] = {}

        for attempt in range(attempts):
            attempt_budget = max(1, summary_budget // (2**attempt))
            try:
                compacted = self.compactor.compact(
                    query=query,
                    observations=compactable,
                    source_token_count=input_tokens,
                    summary_max_tokens=attempt_budget,
                )
                selected_indexes = _validated_indexes(
                    compacted.relevant_observation_indexes,
                    source_count=compactable_count,
                )
            except (RuntimeError, TypeError, ValueError) as exc:
                last_error = _error_code(exc)
                provider_usage = dict(getattr(exc, "provider_usage", {}) or {})
                continue

            provider_usage = dict(compacted.provider_usage)
            returned_indexes = sorted(
                {
                    *selected_indexes,
                    *range(compactable_count, len(source)),
                }
            )
            returned = [source[index] for index in returned_indexes]
            coverage = _coverage(
                compactable,
                source_count=len(source),
                returned_count=len(returned),
            )
            output_tokens = self._count_projection(
                returned,
                timeline_summary=compacted.summary,
                coverage=coverage,
            )
            output_decision = self.window_policy.evaluate(output_tokens)
            if output_decision.hard:
                last_error = "visual_timeline_compacted_output_still_hard"
                continue
            return VisualTimelineProjection(
                observations=returned,
                timeline_summary=compacted.summary,
                coverage=coverage,
                compaction=self._metadata(
                    status="succeeded",
                    decision=decision,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    attempts=attempt + 1,
                    provider_usage=provider_usage,
                ),
            )

        return self._failed_or_raise(
            source=source,
            decision=decision,
            input_tokens=input_tokens,
            attempts=attempts,
            error_code=last_error,
            provider_usage=provider_usage,
        )

    def _failed_or_raise(
        self,
        *,
        source: list[VisualTimelineItem],
        decision,
        input_tokens: int,
        attempts: int,
        error_code: str,
        provider_usage: dict[str, int],
    ) -> VisualTimelineProjection:
        if decision.hard:
            raise VisualTimelineHardLimitError(error_code)
        return VisualTimelineProjection(
            observations=source,
            compaction=self._metadata(
                status="failed_below_hard",
                decision=decision,
                input_tokens=input_tokens,
                output_tokens=input_tokens,
                attempts=attempts,
                provider_usage=provider_usage,
                error_code=error_code,
            ),
        )

    def _metadata(
        self,
        *,
        status: Literal["not_needed", "succeeded", "failed_below_hard"],
        decision,
        input_tokens: int,
        output_tokens: int,
        attempts: int,
        provider_usage: dict[str, int],
        error_code: str | None = None,
    ) -> VisualTimelineCompactionMetadata:
        return VisualTimelineCompactionMetadata(
            status=status,
            tokenizer_id=self.token_counter.tokenizer_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            effective_input_limit=decision.effective_input_limit,
            target_tokens=decision.target_tokens,
            triggered=decision.triggered,
            hard=decision.hard,
            attempts=attempts,
            target_reached=output_tokens <= decision.target_tokens,
            error_code=error_code,
            provider_usage=provider_usage,
        )

    def _count_projection(
        self,
        observations: list[VisualTimelineItem],
        *,
        timeline_summary: str | None = None,
        coverage: VisualTimelineCoverage | None = None,
    ) -> int:
        payload: dict[str, object] = {
            "observations": [
                item.model_dump(mode="json") for item in observations
            ]
        }
        if timeline_summary is not None:
            payload["timeline_summary"] = timeline_summary
        if coverage is not None:
            payload["coverage"] = coverage.model_dump(mode="json")
        return self.token_counter.count_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )


def _validated_indexes(indexes: list[int], *, source_count: int) -> list[int]:
    if any(isinstance(index, bool) or not isinstance(index, int) for index in indexes):
        raise VisualTimelineCompactionError("visual_timeline_invalid_index")
    if len(indexes) != len(set(indexes)):
        raise VisualTimelineCompactionError("visual_timeline_duplicate_index")
    if any(index < 0 or index >= source_count for index in indexes):
        raise VisualTimelineCompactionError("visual_timeline_index_out_of_range")
    return sorted(indexes)


def _coverage(
    compactable: list[VisualTimelineItem],
    *,
    source_count: int,
    returned_count: int,
) -> VisualTimelineCoverage:
    encoded = json.dumps(
        [item.model_dump(mode="json") for item in compactable],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return VisualTimelineCoverage(
        source_count=source_count,
        covered_count=len(compactable),
        returned_count=returned_count,
        start_ms=compactable[0].timestamp_ms if compactable else None,
        end_ms=compactable[-1].timestamp_ms if compactable else None,
        digest=hashlib.sha256(encoded).hexdigest()[:16],
    )


def _error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code else type(exc).__name__
