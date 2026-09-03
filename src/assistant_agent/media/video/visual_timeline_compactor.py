"""LLM adapter for query-aware compaction of historical VLM text."""

from __future__ import annotations

import json
from typing import Any, Mapping

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from assistant_agent.config import VisionConfig
from assistant_agent.provider_mode import ProviderMode
from assistant_agent.media.video.token_budget import (
    ContextWindowPolicy,
    normalize_provider_token_usage,
)
from assistant_agent.media.video.visual_timeline_context import (
    VisualTimelineCompaction,
    VisualTimelineCompactionError,
    VisualTimelineContextService,
    VisualTimelineItem,
    VisualTimelineTokenCounter,
)


_SYSTEM_PROMPT = """你是视觉时间线压缩器。输入包含用户当前检索目标和按时间排序的单帧 VLM 文本。
请覆盖全部输入记录，生成忠实、紧凑、便于后续检索的中文摘要；不要把未观察到的内容写成事实。
同时选择与当前检索目标可能相关的原始记录 index。宁可保留不确定候选，也不要武断判定目标不存在。
只返回一个 JSON object，且只能包含：
{"summary":"...","relevant_observation_indexes":[0,1]}
index 必须来自输入，不能重复。"""


class LLMVisualTimelineCompactor:
    """Use the governed native chat model to compact one old timeline prefix."""

    def __init__(
        self,
        model: BaseChatModel,
        *,
        token_counter: VisualTimelineTokenCounter,
    ) -> None:
        self.model = model
        self.token_counter = token_counter

    def compact(
        self,
        *,
        query: str,
        observations: list[VisualTimelineItem],
        source_token_count: int,
        summary_max_tokens: int,
    ) -> VisualTimelineCompaction:
        if not observations:
            raise VisualTimelineCompactionError("visual_timeline_empty_observations")
        if summary_max_tokens <= 0:
            raise VisualTimelineCompactionError("visual_timeline_invalid_summary_budget")

        try:
            options: dict[str, Any] = {
                "response_format": {"type": "json_object"},
                "temperature": 0.0,
                "max_tokens": max(1, summary_max_tokens),
            }
            if getattr(self.model, "enable_search", False):
                options["provider_search_profile"] = "none"
            extra_body = getattr(self.model, "extra_body", None)
            if isinstance(extra_body, Mapping) and extra_body.get("enable_search"):
                options["extra_body"] = {**extra_body, "enable_search": False}
            response = self.model.bind(**options).invoke(
                [
                    SystemMessage(content=_SYSTEM_PROMPT),
                    HumanMessage(
                        content=_source_payload(
                            query=query,
                            observations=observations,
                            source_token_count=source_token_count,
                        )
                    ),
                ]
            )
        except Exception as exc:
            raise VisualTimelineCompactionError(
                "visual_timeline_compactor_unavailable"
            ) from exc
        provider_usage = normalize_provider_token_usage(
            dict(response.usage_metadata or {})
        )
        response_text = response.text.strip()
        if not response_text:
            raise VisualTimelineCompactionError(
                "visual_timeline_compactor_unavailable",
                provider_usage=provider_usage,
            )
        try:
            payload = json.loads(response_text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise VisualTimelineCompactionError(
                "visual_timeline_invalid_json",
                provider_usage=provider_usage,
            ) from exc
        summary, indexes = _validate_payload(
            payload,
            source_count=len(observations),
        )
        canonical_projection = json.dumps(
            {
                "summary": summary,
                "relevant_observation_indexes": indexes,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if self.token_counter.count_text(canonical_projection) > summary_max_tokens:
            raise VisualTimelineCompactionError(
                "visual_timeline_summary_token_budget_exceeded",
                provider_usage=provider_usage,
            )
        return VisualTimelineCompaction(
            summary=summary,
            relevant_observation_indexes=indexes,
            provider_usage=provider_usage,
        )


def create_visual_timeline_compactor(
    config: VisionConfig,
    model: BaseChatModel,
    *,
    provider_mode: ProviderMode,
    token_counter: VisualTimelineTokenCounter | None,
) -> LLMVisualTimelineCompactor | None:
    """Create the Tool-tail compactor without weakening provider boundaries."""

    if config.visual_context_compactor_mode == "off":
        return None
    if provider_mode != "real":
        return None
    if token_counter is None:
        raise ValueError(
            "LLM visual timeline compaction requires "
            "MULTIMODAL_AGENT_CONTEXT_TOKENIZER_PATH"
        )
    return LLMVisualTimelineCompactor(
        model,
        token_counter=token_counter,
    )


def create_visual_timeline_context_service(
    config: VisionConfig,
    model: BaseChatModel,
    *,
    provider_mode: ProviderMode,
    token_counter: VisualTimelineTokenCounter | None,
) -> VisualTimelineContextService | None:
    """Build the optional Tool-tail hard gate from native composition resources."""

    compactor = create_visual_timeline_compactor(
        config,
        model,
        provider_mode=provider_mode,
        token_counter=token_counter,
    )
    if compactor is None:
        return None
    assert token_counter is not None
    return VisualTimelineContextService(
        compactor=compactor,
        token_counter=token_counter,
        window_policy=ContextWindowPolicy(
            input_token_limit=config.visual_context_input_token_limit,
            trigger_ratio=config.visual_context_compaction_trigger_ratio,
            target_ratio=config.visual_context_compaction_target_ratio,
            hard_ratio=config.visual_context_compaction_hard_ratio,
            safety_margin_tokens=config.visual_context_compaction_safety_margin_tokens,
            summary_max_tokens=config.visual_context_summary_max_tokens,
        ),
    )


def _source_payload(
    *,
    query: str,
    observations: list[VisualTimelineItem],
    source_token_count: int,
) -> str:
    return json.dumps(
        {
            "query": query,
            "source_token_count": max(0, source_token_count),
            "observations": [
                {
                    "index": index,
                    "timestamp_ms": observation.timestamp_ms,
                    "text": observation.text,
                }
                for index, observation in enumerate(observations)
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _validate_payload(
    payload: Any,
    *,
    source_count: int,
) -> tuple[str, list[int]]:
    if not isinstance(payload, Mapping) or set(payload) != {
        "summary",
        "relevant_observation_indexes",
    }:
        raise VisualTimelineCompactionError("visual_timeline_invalid_output")
    summary = payload.get("summary")
    indexes = payload.get("relevant_observation_indexes")
    if not isinstance(summary, str) or not summary.strip():
        raise VisualTimelineCompactionError("visual_timeline_invalid_output")
    if not isinstance(indexes, list):
        raise VisualTimelineCompactionError("visual_timeline_invalid_output")
    if any(isinstance(index, bool) or not isinstance(index, int) for index in indexes):
        raise VisualTimelineCompactionError("visual_timeline_invalid_index")
    if len(indexes) != len(set(indexes)):
        raise VisualTimelineCompactionError("visual_timeline_duplicate_index")
    if any(index < 0 or index >= source_count for index in indexes):
        raise VisualTimelineCompactionError("visual_timeline_index_out_of_range")
    return summary.strip(), sorted(indexes)
