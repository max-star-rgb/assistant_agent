import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from types import SimpleNamespace

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.media.embedding.coordinator import SessionEmbeddingCoordinator
from assistant_agent.media.embedding.provider import create_multimodal_embedding_provider
from assistant_agent.media.video.detection.vision_embedding_provider import (
    MockVisionEmbeddingProvider,
    VisionEmbeddingResult,
    create_vision_embedding_provider,
)
from assistant_agent.media.video.detection.local_siglip2_provider import (
    LocalSiglip2Error,
    LocalSiglip2VisionConfig,
    LocalSiglip2VisionProvider,
    OnnxSiglip2ImageBackend,
    l2_normalize,
    load_siglip2_manifest,
)
from assistant_agent.media.video.detection.semantic_detector import (
    SemanticChangeDetector,
)
from assistant_agent.media.video.keyframe.collector import AdaptiveKeyframeCollector
from assistant_agent.media.video.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.media.video.realtime_video_observer import RealtimeVideoObserver
from assistant_agent.media.video.keyframe.selector import (
    KeyframeSelectorConfig,
    SemanticKeyframeSelector,
)
from assistant_agent.media.video.types import VideoFrame
from assistant_agent.media.video.types import KeyframeChangeMetrics
from assistant_agent.tools.registry import ToolRegistry


_REVISION_SENTINEL = "a" * 40


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
        model_revision=_REVISION_SENTINEL,
        embedding_space_id=(
            f"siglip2-base-p16-224@{_REVISION_SENTINEL}:image-projection-v1"
        ),
        dimension=2,
        normalized=True,
    )

    assert result.embedding_space_id == (
        f"siglip2-base-p16-224@{_REVISION_SENTINEL}:image-projection-v1"
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
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "test-key-sentinel",
            "MULTIMODAL_AGENT_VISION_EMBEDDING_PROVIDER": "local_siglip2",
            "SIGLIP2_VISION_MODEL_DIR": "/model-sentinel",
            "SIGLIP2_CUDA_DEVICE_ID": "2",
        }
    )

    provider = create_vision_embedding_provider(config)

    assert provider.provider == "local_siglip2"
    assert provider.model == "google/siglip2-base-patch16-224"


def test_provider_factory_cannot_bypass_mock_mode_with_direct_config() -> None:
    config = ProviderConfig(
        provider_mode="mock",
        vision_embedding_provider="local_siglip2",
        siglip2_vision_model_dir="/model-sentinel",
    )

    provider = create_vision_embedding_provider(config)

    assert isinstance(provider, MockVisionEmbeddingProvider)


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
    import onnx
    from onnx import TensorProto, helper

    model_dir.mkdir()
    input_info = helper.make_tensor_value_info(
        "pixel_values",
        TensorProto.FLOAT16,
        [1, 3, 224, 224],
    )
    output_info = helper.make_tensor_value_info(
        "image_embeds",
        TensorProto.FLOAT16,
        [1, 3],
    )
    graph = helper.make_graph(
        [helper.make_node("Identity", ["pixel_values"], ["image_embeds"])],
        "siglip2-test-graph",
        [input_info],
        [output_info],
    )
    model_bytes = helper.make_model(graph).SerializeToString()
    (model_dir / "vision_model.onnx").write_bytes(model_bytes)
    (model_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model_id": "google/siglip2-base-patch16-224",
                "model_revision": _REVISION_SENTINEL,
                "model_file": "vision_model.onnx",
                "model_sha256": checksum or hashlib.sha256(model_bytes).hexdigest(),
                "dimension": 3,
                "embedding_space_id": (
                    f"siglip2-base-p16-224@{_REVISION_SENTINEL}:image-projection-v1"
                ),
                "projection": "visual_projection",
                "input_dtype": "float16",
                "external_data": {},
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
    assert result.model_revision == _REVISION_SENTINEL
    assert result.embedding_space_id == (
        f"siglip2-base-p16-224@{_REVISION_SENTINEL}:image-projection-v1"
    )
    assert result.dimension == 3
    assert result.normalized is True
    assert result.errors == []


def test_local_siglip2_provider_validates_immutable_assets_only_once(
    tmp_path,
    monkeypatch,
) -> None:
    model_dir = tmp_path / "model"
    _write_siglip2_assets(model_dir)
    provider = LocalSiglip2VisionProvider(
        LocalSiglip2VisionConfig(model_dir=model_dir),
        backend=_SentinelBackend(),
        preprocessor=_SentinelPreprocessor(),
    )
    provider_module = sys.modules[LocalSiglip2VisionProvider.__module__]
    real_sha256_file = provider_module._sha256_file
    checked_paths: list[Path] = []

    def _counted_sha256_file(path: Path) -> str:
        checked_paths.append(path)
        return real_sha256_file(path)

    monkeypatch.setattr(provider_module, "_sha256_file", _counted_sha256_file)
    frame = _write_frame(tmp_path / "frame.jpg")

    first = provider.embed_image(frame)
    second = provider.embed_image(frame)

    assert first.errors == []
    assert second.errors == []
    assert checked_paths == [model_dir.resolve() / "vision_model.onnx"]


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


@pytest.mark.parametrize("values", [[float("nan"), 1.0], [float("inf"), 1.0]])
def test_local_siglip2_rejects_non_finite_embeddings(values: list[float]) -> None:
    with pytest.raises(LocalSiglip2Error, match="unusable"):
        l2_normalize(values)


def test_runtime_manifest_requires_cross_modal_projection_contract(tmp_path) -> None:
    model_dir = tmp_path / "model"
    _write_siglip2_assets(model_dir)

    manifest = load_siglip2_manifest(model_dir)

    assert manifest.projection == "visual_projection"
    assert manifest.input_dtype == "float16"


@pytest.mark.parametrize(
    ("model_revision", "embedding_space_id"),
    [
        ("main", "siglip2-base-p16-224@main:image-projection-v1"),
        (
            _REVISION_SENTINEL,
            f"siglip2-base-p16-224@{'b' * 40}:image-projection-v1",
        ),
    ],
)
def test_runtime_manifest_requires_immutable_matching_embedding_identity(
    tmp_path,
    model_revision: str,
    embedding_space_id: str,
) -> None:
    model_dir = tmp_path / "model"
    _write_siglip2_assets(model_dir)
    manifest_path = model_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["model_revision"] = model_revision
    manifest["embedding_space_id"] = embedding_space_id
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(LocalSiglip2Error, match="manifest is invalid"):
        load_siglip2_manifest(model_dir)


def test_runtime_manifest_validates_onnx_external_data_checksums(tmp_path) -> None:
    model_dir = tmp_path / "model"
    _write_siglip2_assets(model_dir)
    external_path = model_dir / "vision_model.onnx.data"
    external_path.write_bytes(b"external-weight-sentinel")
    manifest_path = model_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["external_data"] = {
        external_path.name: "0" * 64,
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    provider_module = sys.modules[LocalSiglip2VisionProvider.__module__]
    original_locations = provider_module.onnx_external_data_locations

    def _locations(_model_path: Path) -> set[str]:
        return {external_path.name}

    provider_module.onnx_external_data_locations = _locations
    try:
        with pytest.raises(Exception, match="checksum"):
            load_siglip2_manifest(model_dir)
    finally:
        provider_module.onnx_external_data_locations = original_locations


def test_runtime_manifest_rejects_unlisted_onnx_external_data(
    tmp_path,
    monkeypatch,
) -> None:
    model_dir = tmp_path / "model"
    _write_siglip2_assets(model_dir)
    provider_module = sys.modules[LocalSiglip2VisionProvider.__module__]
    monkeypatch.setattr(
        provider_module,
        "onnx_external_data_locations",
        lambda _model_path: {"vision_model.onnx.data"},
        raising=False,
    )

    with pytest.raises(LocalSiglip2Error, match="external data manifest"):
        load_siglip2_manifest(model_dir)


def test_onnx_backend_rejects_silent_cpu_provider_fallback(
    tmp_path,
    monkeypatch,
) -> None:
    model_dir = tmp_path / "model"
    _write_siglip2_assets(model_dir)
    manifest = load_siglip2_manifest(model_dir)

    class _CpuOnlySession:
        def get_providers(self) -> list[str]:
            return ["CPUExecutionProvider"]

    class _SessionOptions:
        def add_session_config_entry(self, _key: str, _value: str) -> None:
            return None

    fake_ort = SimpleNamespace(
        get_available_providers=lambda: [
            "CUDAExecutionProvider",
            "CPUExecutionProvider",
        ],
        preload_dlls=lambda **_kwargs: None,
        SessionOptions=_SessionOptions,
        InferenceSession=lambda *_args, **_kwargs: _CpuOnlySession(),
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)

    with pytest.raises(LocalSiglip2Error, match="CUDA"):
        OnnxSiglip2ImageBackend(manifest, cuda_device_id=0)


def test_onnx_backend_disables_operator_level_cpu_fallback(
    tmp_path,
    monkeypatch,
) -> None:
    model_dir = tmp_path / "model"
    _write_siglip2_assets(model_dir)
    manifest = load_siglip2_manifest(model_dir)
    captured: dict[str, object] = {}

    class _SessionOptions:
        def add_session_config_entry(self, key: str, value: str) -> None:
            captured[key] = value

    class _CudaSession:
        def get_providers(self) -> list[str]:
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]

    def _session(*_args, **kwargs):
        captured["sess_options"] = kwargs.get("sess_options")
        return _CudaSession()

    fake_ort = SimpleNamespace(
        get_available_providers=lambda: ["CUDAExecutionProvider"],
        preload_dlls=lambda **_kwargs: None,
        SessionOptions=_SessionOptions,
        InferenceSession=_session,
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)

    OnnxSiglip2ImageBackend(manifest, cuda_device_id=0)

    assert captured["session.disable_cpu_ep_fallback"] == "1"
    assert isinstance(captured["sess_options"], _SessionOptions)


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


def test_two_fps_semantic_threshold_trigger_bypasses_slower_sampler() -> None:
    collector = AdaptiveKeyframeCollector(
        keyframe_config=KeyframeSelectorConfig(min_interval_seconds=0.0),
        semantic_probe_fps=2.0,
    )
    initial = _static_frame("frame-0", 0.0)
    initial.metadata["embedding"] = [1.0, 0.0]
    boundary = _static_frame("frame-050", 0.5)
    boundary.metadata["embedding"] = [0.81, 0.586429876]

    collector.collect(initial)
    due = collector.collect(boundary)

    assert due.processing.metrics.semantic_change_score == pytest.approx(
        0.19,
        abs=1e-6,
    )
    assert due.processing.sampled is True
    assert due.processing.keyframe_selected is True
    assert due.processing.decision_reason == "semantic_change"


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


def test_realtime_observer_uses_configured_siglip2_ssim_policy() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "deepseek",
            "DEEPSEEK_API_KEY": "test-key-sentinel",
            "MULTIMODAL_AGENT_VISION_EMBEDDING_PROVIDER": "local_siglip2",
            "SIGLIP2_VISION_MODEL_DIR": "/model-sentinel",
        }
    )

    observer = RealtimeVideoObserver(
        user_id="user-sentinel",
        session_id="session-sentinel",
        registry=ToolRegistry(),
        memory_store=RealtimeVideoMemoryStore(),
        embedding_coordinator=SessionEmbeddingCoordinator(
            "session-sentinel",
            create_multimodal_embedding_provider(config),
        ),
        provider_config=config,
    )

    policy = observer.collector.selector.config
    assert policy.max_interval_seconds == 10.0
    assert policy.structural_threshold == 0.35
    assert policy.semantic_threshold == 0.18
    assert policy.combined_threshold == 0.25
    assert policy.structural_weight == 0.4
    assert policy.semantic_weight == 0.6
    assert observer.collector.semantic_probe_fps == 2.0
    assert observer.collector.semantic_detector.embedding_model.provider == (
        "local_siglip2"
    )


def _load_siglip2_export_script() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "export_siglip2_vision_onnx.py"
    )
    spec = importlib.util.spec_from_file_location("siglip2_export_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_export_manifest_preserves_cross_modal_projection_identity() -> None:
    module = _load_siglip2_export_script()

    manifest = module.build_manifest(
        model_id="google/siglip2-base-patch16-224",
        model_revision=_REVISION_SENTINEL,
        model_file="vision_model.onnx",
        model_sha256="a" * 64,
        dimension=768,
        image_size=224,
        mean=(0.5, 0.5, 0.5),
        std=(0.5, 0.5, 0.5),
        external_data={"vision_model.onnx.data": "b" * 64},
    )

    assert manifest["input_name"] == "pixel_values"
    assert manifest["output_name"] == "image_embeds"
    assert manifest["projection"] == "visual_projection"
    assert manifest["external_data"] == {
        "vision_model.onnx.data": "b" * 64,
    }
    assert manifest["embedding_space_id"] == (
        f"siglip2-base-p16-224@{_REVISION_SENTINEL}:image-projection-v1"
    )


def test_export_reads_external_data_locations_from_onnx_graph(monkeypatch) -> None:
    module = _load_siglip2_export_script()
    external_entry = SimpleNamespace(
        key="location",
        value="vision_model.onnx.data",
    )
    model = SimpleNamespace(
        graph=SimpleNamespace(
            initializer=[SimpleNamespace(external_data=[external_entry])]
        )
    )
    monkeypatch.setitem(
        sys.modules,
        "onnx",
        SimpleNamespace(load_model=lambda *_args, **_kwargs: model),
    )

    locations = module.onnx_external_data_locations(Path("vision_model.onnx"))

    assert locations == {"vision_model.onnx.data"}


def test_export_script_rejects_an_unapproved_model_id() -> None:
    module = _load_siglip2_export_script()

    with pytest.raises(ValueError, match="google/siglip2-base-patch16-224"):
        module.validate_export_request("other/model", "revision-sentinel")


def test_export_script_has_an_offline_help_boundary() -> None:
    script = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "export_siglip2_vision_onnx.py"
    )

    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "--revision" in completed.stdout
    assert "--output-dir" in completed.stdout


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
