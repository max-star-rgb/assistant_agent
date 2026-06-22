from multimodal_agent.config import ProviderConfig
from multimodal_agent.schemas.generation import ImageGenerationResult
from multimodal_agent.services import generated_artifacts
from multimodal_agent.services.generated_artifacts import materialize_image_generation_result
from multimodal_agent.services.image_generation_adapter import (
    ImageGenerationInput,
    ImageGenerationRequest,
    MockImageGenerationAdapter,
    create_image_generation_adapter,
)
from multimodal_agent.providers.ark_image_generation import ArkImageGenerationAdapter
from multimodal_agent.providers.qwen_image_generation import QwenImageGenerationAdapter
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
    assert result.image_urls == ["local://generated/poster.png"]
    assert result.request_id == "mock_image_request_1"
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


def test_qwen_image_provider_with_dashscope_config_uses_real_adapter() -> None:
    adapter = create_image_generation_adapter(
        ProviderConfig(
            image_generation_provider="qwen",
            dashscope_api_key="test-dashscope-key",
            qwen_image_base_url="https://dashscope.local/api/v1",
            qwen_image_model="qwen-image-test",
        )
    )

    assert isinstance(adapter, QwenImageGenerationAdapter)
    assert adapter.config.api_key == "test-dashscope-key"
    assert adapter.config.base_url == "https://dashscope.local/api/v1"
    assert adapter.config.model == "qwen-image-test"


def test_ark_image_provider_with_config_uses_real_adapter() -> None:
    adapter = create_image_generation_adapter(
        ProviderConfig(
            image_generation_provider="ark",
            ark_api_key="test-ark-key",
            ark_image_base_url="https://ark.local/api/v3",
            ark_image_model="ark-image-test",
            ark_image_default_size="2K",
        )
    )

    assert isinstance(adapter, ArkImageGenerationAdapter)
    assert adapter.config.api_key == "test-ark-key"
    assert adapter.config.base_url == "https://ark.local/api/v3"
    assert adapter.config.model == "ark-image-test"
    assert adapter.config.default_size == "2K"


def test_image_generation_tool_maps_unconfigured_provider_to_failed_tool_result() -> None:
    adapter = create_image_generation_adapter(
        ProviderConfig(image_generation_provider="comfyui", comfyui_base_url=None)
    )

    result = ImageGenerationTool(adapter=adapter).run({"prompt": "生成一张产品海报"})

    assert result.success is False
    assert result.data is not None
    assert result.data["provider"] == "comfyui"
    assert "provider_unconfigured" in result.error


def test_materialize_image_generation_result_downloads_provider_url(monkeypatch, tmp_path) -> None:
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "image/png"}
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, size=-1):
            return b"fake-png-bytes"

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(generated_artifacts.urllib.request, "urlopen", fake_urlopen)
    result = ImageGenerationResult(
        task_id="task_1",
        status="succeeded",
        image_url="https://ark.example/generated.png?signature=secret",
        image_urls=["https://ark.example/generated.png?signature=secret"],
        request_id="req_1",
        prompt="生成一张图",
        provider="ark",
        model="ark-image",
        output_ref="https://ark.example/generated.png?signature=secret",
    )

    stored = materialize_image_generation_result(
        result,
        artifact_dir=tmp_path,
        public_prefix="/artifacts/generated",
        timeout_seconds=3,
    )

    assert captured == {"url": "https://ark.example/generated.png?signature=secret", "timeout": 3}
    assert stored.provider_image_urls == ["https://ark.example/generated.png?signature=secret"]
    assert stored.download_url is not None
    assert stored.download_url.startswith("/artifacts/generated/")
    assert stored.image_url == stored.download_url
    assert stored.image_urls == [stored.download_url]
    assert stored.output_ref == stored.download_url
    assert (tmp_path / stored.download_url.rsplit("/", 1)[-1]).read_bytes() == b"fake-png-bytes"


def test_image_generation_tool_outputs_backend_download_url(monkeypatch) -> None:
    class RemoteImageAdapter:
        provider = "ark"

        def generate(self, input: ImageGenerationInput) -> ImageGenerationResult:
            return ImageGenerationResult(
                task_id="task_1",
                status="succeeded",
                image_url="https://ark.example/generated.png",
                image_urls=["https://ark.example/generated.png"],
                request_id="req_1",
                prompt=input.prompt or "生成一张图",
                provider="ark",
                model="ark-image",
                output_ref="https://ark.example/generated.png",
            )

    def fake_materialize(result: ImageGenerationResult) -> ImageGenerationResult:
        return result.model_copy(
            update={
                "provider_image_urls": result.image_urls,
                "download_url": "/artifacts/generated/local.png",
                "download_urls": ["/artifacts/generated/local.png"],
                "image_url": "/artifacts/generated/local.png",
                "image_urls": ["/artifacts/generated/local.png"],
                "output_ref": "/artifacts/generated/local.png",
            }
        )

    monkeypatch.setattr("multimodal_agent.tools.image_generation_tool.materialize_image_generation_result", fake_materialize)
    result = ImageGenerationTool(adapter=RemoteImageAdapter()).run({"prompt": "生成一张图"})

    assert result.success is True
    assert result.output_ref == "/artifacts/generated/local.png"
    assert result.data is not None
    assert result.data["download_url"] == "/artifacts/generated/local.png"
    assert result.data["image_url"] == "/artifacts/generated/local.png"
    assert "provider_image_urls" not in result.data
    assert "https://ark.example/generated.png" not in str(result.data)
