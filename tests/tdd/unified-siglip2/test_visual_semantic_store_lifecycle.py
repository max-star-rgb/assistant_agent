from pathlib import Path

from assistant_agent.config import ProviderConfig
from assistant_agent.media.video.semantic_store import VisualSemanticRecord
from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)
from assistant_agent.runtime.runtime import AgentGraphRuntime


def test_runtime_coordinator_has_no_image_timeline(tmp_path: Path) -> None:
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
        visual_semantic_store_pool=SessionVisualSemanticStorePool(
            root=tmp_path / "pool"
        ),
    )
    try:
        coordinator = runtime.embedding_coordinator_store.resolve(
            "user-1",
            "session-1",
        )

        assert hasattr(coordinator, "temporal_visual_memory") is False
    finally:
        runtime.close()


def test_session_clear_removes_records_and_evidence(tmp_path: Path) -> None:
    pool = SessionVisualSemanticStorePool(root=tmp_path / "pool")
    source = tmp_path / "frame.jpg"
    source.write_bytes(b"jpeg")
    record = pool.resolve("user-1", "session-1").record_success(
        VisualSemanticRecord(
            record_id="record-1",
            session_id="session-1",
            video_id="video-1",
            frame_sequence=1,
            summary="桌面上有钥匙",
            objects=["钥匙"],
            search_embedding=[1.0, 0.0],
            embedding_space_id="siglip2:test",
            index_status="ready",
            evidence_ref=str(source),
            evidence_bytes=source.stat().st_size,
            created_at_ms=1,
        )
    )
    owned_evidence = Path(record.evidence_ref)

    assert pool.clear_session("user-1", "session-1") is True
    assert owned_evidence.exists() is False
