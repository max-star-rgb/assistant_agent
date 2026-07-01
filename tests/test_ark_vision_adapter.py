import json
import sys
import types
import builtins
from pathlib import Path

import pytest

from assistant_agent.providers.ark_vision import (
    ArkVisionProviderAdapter,
    ArkVisionProviderConfig,
    ark_image_url,
    build_ark_vision_input,
    extract_ark_response_text,
)
from assistant_agent.services.provider_errors import ProviderAdapterError
from assistant_agent.services.vision_adapter import VisionUnderstandingInput


def test_build_ark_vision_input_uses_file_url_for_local_images(tmp_path) -> None:
    image = tmp_path / "demo.png"
    image.write_bytes(b"fake")

    payload = build_ark_vision_input(VisionUnderstandingInput(image_ids=[str(image)], question="图里是什么？"))

    content = payload[0]["content"]
    assert content[0]["type"] == "input_image"
    assert content[0]["image_url"] == f"file://{image.resolve()}"
    assert content[1]["type"] == "input_text"
    assert "图里是什么？" in content[1]["text"]
    assert "JSON object" in content[1]["text"]


def test_build_ark_vision_input_keeps_multiple_remote_images() -> None:
    payload = build_ark_vision_input(
        VisionUnderstandingInput(
            image_ids=["https://example.com/a.png", "https://example.com/b.png"],
            question="比较两张图",
        )
    )

    content = payload[0]["content"]
    assert [item["image_url"] for item in content if item["type"] == "input_image"] == [
        "https://example.com/a.png",
        "https://example.com/b.png",
    ]


def test_ark_image_url_rejects_missing_local_file() -> None:
    with pytest.raises(ValueError):
        ark_image_url("/tmp/does-not-exist-image.png")


def test_extract_ark_response_text_supports_output_text_and_nested_output() -> None:
    assert extract_ark_response_text({"output_text": '{"summary":"ok"}'}) == '{"summary":"ok"}'
    assert extract_ark_response_text({"output": [{"content": [{"text": '{"summary":"nested"}'}]}]}) == '{"summary":"nested"}'


def test_ark_vision_adapter_calls_sdk_responses_create(monkeypatch, tmp_path) -> None:
    image = tmp_path / "demo.png"
    image.write_bytes(b"fake")
    captured = {}

    class FakeResponses:
        def create(self, *, model, input):
            captured["model"] = model
            captured["input"] = input
            return types.SimpleNamespace(
                output_text=json.dumps(
                    {
                        "objects": ["蛋糕"],
                        "colors": ["白色"],
                        "materials": ["奶油"],
                        "scene": "餐桌",
                        "style_tags": ["写实"],
                        "text_in_media": [],
                        "summary": "图片里是一个蛋糕。",
                    },
                    ensure_ascii=False,
                )
            )

    class FakeArk:
        def __init__(self, *, base_url, api_key):
            captured["base_url"] = base_url
            captured["api_key"] = api_key
            self.responses = FakeResponses()

    fake_module = types.SimpleNamespace(Ark=FakeArk)
    monkeypatch.setitem(sys.modules, "volcenginesdkarkruntime", fake_module)

    adapter = ArkVisionProviderAdapter(
        ArkVisionProviderConfig(api_key="ark-test-key", base_url="https://ark.local/api/v3", model="ark-vision-test")
    )
    result = adapter.understand(VisionUnderstandingInput(image_ids=[str(image)], question="图里是什么？"))

    assert captured["base_url"] == "https://ark.local/api/v3"
    assert captured["api_key"] == "ark-test-key"
    assert captured["model"] == "ark-vision-test"
    assert captured["input"][0]["content"][0]["image_url"] == f"file://{image.resolve()}"
    assert result.objects == ["蛋糕"]
    assert result.summary == "图片里是一个蛋糕。"


def test_ark_vision_adapter_missing_sdk_returns_dependency_error(monkeypatch, tmp_path) -> None:
    image = tmp_path / "demo.png"
    image.write_bytes(b"fake")
    monkeypatch.delitem(sys.modules, "volcenginesdkarkruntime", raising=False)
    original_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "volcenginesdkarkruntime":
            raise ImportError("missing test dependency")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    adapter = ArkVisionProviderAdapter(ArkVisionProviderConfig(api_key="ark-test-key"))

    with pytest.raises(ProviderAdapterError) as exc_info:
        adapter.understand(VisionUnderstandingInput(image_ids=[str(image)], question="图里是什么？"))

    assert exc_info.value.code == "provider_unconfigured"
    assert "volcenginesdkarkruntime" in exc_info.value.message
