from __future__ import annotations

import json

import pytest

from assistant_agent.media.video.visual_timeline_compactor import (
    LLMVisualTimelineCompactor,
)
from assistant_agent.media.video.visual_timeline_context import (
    VisualTimelineCompactionError,
    VisualTimelineItem,
)
from assistant_agent.runtime.chat_adapter import (
    ChatProviderError,
    ChatRequest,
    ChatResult,
)


class _CharacterCounter:
    tokenizer_id = "character-counter"

    def count_text(self, value: str) -> int:
        return len(value)


class _ScriptedAdapter:
    provider = "test-provider"
    model = "timeline-compactor-test-model"

    def __init__(self, result: ChatResult) -> None:
        self.result = result
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return self.result


def _items() -> list[VisualTimelineItem]:
    return [
        VisualTimelineItem(timestamp_ms=1_000, text="桌面上有一把钥匙"),
        VisualTimelineItem(timestamp_ms=2_000, text="桌面上出现黑色手机"),
    ]


def _result(payload: dict[str, object]) -> ChatResult:
    return ChatResult(
        provider="test-provider",
        model="timeline-compactor-test-model",
        response_text=json.dumps(payload, ensure_ascii=False),
        usage={"input_tokens": 20, "output_tokens": 5},
    )


def test_llm_timeline_compactor_sends_query_and_indexed_vlm_text() -> None:
    adapter = _ScriptedAdapter(
        _result(
            {
                "summary": "先出现钥匙，随后出现黑色手机。",
                "relevant_observation_indexes": [1],
            }
        )
    )
    compactor = LLMVisualTimelineCompactor(
        adapter,
        token_counter=_CharacterCounter(),
    )

    compacted = compactor.compact(
        query="黑色手机",
        observations=_items(),
        source_token_count=400,
        summary_max_tokens=500,
    )

    assert compacted.summary == "先出现钥匙，随后出现黑色手机。"
    assert compacted.relevant_observation_indexes == [1]
    assert compacted.provider_usage == {
        "prompt_tokens": 20,
        "completion_tokens": 5,
        "total_tokens": 25,
    }
    request = adapter.requests[0]
    assert request.response_format == {"type": "json_object"}
    assert request.temperature == 0.0
    assert request.max_tokens == 500
    source = request.messages[1]["content"]
    assert "黑色手机" in source
    assert '"index":1' in source
    assert "桌面上出现黑色手机" in source


@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        (
            {
                "summary": "摘要",
                "relevant_observation_indexes": [2],
            },
            "visual_timeline_index_out_of_range",
        ),
        (
            {
                "summary": "摘要",
                "relevant_observation_indexes": [1, 1],
            },
            "visual_timeline_duplicate_index",
        ),
        (
            {
                "summary": "摘要",
                "relevant_observation_indexes": [1],
                "extra": "not allowed",
            },
            "visual_timeline_invalid_output",
        ),
    ],
)
def test_llm_timeline_compactor_rejects_invalid_index_contract(
    payload: dict[str, object],
    error_code: str,
) -> None:
    compactor = LLMVisualTimelineCompactor(
        _ScriptedAdapter(_result(payload)),
        token_counter=_CharacterCounter(),
    )

    with pytest.raises(VisualTimelineCompactionError) as exc_info:
        compactor.compact(
            query="手机",
            observations=_items(),
            source_token_count=400,
            summary_max_tokens=500,
        )

    assert exc_info.value.code == error_code


def test_llm_timeline_compactor_rejects_invalid_json() -> None:
    adapter = _ScriptedAdapter(
        ChatResult(provider="test-provider", response_text="not-json")
    )

    with pytest.raises(VisualTimelineCompactionError) as exc_info:
        LLMVisualTimelineCompactor(
            adapter,
            token_counter=_CharacterCounter(),
        ).compact(
            query="手机",
            observations=_items(),
            source_token_count=400,
            summary_max_tokens=500,
        )

    assert exc_info.value.code == "visual_timeline_invalid_json"


def test_llm_timeline_compactor_rejects_summary_over_budget() -> None:
    adapter = _ScriptedAdapter(
        _result(
            {
                "summary": "过长摘要",
                "relevant_observation_indexes": [],
            }
        )
    )

    with pytest.raises(VisualTimelineCompactionError) as exc_info:
        LLMVisualTimelineCompactor(
            adapter,
            token_counter=_CharacterCounter(),
        ).compact(
            query="手机",
            observations=_items(),
            source_token_count=400,
            summary_max_tokens=2,
        )

    assert exc_info.value.code == "visual_timeline_summary_token_budget_exceeded"


def test_llm_timeline_compactor_maps_provider_failure() -> None:
    adapter = _ScriptedAdapter(
        ChatResult(
            provider="test-provider",
            errors=[
                ChatProviderError(
                    code="upstream_timeout",
                    message="timeout",
                    recoverable=True,
                )
            ],
        )
    )

    with pytest.raises(VisualTimelineCompactionError) as exc_info:
        LLMVisualTimelineCompactor(
            adapter,
            token_counter=_CharacterCounter(),
        ).compact(
            query="手机",
            observations=_items(),
            source_token_count=400,
            summary_max_tokens=500,
        )

    assert exc_info.value.code == "visual_timeline_compactor_unavailable"
