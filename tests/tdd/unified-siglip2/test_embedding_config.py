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
