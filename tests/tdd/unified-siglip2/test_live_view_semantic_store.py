from __future__ import annotations

from pathlib import Path

from assistant_agent.config import ProviderConfig
from assistant_agent.media.video.semantic_store import VisualSemanticRecord
from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)
from assistant_agent.media.vision.models import VisionUnderstandingRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.ids import LIVE_VIEW_INSPECT_TOOL_NAME
from assistant_agent.tools.plugins.builtin.media_inspection.tool import (
    LiveViewInspectTool,
)


class _FailIfCalledVisionClient:
    def understand(self, request):
        raise AssertionError("live view must not call a vision provider")


def _add_record(
    pool: SessionVisualSemanticStorePool,
    tmp_path: Path,
    *,
    sequence: int,
) -> None:
    evidence = tmp_path / f"frame-{sequence}.jpg"
    evidence.write_bytes(b"jpeg")
    pool.resolve("user-1", "session-1").record_success(
        VisualSemanticRecord(
            record_id=f"record-{sequence}",
            session_id="session-1",
            video_id="video-1",
            frame_sequence=sequence,
            captured_at_ms=sequence * 1000,
            summary="厨房台面上有一把钥匙",
            scene="厨房",
            objects=["钥匙"],
            search_embedding=[1.0, 0.0],
            embedding_space_id="siglip2:test",
            index_status="ready",
            evidence_ref=str(evidence),
            evidence_bytes=evidence.stat().st_size,
            created_at_ms=sequence * 1000,
        )
    )


def _live_context(*, target_sequence: int) -> ToolContext:
    return ToolContext(
        user_id="user-1",
        session_id="session-1",
        metadata={
            "request_metadata": {
                "transport": "agent_service_websocket",
                "gateway": {"session_config": {"entry_profile": "agent_service"}},
                "agent_service": {"visual_target_sequence": target_sequence},
            }
        },
    )


def test_live_view_reads_semantic_store_without_provider(tmp_path: Path) -> None:
    pool = SessionVisualSemanticStorePool(root=tmp_path / "pool")
    _add_record(pool, tmp_path, sequence=12)
    tool = LiveViewInspectTool(
        client=_FailIfCalledVisionClient(),
        semantic_store_pool=pool,
    )

    result = tool.run(
        VisionUnderstandingRequest(video_ids=["video-1"]),
        _live_context(target_sequence=12),
    )

    assert result.success is True
    assert result.model_observation["scene"] == "厨房"
    assert result.model_observation["objects"] == ["钥匙"]
    assert result.model_observation["snapshot_sequence"] == 12


def test_runtime_and_live_tool_share_visual_semantic_pool(tmp_path: Path) -> None:
    runtime = AgentGraphRuntime(
        config=ProviderConfig(),
        visual_semantic_store_pool=SessionVisualSemanticStorePool(
            root=tmp_path / "pool"
        ),
    )
    try:
        tool = runtime.registry.get(LIVE_VIEW_INSPECT_TOOL_NAME)

        assert tool.semantic_store_pool is runtime.visual_semantic_store_pool
    finally:
        runtime.close()
