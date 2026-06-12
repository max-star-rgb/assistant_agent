import pytest

from multimodal_agent.agent.intent import IntentDetector
from multimodal_agent.schemas.requests import UserRequest


def request(text: str, image_ids: list[str] | None = None, video_ids: list[str] | None = None) -> UserRequest:
    return UserRequest(
        user_id="u1",
        session_id="s1",
        text=text,
        image_ids=image_ids or [],
        video_ids=video_ids or [],
    )


@pytest.mark.parametrize(
    ("user_request", "expected_intent"),
    [
        (request("图里是什么", image_ids=["img1"]), "understand_image"),
        (request("视频里发生了什么", video_ids=["video1"]), "understand_video"),
        (request("找相似款"), "product_search"),
        (request("哪个便宜"), "price_compare"),
        (request("生成海报"), "image_generation"),
        (request("放到客厅看看"), "render_3d"),
        (request("上次那个黑色包"), "memory_retrieval"),
        (request("找视频里的鞋子，比价，再生成海报", video_ids=["video1"]), "multi_tool_task"),
    ],
)
def test_detects_acceptance_intents(user_request: UserRequest, expected_intent: str) -> None:
    detector = IntentDetector()

    result = detector.detect(user_request)

    assert result.intent == expected_intent
    assert result.confidence > 0
    assert result.rationale


def test_detects_save_memory() -> None:
    result = IntentDetector().detect(request("记住我喜欢日系风"))

    assert result.intent == "save_memory"


def test_vague_reference_without_context_asks_followup() -> None:
    result = IntentDetector().detect(request("这个"))

    assert result.intent == "ask_followup"
    assert result.missing_slots == ["context"]


def test_media_without_text_asks_followup() -> None:
    result = IntentDetector().detect(request("", image_ids=["img1"]))

    assert result.intent == "ask_followup"
    assert result.missing_slots == ["text"]


def test_defaults_to_chat_without_tool_intent() -> None:
    result = IntentDetector().detect(request("这个风格怎么样"))

    assert result.intent == "direct_chat"
