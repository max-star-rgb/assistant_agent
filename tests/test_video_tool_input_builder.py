from multimodal_agent.agent.tool_input_builder import build_tool_input
from multimodal_agent.schemas.requests import UserRequest
from multimodal_agent.schemas.tools import ToolResult


VIDEO_DATA = {
    "summary": "视频中展示了一双白色低帮运动鞋，整体为简约日系商品展示风格。",
    "objects": ["白色低帮运动鞋", "桌面"],
    "products": ["白色低帮运动鞋"],
    "colors": ["白色"],
    "materials": ["皮革", "橡胶"],
    "style_tags": ["简约", "日系"],
}


def _request(text: str = "找视频里的商品") -> UserRequest:
    return UserRequest(user_id="u1", session_id="s1", text=text, video_ids=["video1"])


def _video_result() -> ToolResult:
    return ToolResult(
        tool_name="video_understanding",
        success=True,
        data=VIDEO_DATA,
        output_ref="mock://video/understanding/video1",
    )


def test_understand_video_input_uses_video_ref() -> None:
    payload = build_tool_input("understand_video", _request("总结这个视频"), {})

    assert payload["video_ref"] == "video1"
    assert payload["user_query"] == "总结这个视频"
    assert payload["user_id"] == "u1"
    assert payload["session_id"] == "s1"


def test_video_result_feeds_product_search_input() -> None:
    payload = build_tool_input("search_product", _request(), {"step_1": _video_result()})

    assert payload["visual_summary"] == VIDEO_DATA["summary"]
    assert payload["video_summary"] == VIDEO_DATA["summary"]
    assert payload["objects"] == ["白色低帮运动鞋", "桌面"]
    assert payload["colors"] == ["白色"]
    assert payload["materials"] == ["皮革", "橡胶"]


def test_video_result_feeds_image_generation_prompt() -> None:
    payload = build_tool_input("generate_image", _request("根据这个视频生成海报"), {"step_1": _video_result()})

    assert "视频中展示了一双白色低帮运动鞋" in payload["prompt"]
    assert payload["reference_image_ids"] == []


def test_video_result_feeds_render_request() -> None:
    payload = build_tool_input("render_3d", _request("做一个展厅 3D 展示"), {"step_1": _video_result()})

    assert payload["video_summary"] == VIDEO_DATA["summary"]
    assert payload["video_ref"] == "mock://video/understanding/video1"
    assert payload["image_ref"] == "mock://video/understanding/video1"


def test_video_result_feeds_memory_save_content() -> None:
    payload = build_tool_input("save_memory", _request("记住这个视频里的商品风格"), {"step_1": _video_result()})

    assert payload["action"] == "save"
    assert payload["content"]["summary"] == VIDEO_DATA["summary"]
    assert payload["content"]["products"] == ["白色低帮运动鞋"]
    assert payload["content"]["video_ref"] == "mock://video/understanding/video1"
