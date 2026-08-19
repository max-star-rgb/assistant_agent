from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import HumanMessage

from assistant_agent.agent_server.media_app import media_graph_input
from assistant_agent.media.runtime_media import latest_runtime_media
from assistant_agent.tools.runtime import latest_human_request


def test_media_projects_one_trusted_window_into_native_message_content() -> None:
    """Regression: projecting only target sequence loses the frozen 4-8 boundary."""

    graph_input = media_graph_input(
        SimpleNamespace(text="现在看到了什么", execution_mode="fast"),
        video_ids=["video-window"],
        visual_window_id="visual-window-test",
        visual_window_start_sequence=4,
        visual_target_sequence=8,
        visual_target_video_id="video-window",
    )
    content = graph_input["messages"][0]["content"]
    state = {"messages": [HumanMessage(content=content)]}

    media = latest_runtime_media(state)
    request = latest_human_request(state)

    assert graph_input["execution_mode"] == "fast"
    assert content[1] == {
        "type": "video",
        "id": "video-window",
        "source": "live_camera",
        "window_id": "visual-window-test",
        "window_start_sequence": 4,
        "target_sequence": 8,
    }
    assert media.visual_window_id == "visual-window-test"
    assert media.visual_window_start_sequence == 4
    assert media.visual_target_sequence == 8
    assert request["visual_window_id"] == "visual-window-test"
    assert request["visual_window_start_sequence"] == 4
    assert request["visual_target_sequence"] == 8


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

