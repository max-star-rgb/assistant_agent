from assistant_agent.config import ProviderConfig
from assistant_agent.media.image_to_3d import ImageTo3DSubmission
from assistant_agent.runtime import generated_artifacts
from assistant_agent.runtime.requests import AgentResponse
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.plugins.builtin.image_generation.tool import (
    _image_generation_model_observation,
)
from assistant_agent.tools.plugins.builtin.image_to_3d.tool import ImageTo3DTool


def test_config_reads_trusted_lan_artifact_base_url() -> None:
    config = ProviderConfig.from_env(
        {"ARTIFACT_BASE_URL": "http://192.168.1.20:8089/"}
    )

    assert config.artifact_base_url == "http://192.168.1.20:8089/"


def test_generated_artifact_public_url_uses_only_managed_ref() -> None:
    public_url = getattr(generated_artifacts, "generated_artifact_public_url")
    base_url = "http://192.168.1.20:8089/"

    assert public_url(
        "/artifacts/generated/image-sentinel.png",
        base_url=base_url,
    ) == "http://192.168.1.20:8089/artifacts/generated/image-sentinel.png"
    assert public_url(
        "https://provider.example/image.png",
        base_url=base_url,
    ) is None
    assert public_url(
        "/artifacts/generated/image-sentinel.png",
        base_url=None,
    ) is None
    assert public_url(
        "/artifacts/generated/image-sentinel.png",
        base_url="http://user:pass@192.168.1.20:8089",
    ) is None


def test_response_delivery_keeps_internal_ref_and_adds_public_url() -> None:
    with_delivery = getattr(generated_artifacts, "with_generated_artifact_delivery")
    response = AgentResponse(
        message="image-ready-sentinel",
        output_refs=["/artifacts/generated/image-sentinel.png"],
    )

    delivered = with_delivery(
        response,
        base_url="http://192.168.1.20:8089",
    )

    assert delivered.output_refs == response.output_refs
    assert delivered.data is not None
    assert delivered.data["artifact_urls"] == [
        "http://192.168.1.20:8089/artifacts/generated/image-sentinel.png"
    ]
    assert delivered.data["artifact_urls"][0] in delivered.message


def test_response_delivery_without_trusted_base_url_is_unchanged() -> None:
    with_delivery = getattr(generated_artifacts, "with_generated_artifact_delivery")
    response = AgentResponse(
        message="image-ready-sentinel",
        output_refs=["/artifacts/generated/image-sentinel.png"],
    )

    assert with_delivery(response, base_url=None) == response


def test_image_generation_observation_does_not_expose_artifact_path() -> None:
    observation = _image_generation_model_observation(
        {
            "status": "succeeded",
            "image_urls": ["/artifacts/generated/image-sentinel.png"],
            "image_id": ["image-sentinel"],
        }
    )

    assert observation["image_id"] == ["image-sentinel"]
    assert "images" not in observation


def test_image_to_3d_prefers_runtime_owned_latest_image() -> None:
    seen: list[str] = []

    class Adapter:
        def start(
            self,
            *,
            user_id: str,
            session_id: str,
            src_image: str,
            output_format: str,
        ) -> ImageTo3DSubmission:
            seen.append(src_image)
            return ImageTo3DSubmission(
                job_id="job-sentinel",
                status="generating",
                source_image_id=src_image,
            )

    result = ImageTo3DTool(adapter=Adapter()).run(
        {"src_image": "model-invented-id"},
        ToolContext(
            user_id="user-sentinel",
            session_id="session-sentinel",
            metadata={"latest_generated_image_id": "latest-image-sentinel"},
        ),
    )

    assert result.success is True
    assert seen == ["latest-image-sentinel"]
    assert result.data["source_image_id"] == "latest-image-sentinel"
