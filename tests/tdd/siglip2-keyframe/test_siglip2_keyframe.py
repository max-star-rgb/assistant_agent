import hashlib
import json
from pathlib import Path

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.media.video.detection.vision_embedding_provider import (
    MockVisionEmbeddingProvider,
    VisionEmbeddingResult,
    create_vision_embedding_provider,
)
from assistant_agent.media.video.detection.local_siglip2_provider import (
    LocalSiglip2VisionConfig,
    LocalSiglip2VisionProvider,
)
from assistant_agent.media.video.detection.semantic_detector import (
    SemanticChangeDetector,
)
from assistant_agent.media.video.keyframe.collector import AdaptiveKeyframeCollector
from assistant_agent.media.video.keyframe.selector import (
    KeyframeSelectorConfig,
    SemanticKeyframeSelector,
)
from assistant_agent.media.video.types import VideoFrame
from assistant_agent.media.video.types import KeyframeChangeMetrics


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


def test_provider_factory_builds_explicit_local_siglip2_image_provider() -> None:
    config = ProviderConfig(
        vision_embedding_provider="local_siglip2",
        siglip2_vision_model_dir="/model-sentinel",
        siglip2_cuda_device_id=2,
    )

    provider = create_vision_embedding_provider(config)

    assert provider.provider == "local_siglip2"
    assert provider.model == "google/siglip2-base-patch16-224"


class _SentinelPreprocessor:
    def to_pixel_values(self, frame: VideoFrame, manifest: object) -> str:
        assert frame.uri is not None
        assert Path(frame.uri).read_bytes() == b"rgb-frame-sentinel"
        assert getattr(manifest, "dimension") == 3
        return "pixel-values-sentinel"


class _SentinelBackend:
    def run_image(self, pixel_values: object) -> list[float]:
        assert pixel_values == "pixel-values-sentinel"
        return [3.0, 4.0, 0.0]


def _write_siglip2_assets(model_dir: Path, *, checksum: str | None = None) -> None:
    model_dir.mkdir()
    model_bytes = b"onnx-model-sentinel"
    (model_dir / "vision_model.onnx").write_bytes(model_bytes)
    (model_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model_id": "google/siglip2-base-patch16-224",
                "model_revision": "revision-sentinel",
                "model_file": "vision_model.onnx",
                "model_sha256": checksum or hashlib.sha256(model_bytes).hexdigest(),
                "dimension": 3,
                "embedding_space_id": (
                    "siglip2-base-p16-224@revision-sentinel:vision-pool-v1"
                ),
                "preprocessing": {
                    "size": 224,
                    "mean": [0.5, 0.5, 0.5],
                    "std": [0.5, 0.5, 0.5],
                },
            }
        ),
        encoding="utf-8",
    )


def _write_frame(path: Path) -> VideoFrame:
    path.write_bytes(b"rgb-frame-sentinel")
    return VideoFrame(
        frame_id="frame-sentinel",
        timestamp_seconds=1.0,
        uri=str(path),
    )


def test_local_siglip2_provider_returns_normalized_image_embedding(tmp_path) -> None:
    model_dir = tmp_path / "model"
    _write_siglip2_assets(model_dir)
    provider = LocalSiglip2VisionProvider(
        LocalSiglip2VisionConfig(model_dir=model_dir),
        backend=_SentinelBackend(),
        preprocessor=_SentinelPreprocessor(),
    )

    result = provider.embed_image(_write_frame(tmp_path / "frame.jpg"))

    assert result.embedding == pytest.approx([0.6, 0.8, 0.0])
    assert result.provider == "local_siglip2"
    assert result.model_family == "siglip2"
    assert result.model_revision == "revision-sentinel"
    assert result.embedding_space_id == (
        "siglip2-base-p16-224@revision-sentinel:vision-pool-v1"
    )
    assert result.dimension == 3
    assert result.normalized is True
    assert result.errors == []


def test_local_siglip2_provider_fails_closed_when_assets_are_missing(tmp_path) -> None:
    provider = LocalSiglip2VisionProvider(
        LocalSiglip2VisionConfig(model_dir=tmp_path / "missing")
    )

    result = provider.embed_image(_write_frame(tmp_path / "frame.jpg"))

    assert result.embedding == []
    assert result.errors[0]["code"] == "local_model_unavailable"


def test_local_siglip2_provider_rejects_model_checksum_mismatch(tmp_path) -> None:
    model_dir = tmp_path / "model"
    _write_siglip2_assets(model_dir, checksum="0" * 64)
    provider = LocalSiglip2VisionProvider(
        LocalSiglip2VisionConfig(model_dir=model_dir),
        backend=_SentinelBackend(),
        preprocessor=_SentinelPreprocessor(),
    )

    result = provider.embed_image(_write_frame(tmp_path / "frame.jpg"))

    assert result.embedding == []
    assert result.errors[0]["code"] == "local_model_integrity_failed"


def _frame_at(timestamp_seconds: float) -> VideoFrame:
    return VideoFrame(
        frame_id=f"frame-{timestamp_seconds}",
        timestamp_seconds=timestamp_seconds,
    )


@pytest.mark.parametrize(
    ("metrics", "reason"),
    [
        (
            KeyframeChangeMetrics(structural_change_score=0.36),
            "structural_change",
        ),
        (
            KeyframeChangeMetrics(semantic_change_score=0.19),
            "semantic_change",
        ),
        (
            KeyframeChangeMetrics(
                structural_change_score=0.20,
                semantic_change_score=0.30,
            ),
            "combined_change",
        ),
    ],
)
def test_selector_allows_structural_semantic_and_combined_triggers(
    metrics: KeyframeChangeMetrics,
    reason: str,
) -> None:
    selector = SemanticKeyframeSelector(
        KeyframeSelectorConfig(min_interval_seconds=0.0)
    )

    decision = selector.select(
        _frame_at(1.0),
        selector.with_score(metrics),
        last_keyframe_at=0.0,
    )

    assert decision.selected is True
    assert decision.reason == reason


def test_selector_forces_static_keyframe_at_ten_seconds_not_before() -> None:
    selector = SemanticKeyframeSelector(
        KeyframeSelectorConfig(min_interval_seconds=0.0)
    )

    before = selector.select(
        _frame_at(9.999),
        KeyframeChangeMetrics(),
        last_keyframe_at=0.0,
    )
    due = selector.select(
        _frame_at(10.0),
        KeyframeChangeMetrics(),
        last_keyframe_at=0.0,
    )

    assert before.selected is False
    assert due.selected is True
    assert due.reason == "max_interval"


def test_pixel_difference_is_not_part_of_final_keyframe_score() -> None:
    selector = SemanticKeyframeSelector()

    scored = selector.with_score(
        KeyframeChangeMetrics(
            pixel_change_score=1.0,
            structural_change_score=0.2,
            semantic_change_score=0.1,
        )
    )

    assert scored.keyframe_score == pytest.approx(0.14)


class _SequenceImageEmbeddingModel:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed_image(self, frame: VideoFrame) -> VisionEmbeddingResult:
        self.calls.append(frame.frame_id)
        embedding = [1.0, 0.0] if len(self.calls) == 1 else [0.0, 1.0]
        return VisionEmbeddingResult(
            embedding=embedding,
            provider="local_siglip2",
            model="google/siglip2-base-patch16-224",
            embedding_space_id="space-sentinel",
            normalized=True,
        )


def _static_frame(frame_id: str, timestamp_seconds: float) -> VideoFrame:
    return VideoFrame(
        frame_id=frame_id,
        timestamp_seconds=timestamp_seconds,
        pixels=[[128, 128], [128, 128]],
    )


def test_collector_probes_semantics_at_two_fps_without_reembedding_early() -> None:
    model = _SequenceImageEmbeddingModel()
    detector = SemanticChangeDetector(model, requires_visual_gate=True)
    collector = AdaptiveKeyframeCollector(
        keyframe_config=KeyframeSelectorConfig(min_interval_seconds=0.0),
        semantic_detector=detector,
        semantic_probe_fps=2.0,
    )

    initial = collector.collect(_static_frame("frame-0", 0.0))
    early = collector.collect(_static_frame("frame-025", 0.25))
    due = collector.collect(_static_frame("frame-050", 0.50))

    assert initial.processing.keyframe_selected is True
    assert early.processing.keyframe_selected is False
    assert due.processing.keyframe_selected is True
    assert due.processing.decision_reason == "semantic_change"
    assert model.calls == ["frame-0", "frame-050"]


class _FailedImageEmbeddingModel:
    def embed_image(self, frame: VideoFrame) -> VisionEmbeddingResult:
        return VisionEmbeddingResult(
            provider="local_siglip2",
            model="google/siglip2-base-patch16-224",
            errors=[
                {
                    "code": "local_model_unavailable",
                    "message": "local model unavailable",
                }
            ],
        )


def test_failed_siglip2_embedding_does_not_create_fake_semantic_change() -> None:
    detector = SemanticChangeDetector(
        _FailedImageEmbeddingModel(),
        requires_visual_gate=True,
    )

    result = detector.compare(
        _static_frame("current", 1.0),
        _static_frame("reference", 0.0),
        semantic_candidate=True,
    )

    assert result.semantic_change_score == 0.0
    assert result.errors[0]["code"] == "local_model_unavailable"


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
