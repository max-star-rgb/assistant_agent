import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.media.embedding.local_siglip2 import (
    LocalSiglip2EmbeddingConfig,
    LocalSiglip2EmbeddingProvider,
)
from assistant_agent.media.embedding.models import (
    EmbeddingEvent,
    EmbeddingFailureEvent,
    ImageObservation,
    TextObservation,
)
from assistant_agent.media.embedding.provider import (
    DashScopeImageOnlyEmbeddingProvider,
    create_multimodal_embedding_provider,
)

from test_siglip2_joint_manifest import _write_joint_manifest


class _Backend:
    def run_image(self, _values: object) -> list[float]:
        return [3.0, 4.0, 0.0]

    def run_text(self, _values: dict[str, object]) -> list[float]:
        return [0.0, 4.0, 3.0]


class _ImagePreprocessor:
    def to_pixel_values(self, _observation, _manifest) -> object:
        return "pixels"


class _TextPreprocessor:
    def to_token_inputs(self, _observation, _manifest) -> dict[str, object]:
        return {"input_ids": [[1]], "attention_mask": [[1]]}


def _provider(tmp_path, monkeypatch) -> LocalSiglip2EmbeddingProvider:
    root = _write_joint_manifest(tmp_path / "model")
    monkeypatch.setattr(
        "assistant_agent.media.embedding.local_siglip2.onnx_external_data_locations",
        lambda _path: set(),
    )
    return LocalSiglip2EmbeddingProvider(
        LocalSiglip2EmbeddingConfig(model_dir=root),
        backend=_Backend(),
        image_preprocessor=_ImagePreprocessor(),
        text_preprocessor=_TextPreprocessor(),
    )


def test_joint_provider_normalizes_image_and_text_in_one_space(tmp_path, monkeypatch) -> None:
    provider = _provider(tmp_path, monkeypatch)

    image = provider.embed_image(
        ImageObservation(session_id="s", observation_id="i", image_ref="frame.jpg")
    )
    text = provider.embed_text(
        TextObservation(session_id="s", observation_id="t", text="红色杯子", source="asr")
    )

    assert isinstance(image, EmbeddingEvent)
    assert isinstance(text, EmbeddingEvent)
    assert image.vector == pytest.approx([0.6, 0.8, 0.0])
    assert text.vector == pytest.approx([0.0, 0.8, 0.6])
    assert image.embedding_space_id == text.embedding_space_id
    assert image.normalized is text.normalized is True
    assert provider.readiness().image_ready is True
    assert provider.readiness().text_ready is True


def test_provider_rejects_wrong_output_dimension(tmp_path, monkeypatch) -> None:
    provider = _provider(tmp_path, monkeypatch)
    provider._backend = type(
        "WrongBackend",
        (),
        {"run_image": lambda *_: [1.0], "run_text": lambda *_: [1.0]},
    )()

    result = provider.embed_text(
        TextObservation(session_id="s", observation_id="t", text="杯子", source="user_text")
    )

    assert isinstance(result, EmbeddingFailureEvent)
    assert result.code == "local_model_inference_failed"


def test_provider_failure_does_not_expose_input_or_vector(tmp_path) -> None:
    provider = LocalSiglip2EmbeddingProvider(
        LocalSiglip2EmbeddingConfig(model_dir=tmp_path / "missing")
    )

    result = provider.embed_text(
        TextObservation(session_id="s", observation_id="t", text="secret", source="user_text")
    )

    assert isinstance(result, EmbeddingFailureEvent)
    assert "secret" not in result.model_dump_json()
    assert "vector" not in result.model_dump()


def test_factory_builds_joint_local_provider_from_canonical_config() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "test-key",
            "MULTIMODAL_AGENT_EMBEDDING_PROVIDER": "local_siglip2",
            "SIGLIP2_MODEL_DIR": "/models/siglip2",
        }
    )

    provider = create_multimodal_embedding_provider(config)

    assert isinstance(provider, LocalSiglip2EmbeddingProvider)
    assert provider.config.model_dir.as_posix() == "/models/siglip2"


def test_dashscope_adapter_never_claims_text_readiness() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "test-key",
            "MULTIMODAL_AGENT_EMBEDDING_PROVIDER": "dashscope",
            "QWEN_API_KEY": "embedding-key",
        }
    )

    provider = create_multimodal_embedding_provider(config)

    assert isinstance(provider, DashScopeImageOnlyEmbeddingProvider)
    assert provider.readiness().image_ready is True
    assert provider.readiness().text_ready is False
