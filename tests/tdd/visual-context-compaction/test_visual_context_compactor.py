from __future__ import annotations

import json

import pytest

from assistant_agent.media.video.semantic_store import VisualSemanticRecord
from assistant_agent.media.video.visual_context_compactor import (
    LLMVisualContextCompactor,
    VisualContextCompactionError,
)
from assistant_agent.media.video.visual_context_models import VisualContextSummary
from assistant_agent.runtime.chat_adapter import ChatRequest, ChatResult


class WordCounter:
    def count_text(self, text: str) -> int:
        return len(text.split())


class ScriptedChatAdapter:
    provider = "test-provider"
    model = "visual-compactor-test-model"

    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return ChatResult(
            provider=self.provider,
            model=self.model,
            response_text=self.response_text,
        )


@pytest.fixture
def records() -> list[VisualSemanticRecord]:
    return [
        _record(record_id="record-2", sequence=2, captured_at_ms=2_000),
        _record(record_id="record-3", sequence=3, captured_at_ms=3_000),
    ]


def test_llm_visual_compactor_rejects_non_contiguous_coverage(
    records: list[VisualSemanticRecord],
) -> None:
    adapter = ScriptedChatAdapter(
        response_text=json.dumps(
            {
                "covered_record_ids": [records[1].record_id],
                "stable_scene": [],
                "object_last_confirmed": [],
                "people_last_confirmed": [],
                "changes": [],
                "uncertainties": [],
            }
        )
    )
    compactor = LLMVisualContextCompactor(adapter, token_counter=WordCounter())

    with pytest.raises(VisualContextCompactionError, match="non_contiguous"):
        compactor.compact(
            video_id="video-1",
            existing_summary=None,
            records=records,
            source_token_count=30,
            summary_max_tokens=20,
        )


def test_llm_visual_compactor_computes_coverage_metadata_from_records(
    records: list[VisualSemanticRecord],
) -> None:
    existing = VisualContextSummary(
        video_id="video-1",
        summary_revision=4,
        covered_record_ids=["record-1"],
        first_sequence=1,
        last_sequence=1,
        first_captured_at_ms=1_000,
        last_captured_at_ms=1_000,
        stable_scene=["原有场景"],
        source_token_count=10,
        summary_token_count=2,
    )
    adapter = ScriptedChatAdapter(
        response_text=json.dumps(
            {
                "covered_record_ids": ["record-1", "record-2", "record-3"],
                "stable_scene": ["客厅"],
                "object_last_confirmed": ["杯子@record-3"],
                "people_last_confirmed": [],
                "changes": ["杯子被移动"],
                "uncertainties": [],
            },
            ensure_ascii=False,
        )
    )

    summary = LLMVisualContextCompactor(adapter, token_counter=WordCounter()).compact(
        video_id="video-1",
        existing_summary=existing,
        records=records,
        source_token_count=30,
        summary_max_tokens=20,
    )

    assert summary.video_id == "video-1"
    assert summary.summary_revision == 5
    assert summary.covered_record_ids == ["record-1", "record-2", "record-3"]
    assert summary.first_sequence == 1
    assert summary.last_sequence == 3
    assert summary.first_captured_at_ms == 1_000
    assert summary.last_captured_at_ms == 3_000
    assert summary.source_token_count == 30
    assert summary.compactor_model == "visual-compactor-test-model"
    assert adapter.requests[0].response_format == {"type": "json_object"}
    serialized_request = json.dumps(
        adapter.requests[0].messages,
        ensure_ascii=False,
    )
    assert "/private/evidence" not in serialized_request
    assert "search_embedding" not in serialized_request


def test_llm_visual_compactor_rejects_summary_over_token_budget(
    records: list[VisualSemanticRecord],
) -> None:
    adapter = ScriptedChatAdapter(
        response_text=json.dumps(
            {
                "covered_record_ids": ["record-2", "record-3"],
                "stable_scene": ["one two three four five six"],
                "object_last_confirmed": [],
                "people_last_confirmed": [],
                "changes": [],
                "uncertainties": [],
            }
        )
    )

    with pytest.raises(VisualContextCompactionError, match="token_budget"):
        LLMVisualContextCompactor(adapter, token_counter=WordCounter()).compact(
            video_id="video-1",
            existing_summary=None,
            records=records,
            source_token_count=30,
            summary_max_tokens=3,
        )


def test_llm_visual_compactor_rejects_fields_outside_fixed_schema(
    records: list[VisualSemanticRecord],
) -> None:
    adapter = ScriptedChatAdapter(
        response_text=json.dumps(
            {
                "covered_record_ids": ["record-2", "record-3"],
                "stable_scene": [],
                "object_last_confirmed": [],
                "people_last_confirmed": [],
                "changes": [],
                "uncertainties": [],
                "evidence_ref": "/private/model-invented-path.jpg",
            }
        )
    )

    with pytest.raises(VisualContextCompactionError, match="invalid_output"):
        LLMVisualContextCompactor(adapter, token_counter=WordCounter()).compact(
            video_id="video-1",
            existing_summary=None,
            records=records,
            source_token_count=30,
            summary_max_tokens=20,
        )


def _record(
    *,
    record_id: str,
    sequence: int,
    captured_at_ms: int,
) -> VisualSemanticRecord:
    return VisualSemanticRecord(
        record_id=record_id,
        session_id="session-1",
        video_id="video-1",
        frame_sequence=sequence,
        captured_at_ms=captured_at_ms,
        scene="客厅",
        objects=["杯子"],
        people=[],
        actions=[],
        events=[],
        text_in_video=[],
        summary="画面稳定",
        index_status="unavailable",
        evidence_ref=f"/private/evidence-{sequence}.jpg",
        evidence_bytes=100,
        created_at_ms=captured_at_ms,
    )
