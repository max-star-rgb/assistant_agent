from multimodal_agent.config import ProviderConfig
from multimodal_agent.services.image_generation_adapter import (
    ImageGenerationInput,
    ImageGenerationRequest,
    MockImageGenerationAdapter,
    create_image_generation_adapter,
)
from multimodal_agent.tools.image_generation_tool import ImageGenerationTool


def test_image_generation_request_alias_accepts_text_only_prompt() -> None:
    request = ImageGenerationRequest(prompt="生成一张赛博朋克风格海报", width=1024, height=1024)

    assert request.prompt == "生成一张赛博朋克风格海报"
    assert request.reference_image_ids == []


def test_mock_image_generation_adapter_returns_provider_metadata_and_output_ref() -> None:
    result = MockImageGenerationAdapter().generate(ImageGenerationInput(prompt="生成一张赛博朋克风格海报"))

    assert result.status == "succeeded"
    assert result.provider == "mock"
    assert result.model == "mock-image-generation"
    assert result.output_ref == "local://generated/poster.png"
    assert result.prompt_used == result.prompt
    assert result.errors == []


def test_create_image_generation_adapter_defaults_to_mock() -> None:
    adapter = create_image_generation_adapter(ProviderConfig())

    result = adapter.generate(ImageGenerationInput(prompt="做一张小红书封面"))

    assert result.status == "succeeded"
    assert result.provider == "mock"


def test_real_image_provider_without_config_returns_provider_unconfigured() -> None:
    adapter = create_image_generation_adapter(
        ProviderConfig(image_generation_provider="openai", openai_api_key=None)
    )

    result = adapter.generate(ImageGenerationInput(prompt="生成一张产品海报"))

    assert result.status == "failed"
    assert result.provider == "openai"
    assert result.errors[0]["code"] == "provider_unconfigured"


def test_image_generation_tool_maps_unconfigured_provider_to_failed_tool_result() -> None:
    adapter = create_image_generation_adapter(
        ProviderConfig(image_generation_provider="comfyui", comfyui_base_url=None)
    )

    result = ImageGenerationTool(adapter=adapter).run({"prompt": "生成一张产品海报"})

    assert result.success is False
    assert result.data is not None
    assert result.data["provider"] == "comfyui"
    assert "provider_unconfigured" in result.error
