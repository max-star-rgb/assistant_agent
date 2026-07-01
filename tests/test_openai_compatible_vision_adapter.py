import base64
import json

from assistant_agent.services.real_vision_adapter import (
    build_openai_vision_payload,
    chat_completions_url,
    image_to_data_url,
    parse_openai_vision_response,
)
from assistant_agent.services.vision_adapter import VisionUnderstandingInput


def test_chat_completions_url_accepts_base_url_or_full_endpoint() -> None:
    assert chat_completions_url("https://example.com/v1") == "https://example.com/v1/chat/completions"
    assert (
        chat_completions_url("https://example.com/v1/chat/completions")
        == "https://example.com/v1/chat/completions"
    )


def test_local_image_path_is_encoded_as_data_url(tmp_path) -> None:
    image = tmp_path / "shoe.jpg"
    image.write_bytes(b"fake-image")

    data_url = image_to_data_url(str(image))

    assert data_url.startswith("data:image/jpeg;base64,")
    assert data_url.endswith(base64.b64encode(b"fake-image").decode("ascii"))


def test_openai_vision_payload_uses_messages_and_image_url(tmp_path) -> None:
    image = tmp_path / "shoe.jpg"
    image.write_bytes(b"fake-image")

    payload = build_openai_vision_payload(
        VisionUnderstandingInput(image_ids=[str(image)], question="图里是什么"),
        model="qwen-vl-plus",
    )

    assert payload["model"] == "qwen-vl-plus"
    assert payload["response_format"] == {"type": "json_object"}
    message = payload["messages"][0]
    assert message["role"] == "user"
    assert message["content"][0]["type"] == "text"
    assert message["content"][1]["type"] == "image_url"
    assert message["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_parse_openai_vision_response_from_json_content() -> None:
    content = {
        "objects": ["鞋子"],
        "colors": ["白色"],
        "materials": ["皮革"],
        "scene": "室内",
        "style_tags": ["简约"],
        "text_in_media": [],
        "summary": "图片中是一双白色鞋子。",
    }

    result = parse_openai_vision_response(
        {"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]}
    )

    assert result.objects == ["鞋子"]
    assert result.summary == "图片中是一双白色鞋子。"
