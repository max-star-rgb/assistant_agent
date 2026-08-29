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
    def __init__(self, output_ref: str) -> None:
        self.output_ref = output_ref

    def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        return ImageGenerationResult(
            task_id="cake-task",
            status="succeeded",
            prompt=request.prompt,
            image_id=["cake"],
            download_url=self.output_ref,
            download_urls=[self.output_ref],
            output_ref=self.output_ref,
            provider_image_urls=["https://provider.example/cake.png?signature=secret"],
        )


def test_model_observation_exposes_only_backend_owned_image_url(tmp_path) -> None:
    thread_ref = "a" * 32
    output_ref = f"/artifacts/{thread_ref}/generated/cake.png"
    result = _execute_image_generation(
        _GeneratedImageAdapter(output_ref),
        ImageGenerationRequest(prompt="cake"),
        artifact_dir=tmp_path,
        public_prefix=f"/artifacts/{thread_ref}/generated",
        artifact_base_url="http://127.0.0.1:8089",
    )

    content, artifact = native_tool_response("image_generation", result)
    observation = json.loads(content[0]["text"])

    assert "image_id" not in observation
    assert observation["images"] == [
        {
            "image_id": "cake",
            "url": f"http://127.0.0.1:8089{output_ref}",
        }
    ]
    assert artifact["images"][0]["url"] == observation["images"][0]["url"]
    assert "provider.example" not in content[0]["text"]
