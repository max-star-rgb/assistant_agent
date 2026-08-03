"""Local image-only SigLIP2 embeddings backed by explicit ONNX assets."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from math import sqrt
from pathlib import Path
from threading import Lock
from typing import Any, Protocol

from assistant_agent.media.video.detection.vision_embedding_provider import (
    VisionEmbeddingResult,
)
from assistant_agent.media.video.types import VideoFrame


SIGLIP2_MODEL_ID = "google/siglip2-base-patch16-224"
SIGLIP2_MANIFEST_NAME = "manifest.json"
SIGLIP2_MANIFEST_SCHEMA_VERSION = 1


class LocalSiglip2Error(RuntimeError):
    """Prompt-safe local model failure."""

    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


@dataclass(frozen=True)
class LocalSiglip2VisionConfig:
    """Runtime location and CUDA device for an exported vision tower."""

    model_dir: Path | None
    cuda_device_id: int = 0


@dataclass(frozen=True)
class Siglip2VisionManifest:
    """Validated identity and preprocessing contract for one ONNX asset."""

    model_dir: Path
    model_path: Path
    model_revision: str
    model_sha256: str
    dimension: int
    embedding_space_id: str
    image_size: int
    mean: tuple[float, float, float]
    std: tuple[float, float, float]
    input_name: str = "pixel_values"
    output_name: str = "image_embeds"


class Siglip2ImageBackend(Protocol):
    """Narrow inference boundary used by the provider."""

    def run_image(self, pixel_values: object) -> list[float]:
        """Return one unnormalized pooled image embedding."""


class Siglip2ImagePreprocessor(Protocol):
    """Image preprocessing boundary kept independent from ONNX execution."""

    def to_pixel_values(
        self,
        frame: VideoFrame,
        manifest: Siglip2VisionManifest,
    ) -> object:
        """Return one NCHW float tensor compatible with the model asset."""


class PillowSiglip2ImagePreprocessor:
    """Apply manifest-pinned SigLIP2 image preprocessing lazily."""

    def to_pixel_values(
        self,
        frame: VideoFrame,
        manifest: Siglip2VisionManifest,
    ) -> object:
        if not frame.uri:
            raise LocalSiglip2Error(
                "local_model_unsupported_input",
                "local SigLIP2 requires a readable image frame",
            )
        try:
            import numpy as np
            from PIL import Image
        except ImportError as exc:
            raise LocalSiglip2Error(
                "local_model_unavailable",
                "local SigLIP2 runtime dependencies are unavailable",
            ) from exc
        try:
            with Image.open(frame.uri) as source:
                image = source.convert("RGB").resize(
                    (manifest.image_size, manifest.image_size),
                    Image.Resampling.BICUBIC,
                )
                values = np.asarray(image, dtype=np.float32) / np.float32(255.0)
        except Exception as exc:
            raise LocalSiglip2Error(
                "local_model_unsupported_input",
                "local SigLIP2 could not read the image frame",
            ) from exc
        mean = np.asarray(manifest.mean, dtype=np.float32)
        std = np.asarray(manifest.std, dtype=np.float32)
        values = (values - mean) / std
        return np.transpose(values, (2, 0, 1))[None, ...]


class OnnxSiglip2ImageBackend:
    """CUDA-only ONNX Runtime session for the exported vision tower."""

    def __init__(self, manifest: Siglip2VisionManifest, *, cuda_device_id: int) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise LocalSiglip2Error(
                "local_model_unavailable",
                "local SigLIP2 ONNX Runtime dependency is unavailable",
            ) from exc
        if "CUDAExecutionProvider" not in ort.get_available_providers():
            raise LocalSiglip2Error(
                "local_model_unavailable",
                "local SigLIP2 CUDA execution provider is unavailable",
            )
        try:
            self._session = ort.InferenceSession(
                str(manifest.model_path),
                providers=[
                    (
                        "CUDAExecutionProvider",
                        {"device_id": cuda_device_id},
                    )
                ],
            )
        except Exception as exc:
            raise LocalSiglip2Error(
                "local_model_unavailable",
                "local SigLIP2 CUDA session initialization failed",
            ) from exc
        self._input_name = manifest.input_name
        self._output_name = manifest.output_name

    def run_image(self, pixel_values: object) -> list[float]:
        try:
            outputs = self._session.run(
                [self._output_name],
                {self._input_name: pixel_values},
            )
            vector = outputs[0][0]
            return [float(value) for value in vector]
        except Exception as exc:
            raise LocalSiglip2Error(
                "local_model_inference_failed",
                "local SigLIP2 image inference failed",
            ) from exc


_BACKEND_CACHE: dict[tuple[str, int], OnnxSiglip2ImageBackend] = {}
_BACKEND_CACHE_LOCK = Lock()


class LocalSiglip2VisionProvider:
    """Image-only SigLIP2 provider with validated assets and lazy CUDA loading."""

    provider = "local_siglip2"
    model = SIGLIP2_MODEL_ID

    def __init__(
        self,
        config: LocalSiglip2VisionConfig,
        *,
        backend: Siglip2ImageBackend | None = None,
        preprocessor: Siglip2ImagePreprocessor | None = None,
    ) -> None:
        self.config = config
        self._backend = backend
        self._preprocessor = preprocessor or PillowSiglip2ImagePreprocessor()

    def embed_image(self, frame: VideoFrame) -> VisionEmbeddingResult:
        manifest: Siglip2VisionManifest | None = None
        try:
            manifest = load_siglip2_manifest(self.config.model_dir)
            pixel_values = self._preprocessor.to_pixel_values(frame, manifest)
            backend = self._backend or shared_onnx_backend(
                manifest,
                cuda_device_id=self.config.cuda_device_id,
            )
            embedding = l2_normalize(backend.run_image(pixel_values))
            if len(embedding) != manifest.dimension:
                raise LocalSiglip2Error(
                    "local_model_inference_failed",
                    "local SigLIP2 output dimension does not match its manifest",
                )
            return VisionEmbeddingResult(
                embedding=embedding,
                provider=self.provider,
                model=self.model,
                model_family="siglip2",
                model_revision=manifest.model_revision,
                embedding_space_id=manifest.embedding_space_id,
                dimension=len(embedding),
                normalized=True,
            )
        except LocalSiglip2Error as exc:
            return failed_local_embedding(exc, manifest=manifest)
        except Exception:
            return failed_local_embedding(
                LocalSiglip2Error(
                    "local_model_inference_failed",
                    "local SigLIP2 image inference failed",
                ),
                manifest=manifest,
            )


def load_siglip2_manifest(model_dir: Path | None) -> Siglip2VisionManifest:
    """Load and integrity-check a local image-tower manifest."""

    if model_dir is None:
        raise LocalSiglip2Error(
            "provider_unconfigured",
            "local SigLIP2 vision model directory is not configured",
        )
    root = model_dir.expanduser().resolve()
    manifest_path = root / SIGLIP2_MANIFEST_NAME
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalSiglip2Error(
            "local_model_unavailable",
            "local SigLIP2 model manifest is unavailable",
        ) from exc
    try:
        schema_version = int(raw["schema_version"])
        model_id = str(raw["model_id"])
        model_revision = str(raw["model_revision"])
        model_sha256 = str(raw["model_sha256"])
        dimension = int(raw["dimension"])
        embedding_space_id = str(raw["embedding_space_id"])
        preprocessing = raw["preprocessing"]
        image_size = int(preprocessing["size"])
        mean = _three_floats(preprocessing["mean"])
        std = _three_floats(preprocessing["std"])
        model_path = (root / str(raw["model_file"])).resolve()
        input_name = str(raw.get("input_name") or "pixel_values")
        output_name = str(raw.get("output_name") or "image_embeds")
    except (KeyError, TypeError, ValueError) as exc:
        raise LocalSiglip2Error(
            "local_model_integrity_failed",
            "local SigLIP2 model manifest is invalid",
        ) from exc
    if (
        schema_version != SIGLIP2_MANIFEST_SCHEMA_VERSION
        or model_id != SIGLIP2_MODEL_ID
        or not model_revision
        or dimension <= 0
        or image_size <= 0
        or not embedding_space_id
        or not input_name
        or not output_name
        or any(value == 0.0 for value in std)
        or root not in model_path.parents
    ):
        raise LocalSiglip2Error(
            "local_model_integrity_failed",
            "local SigLIP2 model manifest is invalid",
        )
    try:
        actual_sha256 = _sha256_file(model_path)
    except OSError as exc:
        raise LocalSiglip2Error(
            "local_model_unavailable",
            "local SigLIP2 vision model asset is unavailable",
        ) from exc
    if actual_sha256 != model_sha256:
        raise LocalSiglip2Error(
            "local_model_integrity_failed",
            "local SigLIP2 vision model checksum does not match its manifest",
        )
    return Siglip2VisionManifest(
        model_dir=root,
        model_path=model_path,
        model_revision=model_revision,
        model_sha256=model_sha256,
        dimension=dimension,
        embedding_space_id=embedding_space_id,
        image_size=image_size,
        mean=mean,
        std=std,
        input_name=input_name,
        output_name=output_name,
    )


def shared_onnx_backend(
    manifest: Siglip2VisionManifest,
    *,
    cuda_device_id: int,
) -> OnnxSiglip2ImageBackend:
    """Return one process-shared CUDA session per immutable asset and device."""

    key = (str(manifest.model_path), cuda_device_id)
    with _BACKEND_CACHE_LOCK:
        backend = _BACKEND_CACHE.get(key)
        if backend is None:
            backend = OnnxSiglip2ImageBackend(
                manifest,
                cuda_device_id=cuda_device_id,
            )
            _BACKEND_CACHE[key] = backend
        return backend


def l2_normalize(values: list[float]) -> list[float]:
    """Return a unit vector or fail instead of publishing a fake embedding."""

    norm = sqrt(sum(float(value) ** 2 for value in values))
    if norm <= 0.0:
        raise LocalSiglip2Error(
            "local_model_inference_failed",
            "local SigLIP2 returned an unusable image embedding",
        )
    return [float(value) / norm for value in values]


def failed_local_embedding(
    error: LocalSiglip2Error,
    *,
    manifest: Siglip2VisionManifest | None,
) -> VisionEmbeddingResult:
    """Build a safe structured failure without paths or embedding values."""

    return VisionEmbeddingResult(
        provider="local_siglip2",
        model=SIGLIP2_MODEL_ID,
        model_family="siglip2",
        model_revision=manifest.model_revision if manifest else None,
        embedding_space_id=manifest.embedding_space_id if manifest else None,
        dimension=manifest.dimension if manifest else None,
        errors=[{"code": error.code, "message": error.safe_message}],
    )


def _three_floats(value: Any) -> tuple[float, float, float]:
    if not isinstance(value, list | tuple) or len(value) != 3:
        raise ValueError("expected three numeric values")
    converted = tuple(float(item) for item in value)
    return converted[0], converted[1], converted[2]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
