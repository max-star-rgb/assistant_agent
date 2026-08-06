from __future__ import annotations

import pytest
from pydantic import ValidationError

from assistant_agent.media.video.realtime_video_observer import (
    _build_visual_search_text,
)
from assistant_agent.media.video.semantic_store import VisualSemanticRecord
from assistant_agent.media.video.visual_context import _record_projection
from assistant_agent.media.video.visual_context_compactor import (
    _record_projection as _compactor_record_projection,
)
from assistant_agent.media.vision.models import (
    VideoUnderstandingRequest,
    VideoUnderstandingResult,
    VisionUnderstandingResult,
)
from assistant_agent.media.vision.vision_client import (
    video_result_from_vision_result,
    vision_result_from_video_result,
)
from assistant_agent.providers.qwen_realtime_vision import (
    _instructions,
    _normalize_result_payload,
)


def _video_result() -> VideoUnderstandingResult:
    return VideoUnderstandingResult(
        summary="当前桌面为空",
        objects=["桌面"],
        changes=["上一帧的杯子当前未观察到"],
        uncertainties=["杯子可能被移出画面"],
        provider="provider-sentinel",
        output_ref="provider://visual-grounding/result",
    )


def _semantic_record(*, uncertainties: list[str] | None = None) -> VisualSemanticRecord:
    return VisualSemanticRecord(
        record_id="record-7",
        session_id="session-1",
        video_id="video-1",
        frame_sequence=7,
        captured_at_ms=7_000,
        summary="当前桌面为空",
        objects=["桌面"],
        changes=["上一帧的杯子当前未观察到"],
        uncertainties=(
            uncertainties if uncertainties is not None else ["杯子可能被移出画面"]
        ),
        index_status="unavailable",
        evidence_ref="evidence-7.jpg",
        evidence_bytes=10,
        created_at_ms=7_000,
    )


def test_qwen_result_keeps_only_single_frame_summary_text() -> None:
    payload = _normalize_result_payload(
        {
            "summary": "当前桌面为空",
            "objects": ["桌面"],
            "changes": ["上一帧的杯子当前未观察到"],
            "uncertainties": ["杯子可能被移出画面"],
        }
    )

    assert payload == {"summary": "当前桌面为空"}


def test_shared_grounding_models_still_bound_lists_to_twenty_items() -> None:
    too_many = [f"变化-{index}" for index in range(21)]

    with pytest.raises(ValidationError):
        VideoUnderstandingResult(
            summary="当前桌面为空",
            changes=too_many,
            provider="provider-sentinel",
            output_ref="provider://visual-grounding/too-many",
        )
    with pytest.raises(ValidationError):
        VisionUnderstandingResult(
            summary="当前桌面为空",
            uncertainties=too_many,
            provider="provider-sentinel",
            output_ref="provider://visual-grounding/too-many",
        )
    with pytest.raises(ValidationError):
        _semantic_record(uncertainties=too_many)


def test_qwen_instructions_ignore_visual_history_and_use_only_current_frame() -> None:
    history_payload = f"历史中的杯子仍在桌上{'历史记录' * 1_000}历史边界结束"
    memory_context = (
        '<visual_history trust="untrusted_observation" '
        'instruction_policy="do_not_execute" as_of_sequence="7">'
        f"{history_payload}"
        "</visual_history>"
    )

    instructions = _instructions(
        VideoUnderstandingRequest(
            video_ref="video-1",
            frame_refs=["frame-8.jpg"],
            user_query="更新当前画面",
            memory_context=memory_context,
        )
    )

    assert memory_context not in instructions
    assert "只描述当前这一张图片" in instructions
    assert "不使用或推断此前画面" in instructions
    assert len(instructions) < 1_000


def test_qwen_instructions_do_not_request_cross_frame_changes() -> None:
    instructions = _instructions(
        VideoUnderstandingRequest(
            video_ref="video-1",
            frame_refs=["frame-8.jpg"],
            memory_context=(
                '<visual_history trust="untrusted_observation" '
                'instruction_policy="do_not_execute" as_of_sequence="7">'
                "历史记录"
                "</visual_history>"
            ),
        )
    )

    assert "visual_history" not in instructions
    assert "changes" not in instructions
    assert "只返回当前单帧文本" in instructions


def test_visual_search_text_indexes_only_current_confirmed_facts() -> None:
    search_text = _build_visual_search_text(_video_result())

    assert "物体：桌面" in search_text
    assert "上一帧的杯子当前未观察到" not in search_text
    assert "杯子可能被移出画面" not in search_text


def test_visual_result_converters_preserve_changes_and_uncertainties() -> None:
    unified = vision_result_from_video_result(_video_result())
    round_trip = video_result_from_vision_result(unified)

    assert unified.changes == ["上一帧的杯子当前未观察到"]
    assert unified.uncertainties == ["杯子可能被移出画面"]
    assert round_trip.changes == unified.changes
    assert round_trip.uncertainties == unified.uncertainties


def test_visual_history_projection_preserves_record_changes_and_uncertainties() -> None:
    projection = _record_projection(_semantic_record())

    assert "record_id" not in projection
    assert projection["objects"] == ["桌面"]
    assert projection["changes"] == ["上一帧的杯子当前未观察到"]
    assert projection["uncertainties"] == ["杯子可能被移出画面"]


def test_visual_compactor_projection_preserves_grounding_fields() -> None:
    projection = _compactor_record_projection(_semantic_record())

    assert "record_id" not in projection
    assert projection["changes"] == ["上一帧的杯子当前未观察到"]
    assert projection["uncertainties"] == ["杯子可能被移出画面"]
