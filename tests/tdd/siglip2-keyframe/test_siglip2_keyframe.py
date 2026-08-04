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
from assistant_agent.media.video.types import VideoFrame


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
    assert config.semantic_input_fps == 5.0
    assert config.keyframe_semantic_threshold == 0.18


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
        ("keyframe_semantic_threshold", -0.01),
        ("siglip2_cuda_device_id", -1),
    ],
)
def test_keyframe_config_rejects_invalid_local_model_values(
    field: str,
    value: float,
) -> None:
    with pytest.raises(ValueError, match="keyframe|siglip2"):
        ProviderConfig(**{field: value})
