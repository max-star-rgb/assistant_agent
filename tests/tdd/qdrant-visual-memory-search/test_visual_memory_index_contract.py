from __future__ import annotations

from assistant_agent.media.video.visual_memory_index import (
    UnavailableVisualMemoryTextIndex,
    VisualMemoryIndexDocument,
    VisualMemoryIndexQuery,
)


def test_unavailable_index_reports_structured_error_without_fallback() -> None:
    index = UnavailableVisualMemoryTextIndex(
        code="visual_memory_qdrant_unavailable",
        message="Qdrant is unavailable",
    )

    result = index.search(
        VisualMemoryIndexQuery(
            user_id="user-a",
            session_id="session-a",
            query="鼠标",
            limit=12,
        )
    )

    assert result.status == "unavailable"
    assert result.hits == []
    assert result.coverage_complete is False
    assert [error.model_dump() for error in result.errors] == [
        {
            "code": "visual_memory_qdrant_unavailable",
            "message": "Qdrant is unavailable",
            "recoverable": True,
        }
    ]


def test_index_document_keeps_identity_time_and_full_vlm_text() -> None:
    document = VisualMemoryIndexDocument(
        record_id="visual-81",
        user_id="user-a",
        session_id="session-a",
        video_id="video-a",
        frame_sequence=81,
        captured_at_ms=1_754_469_465_000,
        text="画面中可见一只罗技鼠标，位于键盘右侧。",
    )

    assert document.model_dump() == {
        "record_id": "visual-81",
        "user_id": "user-a",
        "session_id": "session-a",
        "video_id": "video-a",
        "frame_sequence": 81,
        "captured_at_ms": 1_754_469_465_000,
        "text": "画面中可见一只罗技鼠标，位于键盘右侧。",
    }
