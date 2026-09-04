from pathlib import Path

from assistant_agent.tools.plugins.builtin.image_generation import backend
from assistant_agent.tools.plugins.builtin.image_generation import tool as image_tool
from assistant_agent.tools.plugins.builtin.image_generation.backend import StoredArtifact
from assistant_agent.tools.plugins.builtin.image_generation.models import (
    ImageGenerationResult,
)


def test_image_tool_materializes_provider_urls(monkeypatch, tmp_path: Path) -> None:
    materialize = getattr(image_tool, "_materialize_image_generation_result", None)
    assert callable(materialize), "image generation Tool must own result materialization"

    local_path = tmp_path / "generated.png"
    local_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(
        backend,
        "store_remote_artifact",
        lambda *args, **kwargs: StoredArtifact(
            path=local_path,
            download_url="artifact://v1/thread/generated/generated.png",
            source_url="https://provider.example/generated.png",
        ),
    )

    result = materialize(
        ImageGenerationResult(
            task_id="task-1",
            request_id="request-1",
            status="succeeded",
            image_url="https://provider.example/generated.png",
            prompt="draw a lighthouse",
        ),
        artifact_dir=tmp_path,
        public_prefix="artifact://v1/thread/generated",
    )

    assert result.provider_image_urls == [
        "https://provider.example/generated.png"
    ]
    assert result.image_urls == ["artifact://v1/thread/generated/generated.png"]
    assert result.output_ref == "artifact://v1/thread/generated/generated.png"
