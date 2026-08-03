import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.media.video.detection.vision_embedding_provider import (
    MockVisionEmbeddingProvider,
    VisionEmbeddingResult,
)
from assistant_agent.media.video.types import VideoFrame


def test_real_mode_explicitly_configures_local_siglip2_image_embeddings() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "test-key-sentinel",
            "MULTIMODAL_AGENT_VISION_EMBEDDING_PROVIDER": "local_siglip2",
            "SIGLIP2_VISION_MODEL_DIR": "/models/siglip2",
        }
    )

    assert config.vision_embedding_provider == "local_siglip2"
    assert config.siglip2_vision_model_dir == "/models/siglip2"
    assert config.siglip2_cuda_device_id == 0
    assert config.keyframe_max_interval_seconds == 10.0
    assert config.keyframe_semantic_probe_fps == 2.0
    assert config.keyframe_structural_threshold == 0.35
    assert config.keyframe_semantic_threshold == 0.18
    assert config.keyframe_combined_threshold == 0.25


def test_mock_mode_does_not_enable_local_siglip2_model_loading() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
            "MULTIMODAL_AGENT_VISION_EMBEDDING_PROVIDER": "local_siglip2",
            "SIGLIP2_VISION_MODEL_DIR": "/models/siglip2",
        }
    )

    assert config.vision_embedding_provider == "mock"


def test_image_embedding_result_identifies_future_cross_modal_space() -> None:
    result = VisionEmbeddingResult(
        embedding=[1.0, 0.0],
        provider="local_siglip2",
        model="google/siglip2-base-patch16-224",
        model_family="siglip2",
        model_revision="revision-sentinel",
        embedding_space_id=(
            "siglip2-base-p16-224@revision-sentinel:vision-pool-v1"
        ),
        dimension=2,
        normalized=True,
    )

    assert result.embedding_space_id == (
        "siglip2-base-p16-224@revision-sentinel:vision-pool-v1"
    )
    assert result.model_family == "siglip2"
    assert result.dimension == 2
    assert result.normalized is True


def test_embedding_provider_contract_is_image_specific() -> None:
    provider = MockVisionEmbeddingProvider()
    frame = VideoFrame(
        frame_id="frame-sentinel",
        timestamp_seconds=1.0,
        metadata={"embedding": [0.25, 0.75]},
    )

    result = provider.embed_image(frame)

    assert result.embedding == [0.25, 0.75]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("keyframe_max_interval_seconds", 0.0),
        ("keyframe_semantic_probe_fps", 0.0),
        ("keyframe_structural_threshold", 1.01),
        ("keyframe_semantic_threshold", -0.01),
        ("keyframe_combined_threshold", 1.01),
        ("siglip2_cuda_device_id", -1),
    ],
)
def test_keyframe_config_rejects_invalid_local_model_values(
    field: str,
    value: float,
) -> None:
    with pytest.raises(ValueError, match="keyframe|siglip2"):
        ProviderConfig(**{field: value})
