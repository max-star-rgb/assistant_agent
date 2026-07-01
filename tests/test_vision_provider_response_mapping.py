import json

from assistant_agent.config import ProviderConfig
from assistant_agent.services.real_vision_adapter import (
    parse_openai_vision_response,
)
from assistant_agent.tools.registry import create_default_registry


def response(content) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def test_maps_complete_openai_compatible_response() -> None:
    result = parse_openai_vision_response(
        response(
            json.dumps(
                {
                    "objects": ["鞋子"],
                    "colors": ["白色"],
                    "materials": ["皮革"],
                    "scene": "室内",
                    "style_tags": ["简约"],
                    "text_in_media": ["logo"],
                    "summary": "图片中是一双白色鞋子。",
                },
                ensure_ascii=False,
            )
        )
    )

    assert result.objects == ["鞋子"]
    assert result.colors == ["白色"]
    assert result.summary == "图片中是一双白色鞋子。"


def test_maps_missing_fields_to_stable_defaults() -> None:
    result = parse_openai_vision_response(response(json.dumps({"objects": ["杯子"]}, ensure_ascii=False)))

    assert result.objects == ["杯子"]
    assert result.colors == []
    assert result.materials == []
    assert result.scene == ""
    assert result.style_tags == []
    assert result.text_in_media == []
    assert result.summary == "视觉结果包含：杯子。"


def test_empty_response_is_bad_response() -> None:
    try:
        parse_openai_vision_response(response("{}"))
    except ValueError as exc:
        assert "empty" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_non_json_response_is_bad_response() -> None:
    try:
        parse_openai_vision_response(response("not json"))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_wrong_field_types_are_bad_response() -> None:
    try:
        parse_openai_vision_response(response(json.dumps({"objects": "鞋子", "summary": "x"}, ensure_ascii=False)))
    except ValueError as exc:
        assert "objects must be a list" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_provider_bad_response_does_not_fallback_to_mock() -> None:
    registry = create_default_registry(
        ProviderConfig(
            vision_provider="qwen",
            qwen_api_key=None,
        )
    )

    result = registry.run("vision_understanding", {"image_ids": ["image1"], "question": "图里是什么"})

    assert result.success is False
    assert result.error is not None
    assert result.error.startswith("provider_unconfigured:")
    assert result.output_ref != "mock://vision/white-low-top-sneaker"
