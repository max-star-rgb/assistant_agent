from __future__ import annotations

from pathlib import Path

from assistant_agent.media.embedding.consumers.object_search import (
    VisualMemorySearchRequest,
    VisualMemorySearchService,
)
from assistant_agent.media.video.semantic_store import (
    SessionVisualSemanticStore,
    VisualSemanticRecord,
)
from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)
from assistant_agent.media.video.visual_memory_index import (
    VisualMemoryIndexDocument,
    VisualMemoryIndexHit,
    VisualMemoryIndexQuery,
    VisualMemoryIndexSearchResult,
    VisualMemoryIndexWriteResult,
)
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.plugins.builtin.media_inspection.visual_memory_tool import (
    VisualMemorySearchInput,
    VisualMemorySearchTool,
)


class RecordingIndex:
    def __init__(self) -> None:
        self.queries: list[VisualMemoryIndexQuery] = []

    def upsert(self, document) -> VisualMemoryIndexWriteResult:
        del document
        return VisualMemoryIndexWriteResult(status="ready")

    def search(self, query: VisualMemoryIndexQuery) -> VisualMemoryIndexSearchResult:
        self.queries.append(query)
        document = VisualMemoryIndexDocument(
            record_id="record-81",
            user_id=query.user_id,
            session_id=query.session_id,
            video_id="video-1",
            frame_sequence=81,
            captured_at_ms=1_754_469_465_000,
            text="画面中可见一只罗技鼠标，位于键盘右侧。",
        )
        return VisualMemoryIndexSearchResult(
            status="records",
            hits=[VisualMemoryIndexHit(document=document, score=0.75)],
        )

    def delete_session(self, user_id: str, session_id: str) -> None:
        del user_id, session_id

    def delete_user(self, user_id: str) -> None:
        del user_id

    def close(self) -> None:
        return None


class StaleRecordingIndex(RecordingIndex):
    def search(self, query: VisualMemoryIndexQuery) -> VisualMemoryIndexSearchResult:
        current = super().search(query)
        stale = VisualMemoryIndexDocument(
            record_id="evicted-record",
            user_id=query.user_id,
            session_id=query.session_id,
            video_id="video-1",
            frame_sequence=1,
            captured_at_ms=1_754_469_465_000,
            text="已经越过本地 retention 的旧文本",
        )
        return current.model_copy(
            update={
                "hits": [
                    VisualMemoryIndexHit(document=stale, score=0.9),
                    *current.hits,
                ]
            }
        )


def test_service_returns_qdrant_hits_as_timestamped_vlm_text(tmp_path: Path) -> None:
    evidence = tmp_path / "frame.jpg"
    evidence.write_bytes(b"jpeg")
    store = SessionVisualSemanticStore(
        root=tmp_path / "store",
        session_id="session-1",
    )
    store.record_success(
        VisualSemanticRecord(
            record_id="record-81",
            session_id="session-1",
            video_id="video-1",
            frame_sequence=81,
            captured_at_ms=1_754_469_465_000,
            summary="画面中可见一只罗技鼠标，位于键盘右侧。",
            search_embedding=[1.0],
            embedding_space_id="legacy-test",
            index_status="ready",
            evidence_ref=str(evidence),
            evidence_bytes=evidence.stat().st_size,
            created_at_ms=1_754_469_465_010,
        )
    )
    index = RecordingIndex()
    service = VisualMemorySearchService(
        semantic_store=store,
        text_index=index,
        limit=12,
        clock_ms=lambda: 1_754_469_470_000,
    )

    result = service.search(
        VisualMemorySearchRequest(
            user_id="user-1",
            session_id="session-1",
            request_id="request-1",
            query="鼠标",
            as_of_sequence=85,
        )
    )

    assert result.status == "records"
    assert result.observations[0].timestamp_ms == 1_754_469_465_000
    assert result.observations[0].time_label.startswith("约5秒前（")
    assert result.observations[0].text == "画面中可见一只罗技鼠标，位于键盘右侧。"
    assert index.queries == [
        VisualMemoryIndexQuery(
            user_id="user-1",
            session_id="session-1",
            query="鼠标",
            as_of_sequence=85,
            since_ms=1_754_469_465_000,
            freshness_record_id="record-81",
            limit=12,
        )
    ]


def test_tool_queries_injected_qdrant_index_without_embedding_coordinator(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "tool-frame.jpg"
    evidence.write_bytes(b"jpeg")
    pool = SessionVisualSemanticStorePool(root=tmp_path / "pool")
    store = pool.resolve("user-1", "session-1")
    store.record_success(
        VisualSemanticRecord(
            record_id="record-81",
            session_id="session-1",
            video_id="video-1",
            frame_sequence=81,
            captured_at_ms=1_754_469_465_000,
            summary="画面中可见一只罗技鼠标，位于键盘右侧。",
            index_status="ready",
            evidence_ref=str(evidence),
            evidence_bytes=evidence.stat().st_size,
            created_at_ms=1_754_469_465_010,
        )
    )
    index = RecordingIndex()
    tool = VisualMemorySearchTool(
        semantic_store_pool=pool,
        text_index=index,
    )

    result = tool.run(
        VisualMemorySearchInput(query="鼠标", session_id="session-1"),
        ToolContext(user_id="user-1", session_id="session-1"),
    )

    assert result.success is True
    assert result.data["status"] == "records"
    assert result.data["observations"][0]["text"] == (
        "画面中可见一只罗技鼠标，位于键盘右侧。"
    )
    assert index.queries[0].user_id == "user-1"


def test_service_drops_qdrant_points_no_longer_retained_locally(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "retained-frame.jpg"
    evidence.write_bytes(b"jpeg")
    store = SessionVisualSemanticStore(
        root=tmp_path / "retained-store",
        session_id="session-1",
    )
    store.record_success(
        VisualSemanticRecord(
            record_id="record-81",
            session_id="session-1",
            video_id="video-1",
            frame_sequence=81,
            captured_at_ms=1_754_469_465_000,
            summary="画面中可见一只罗技鼠标。",
            index_status="ready",
            evidence_ref=str(evidence),
            evidence_bytes=evidence.stat().st_size,
            created_at_ms=1_754_469_465_010,
        )
    )

    result = VisualMemorySearchService(
        semantic_store=store,
        text_index=StaleRecordingIndex(),
    ).search(
        VisualMemorySearchRequest(
            user_id="user-1",
            session_id="session-1",
            request_id="request-2",
            query="鼠标",
        )
    )

    assert [item.text for item in result.observations] == ["画面中可见一只罗技鼠标。"]
