from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import HumanMessage

from assistant_agent.agent_server.media_app import media_graph_input
from assistant_agent.media.runtime_media import latest_runtime_media
from assistant_agent.media.visual_perception.module import (
    VisualPerceptionModule,
    VisualTargetWindow,
)
from assistant_agent.tools.runtime import latest_human_request


def test_media_projects_text_only_not_live_camera_block() -> None:
    """Regression: the frozen camera window must NOT reach the main LLM messages."""

    graph_input = media_graph_input(
        SimpleNamespace(text="现在看到了什么", execution_mode="fast"),
    )
    content = graph_input["messages"][0]["content"]

    assert graph_input["execution_mode"] == "fast"
    assert content == [{"type": "text", "text": "现在看到了什么"}]
    assert not any(
        isinstance(block, dict) and block.get("type") == "video"
        for block in content
    )


def test_live_view_window_resolves_from_vlm_side_module() -> None:
    """Regression: window boundary is owned by the process module, not messages."""

    module = VisualPerceptionModule()
    module.record_live_view(
        "user-1",
        "thread-1",
        video_ids=["video-window"],
        window=VisualTargetWindow(
            window_id="visual-window-test",
            video_id="video-window",
            start_sequence=4,
            target_sequence=8,
            sequences=(4, 5, 6, 7, 8),
        ),
    )

    projection = module.resolve_live_view("user-1", "thread-1")
    assert projection is not None
    assert projection.live_video_ids == ("video-window",)
    assert projection.window_id == "visual-window-test"
    assert projection.window_start_sequence == 4
    assert projection.target_sequence == 8
    assert projection.target_video_id == "video-window"
    # a different session must not observe it
    assert module.resolve_live_view("user-1", "thread-2") is None


def test_uploaded_or_malformed_video_blocks_cannot_claim_a_strict_window() -> None:
    """Regression: untrusted upload metadata must not control live as-of boundaries."""

    state = {
        "messages": [
            HumanMessage(
                content=[
                    {
                        "type": "video",
                        "id": "uploaded-video",
                        "source": "uploaded",
                        "window_id": "visual-window-uploaded",
                        "window_start_sequence": 4,
                        "target_sequence": 8,
                    },
                    {
                        "type": "video",
                        "id": "live-video",
                        "source": "live_camera",
                        "window_id": "",
                        "window_start_sequence": True,
                        "target_sequence": 8,
                    },
                ]
            )
        ]
    }

    media = latest_runtime_media(state)
    request = latest_human_request(state)

    assert media.live_video_ids == ("live-video",)
    assert media.visual_window_id is None
    assert media.visual_window_start_sequence is None
    assert media.visual_target_sequence is None
    assert "visual_window_id" not in request
    assert "visual_window_start_sequence" not in request
    assert "visual_target_sequence" not in request