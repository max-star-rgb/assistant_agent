from __future__ import annotations

import pytest

from assistant_agent.config import ProviderConfig


def _real_env(**values: str) -> dict[str, str]:
    return {
        "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
        "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
        "DEEPSEEK_API_KEY": "test-key-sentinel",
        **values,
    }


def test_canonical_embedding_environment_is_loaded() -> None:
    config = ProviderConfig.from_env(
        _real_env(
            MULTIMODAL_AGENT_EMBEDDING_PROVIDER="local_siglip2",
            SIGLIP2_MODEL_DIR="/models/canonical",
            SIGLIP2_CUDA_DEVICE_ID="2",
        )
    )

    assert config.embedding_provider == "local_siglip2"
    assert config.siglip2_model_dir == "/models/canonical"
    assert config.embedding_cuda_device_id == 2


def test_legacy_embedding_environment_populates_canonical_config() -> None:
    config = ProviderConfig.from_env(
        _real_env(
            MULTIMODAL_AGENT_VISION_EMBEDDING_PROVIDER="local_siglip2",
            SIGLIP2_VISION_MODEL_DIR="/models/legacy",
            SIGLIP2_CUDA_DEVICE_ID="3",
        )
    )

    assert config.embedding_provider == "local_siglip2"
    assert config.siglip2_model_dir == "/models/legacy"
    assert config.embedding_cuda_device_id == 3
    assert config.vision_embedding_provider == "local_siglip2"
    assert config.siglip2_vision_model_dir == "/models/legacy"


@pytest.mark.parametrize(
    ("canonical_name", "canonical_value", "legacy_name", "legacy_value", "code"),
    [
        (
            "MULTIMODAL_AGENT_EMBEDDING_PROVIDER",
            "local_siglip2",
            "MULTIMODAL_AGENT_VISION_EMBEDDING_PROVIDER",
            "dashscope",
            "conflicting_embedding_provider",
        ),
        (
            "SIGLIP2_MODEL_DIR",
            "/models/new",
            "SIGLIP2_VISION_MODEL_DIR",
            "/models/old",
            "conflicting_siglip2_model_dir",
        ),
    ],
)
def test_conflicting_canonical_and_legacy_environment_fails(
    canonical_name: str,
    canonical_value: str,
    legacy_name: str,
    legacy_value: str,
    code: str,
) -> None:
    with pytest.raises(ValueError, match=code):
        ProviderConfig.from_env(
            _real_env(
                **{
                    canonical_name: canonical_value,
                    legacy_name: legacy_value,
                }
            )
        )


def test_mock_mode_cannot_enable_real_embedding_provider() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
            "MULTIMODAL_AGENT_EMBEDDING_PROVIDER": "local_siglip2",
            "SIGLIP2_MODEL_DIR": "/models/canonical",
        }
    )

    assert config.embedding_provider == "mock"


def test_semantic_input_defaults_to_five_fps() -> None:
    config = ProviderConfig.from_env(
        {"MULTIMODAL_AGENT_PROVIDER_MODE": "mock"}
    )

    assert config.semantic_input_fps == 5.0
    assert config.keyframe_min_interval_seconds == 0.5


def test_legacy_semantic_probe_fps_populates_canonical_config() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
            "REALTIME_KEYFRAME_SEMANTIC_PROBE_FPS": "4",
        }
    )

    assert config.semantic_input_fps == 4.0


def test_conflicting_semantic_input_alias_fails() -> None:
    with pytest.raises(ValueError, match="conflicting_semantic_input_fps"):
        ProviderConfig.from_env(
            {
                "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
                "REALTIME_SEMANTIC_INPUT_FPS": "5",
                "REALTIME_KEYFRAME_SEMANTIC_PROBE_FPS": "2",
            }
        )


@pytest.mark.parametrize(
    "removed_name",
    [
        "REALTIME_KEYFRAME_STRUCTURAL_THRESHOLD",
        "REALTIME_KEYFRAME_COMBINED_THRESHOLD",
    ],
)
def test_removed_structural_keyframe_config_is_rejected(removed_name: str) -> None:
    with pytest.raises(ValueError, match="removed_realtime_keyframe_config"):
        ProviderConfig.from_env(
            {
                "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
                removed_name: "0.25",
            }
        )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("REALTIME_SEMANTIC_INPUT_FPS", "0"),
        ("REALTIME_KEYFRAME_MIN_INTERVAL_SECONDS", "-0.1"),
        ("REALTIME_KEYFRAME_SEMANTIC_THRESHOLD", "1.1"),
    ],
)
def test_invalid_semantic_keyframe_config_is_rejected(
    name: str,
    value: str,
) -> None:
    with pytest.raises(ValueError):
        ProviderConfig.from_env(
            {
                "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
                name: value,
            }
        )


def test_keyframe_min_interval_cannot_exceed_max_interval() -> None:
    with pytest.raises(ValueError, match="keyframe min interval"):
        ProviderConfig.from_env(
            {
                "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
                "REALTIME_KEYFRAME_MIN_INTERVAL_SECONDS": "2",
                "REALTIME_KEYFRAME_MAX_INTERVAL_SECONDS": "1",
            }
        )


def test_visual_memory_similarity_thresholds_are_configurable() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
            "REALTIME_VISUAL_MEMORY_CANDIDATE_SIMILARITY": "0.22",
            "REALTIME_VISUAL_MEMORY_CONFIRMED_SIMILARITY": "0.34",
        }
    )

    assert config.visual_memory_candidate_similarity == 0.22
    assert config.visual_memory_confirmed_similarity == 0.34


def test_visual_memory_similarity_threshold_order_is_validated() -> None:
    with pytest.raises(ValueError, match="candidate < confirmed"):
        ProviderConfig.from_env(
            {
                "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
                "REALTIME_VISUAL_MEMORY_CANDIDATE_SIMILARITY": "0.4",
                "REALTIME_VISUAL_MEMORY_CONFIRMED_SIMILARITY": "0.3",
            }
        )
