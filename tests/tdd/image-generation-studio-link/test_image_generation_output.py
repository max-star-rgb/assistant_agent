import json

from assistant_agent.tools.native_boundary import native_tool_response
from assistant_agent.tools.plugins.builtin.image_generation.models import (
    ImageGenerationRequest,
    ImageGenerationResult,
)
from assistant_agent.tools.plugins.builtin.image_generation.tool import (
    _execute_image_generation,
)


class _GeneratedImageAdapter:
    def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        return ImageGenerationResult(
            task_id="cake-task",
            status="succeeded",
            prompt=request.prompt,
            image_id=["cake"],
            download_url="/artifacts/generated/cake.png",
            download_urls=["/artifacts/generated/cake.png"],
            output_ref="/artifacts/generated/cake.png",
            provider_image_urls=["https://provider.example/cake.png?signature=secret"],
        )


def test_model_observation_exposes_only_backend_owned_image_url() -> None:
    result = _execute_image_generation(
        _GeneratedImageAdapter(),
        ImageGenerationRequest(prompt="cake"),
        artifact_base_url="http://127.0.0.1:8089",
    )

    content, artifact = native_tool_response("image_generation", result)
    observation = json.loads(content[0]["text"])

    assert observation["images"] == [
        {
            "image_id": "cake",
            "url": "http://127.0.0.1:8089/artifacts/generated/cake.png",
        }
    ]
    assert artifact["images"][0]["url"] == observation["images"][0]["url"]
    assert "provider.example" not in content[0]["text"]
