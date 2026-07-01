from datetime import datetime, timezone

from assistant_agent.agent.tool_input_builder import build_tool_input
from assistant_agent.schemas.memory import MemoryItem
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult


def test_text_to_render_builds_scene_description() -> None:
    tool_input = build_tool_input(
        "render_3d",
        UserRequest(user_id="u1", session_id="s1", text="把浅灰色沙发放到北欧风客厅看看"),
        {},
    )

    assert tool_input["scene_description"] == "把浅灰色沙发放到北欧风客厅看看"
    assert tool_input["scene"] == "把浅灰色沙发放到北欧风客厅看看"
    assert tool_input["user_id"] == "u1"
    assert tool_input["session_id"] == "s1"


def test_product_search_result_feeds_render_request() -> None:
    tool_input = build_tool_input(
        "render_3d",
        UserRequest(user_id="u1", session_id="s1", text="放到现代办公室里看看"),
        {
            "step_1": ToolResult(
                tool_name="product_search",
                success=True,
                data={
                    "items": [
                        {
                            "product_id": "p-office-chair",
                            "title": "黑色人体工学办公椅",
                            "category": "办公椅",
                            "product_url": "mock://products/chair",
                            "image_url": "mock://images/chair.png",
                            "reason": "符合黑色办公椅需求",
                            "style_tags": ["modern", "office"],
                        }
                    ]
                },
            )
        },
    )

    assert tool_input["product_ref"] == "p-office-chair"
    assert tool_input["product_title"] == "黑色人体工学办公椅"
    assert tool_input["product_image_url"] == "mock://images/chair.png"
    assert tool_input["image_url"] == "mock://images/chair.png"
    assert tool_input["style"] == "modern, office"


def test_image_understanding_result_feeds_render_request() -> None:
    tool_input = build_tool_input(
        "render_3d",
        UserRequest(user_id="u1", session_id="s1", text="把图里的商品放到卧室里渲染一下", image_ids=["img1"]),
        {
            "step_1": ToolResult(
                tool_name="vision_understanding",
                success=True,
                data={
                    "summary": "图片里是一只黑色通勤包。",
                    "objects": ["通勤包"],
                    "colors": ["黑色"],
                    "materials": ["皮革"],
                    "style_tags": ["极简"],
                },
                output_ref="mock://vision/bag",
            )
        },
    )

    assert tool_input["visual_summary"] == "图片里是一只黑色通勤包。"
    assert tool_input["image_ref"] == "mock://vision/bag"
    assert tool_input["style"] == "极简"
    assert tool_input["material"] == "皮革"


def test_video_understanding_result_feeds_render_request() -> None:
    tool_input = build_tool_input(
        "render_3d",
        UserRequest(user_id="u1", session_id="s1", text="把视频里的商品做一个展厅 3D 展示", video_ids=["v1"]),
        {
            "step_1": ToolResult(
                tool_name="vision_understanding",
                success=True,
                data={
                    "summary": "视频中展示了一双白色低帮运动鞋。",
                    "objects": ["运动鞋"],
                    "actions": ["旋转展示"],
                    "scene": "展台",
                },
                output_ref="mock://vision/video-sneaker",
            )
        },
    )

    assert tool_input["video_summary"] == "视频中展示了一双白色低帮运动鞋。"
    assert tool_input["video_ref"] == "mock://vision/video-sneaker"
    assert tool_input["image_ref"] == "mock://vision/video-sneaker"


def test_memory_result_feeds_render_request() -> None:
    memory = MemoryItem(
        memory_id="m1",
        user_id="u1",
        memory_type="product",
        content={"item": "黑色通勤包", "style": "极简"},
        summary="用户上次关注了一个黑色通勤包。",
        relevance=0.9,
        reason="query 命中历史商品描述",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    tool_input = build_tool_input(
        "render_3d",
        UserRequest(user_id="u1", session_id="s1", text="把上次那个黑色包放到极简客厅里看看"),
        {
            "step_1": ToolResult(
                tool_name="memory_retrieval",
                success=True,
                data={"items": [memory.model_dump(mode="json")]},
                output_ref="mock://memory/m1",
            )
        },
    )

    assert tool_input["product_ref"] == "黑色通勤包"
    assert tool_input["style"] == "极简"
    assert tool_input["memory_context"] == ["用户上次关注了一个黑色通勤包。"]
