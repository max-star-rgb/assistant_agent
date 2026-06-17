import pytest

from multimodal_agent.agent.intent import IntentDetector
from multimodal_agent.agent.runtime import AgentGraphRuntime
from multimodal_agent.memory.store import InMemoryStore
from multimodal_agent.schemas.requests import UserRequest


def _run(text: str, *, image: bool = False, video: bool = False):
    return AgentGraphRuntime(memory_store=InMemoryStore()).run_state(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text=text,
            image_ids=["img1"] if image else [],
            video_ids=["video1"] if video else [],
        )
    )


@pytest.mark.parametrize(
    ("text", "media", "expected_tool"),
    [
        ("图里是什么？请简要描述主要物体、颜色、材质和场景。", "image", "vision_understanding"),
        ("请描述这张图片的场景。", "image", "vision_understanding"),
        ("这个视频里的场景发生了什么？", "video", "video_understanding"),
        ("画面中的主要场景是什么？", "image", "vision_understanding"),
        ("分析一下图片中的物体和场景。", "image", "vision_understanding"),
    ],
)
def test_scene_description_does_not_trigger_render_3d(text: str, media: str, expected_tool: str) -> None:
    state = _run(text, image=media == "image", video=media == "video")

    assert state.status == "completed"
    assert state.intent is not None
    assert state.intent.intent in {"image_understanding", "video_understanding"}
    assert [call.tool_name for call in state.tool_calls] == [expected_tool]
    assert "render_3d" not in [call.tool_name for call in state.tool_calls]
    assert "mock://render/preview.png" not in (state.response.message if state.response else "")


@pytest.mark.parametrize(
    ("text", "image", "expected_tools"),
    [
        ("根据这张图创建一个 3D 场景预览。", True, ["vision_understanding", "render_3d"]),
        ("把这个商品放进一个客厅场景里渲染。", False, ["render_3d"]),
        ("生成一个三维商品展示场景。", False, ["render_3d"]),
        ("请用 3D 方式建模这个场景。", False, ["render_3d"]),
        ("渲染一个包含这个商品的展示空间。", False, ["render_3d"]),
    ],
)
def test_strong_render_intent_still_triggers_render_3d(
    text: str,
    image: bool,
    expected_tools: list[str],
) -> None:
    state = _run(text, image=image)

    assert state.status == "completed"
    assert [call.tool_name for call in state.tool_calls] == expected_tools
    assert "render_3d" in [call.tool_name for call in state.tool_calls]


def test_rule_decision_scene_description_has_no_render_capability() -> None:
    decision = IntentDetector().detect_decision(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="图里是什么？请简要描述主要物体、颜色、材质和场景。",
            image_ids=["img1"],
        )
    )

    assert decision.capabilities == ["image_understanding"]
    assert [step.tool_name for step in decision.plan_steps] == ["vision_understanding"]
