from __future__ import annotations

from pathlib import Path

from assistant_agent.media.video.semantic_store import VisualSemanticRecord
from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)
from assistant_agent.media.vision.models import VisionUnderstandingRequest
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.observation import observation_from_tool_result
from assistant_agent.tools.plugins.builtin.media_inspection import video_branch
from assistant_agent.tools.plugins.builtin.media_inspection.tool import (
    LiveViewInspectTool,
)


class _FailIfCalledVisionClient:
    def understand(self, request):
        raise AssertionError("live view must read the semantic store")


def _context(*, target_sequence: int) -> ToolContext:
    return ToolContext(
        user_id="user-sentinel",
        session_id="session-sentinel",
        metadata={
            "request_metadata": {
                "transport": "agent_service_websocket",
                "gateway": {"session_config": {"entry_profile": "agent_service"}},
                "agent_service": {"visual_target_sequence": target_sequence},
            }
        },
    )


def _tool(pool: SessionVisualSemanticStorePool) -> LiveViewInspectTool:
    return LiveViewInspectTool(
        client=_FailIfCalledVisionClient(),
        semantic_store_pool=pool,
    )


def test_live_view_tool_observation_groups_only_answer_relevant_fields(
    tmp_path: Path,
) -> None:
    pool = SessionVisualSemanticStorePool(root=tmp_path / "pool")
    evidence = tmp_path / "frame-12.jpg"
    evidence.write_bytes(b"jpeg")
    pool.resolve("user-sentinel", "session-sentinel").record_success(
        VisualSemanticRecord(
            record_id="record-12",
            session_id="session-sentinel",
            video_id="video-sentinel",
            frame_sequence=12,
            captured_at_ms=12_345,
            summary="桌面上有一把钥匙",
            scene="办公桌",
            objects=["钥匙"],
            colors=["银色"],
            timestamps=[{}],
            confidence=0.95,
            provider="provider-sentinel",
            model="model-sentinel",
            search_embedding=[1.0, 0.0],
            embedding_space_id="space-sentinel",
            index_status="ready",
            evidence_ref=str(evidence),
            evidence_bytes=evidence.stat().st_size,
            created_at_ms=12_400,
        )
    )

    result = _tool(pool).run(
        VisionUnderstandingRequest(video_ids=["video-sentinel"]),
        _context(target_sequence=12),
    )
    observation = observation_from_tool_result(result)

    assert result.data is not None
    assert result.data["snapshot_sequence"] == 12
    assert result.data["provider"] == "provider-sentinel"
    assert observation.summary == "桌面上有一把钥匙"
    assert observation.data == {
        "status": "ready",
        "observations": [
            {"timestamp_ms": 12_345, "text": "桌面上有一把钥匙"}
        ],
        "freshness": {
            "observed_timestamp_ms": 12_345,
            "sequence_gap": 0,
            "fallback_used": False,
            "refresh_in_progress": False,
        },
        "usable_visual_text": True,
    }


def test_pending_live_view_observation_omits_internal_and_empty_fields(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(video_branch, "LIVE_VIEW_SNAPSHOT_WAIT_SECONDS", 0.0)
    pool = SessionVisualSemanticStorePool(root=tmp_path / "pool")
    pool.resolve("user-sentinel", "session-sentinel").mark_pending(
        "video-sentinel",
        pending_count=1,
        in_flight=True,
    )

    result = _tool(pool).run(
        VisionUnderstandingRequest(video_ids=["video-sentinel"]),
        _context(target_sequence=1),
    )
    observation = observation_from_tool_result(result)

    assert result.data is not None
    assert result.data["pending_count"] == 1
    assert result.data["media_refs"] == ["video-sentinel"]
    assert observation.data == {
        "status": "pending",
        "freshness": {
            "sequence_gap": 1,
            "fallback_used": True,
            "refresh_in_progress": True,
        },
        "usable_visual_text": False,
    }
    assert "description" not in observation.data
    assert "media_refs" not in observation.data
    assert "target_sequence" not in observation.data
    assert "snapshot_sequence" not in observation.data
