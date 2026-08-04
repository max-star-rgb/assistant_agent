"""Validated local SigLIP2 image/text embeddings backed by joint ONNX assets."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from math import isfinite, sqrt
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any, Protocol

from assistant_agent.media.embedding.models import (
    EmbeddingEvent,
    EmbeddingFailureEvent,
    EmbeddingOutcome,
    EmbeddingReadiness,
    ImageObservation,
    TextObservation,
)


SIGLIP2_MODEL_ID = "google/siglip2-base-patch16-224"
SIGLIP2_MANIFEST_NAME = "manifest.json"
SIGLIP2_JOINT_SCHEMA_VERSION = 2


class LocalSiglip2Error(RuntimeError):
    """Prompt-safe local model failure with a stable machine code."""

    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


@dataclass(frozen=True)
class LocalSiglip2EmbeddingConfig:
    model_dir: Path | None
    cuda_device_id: int = 0


@dataclass(frozen=True)
class Siglip2ImageAsset:
    model_path: Path
    model_sha256: str
    external_data: tuple[tuple[str, str], ...]
    projection: str
    input_name: str
    output_name: str
    input_dtype: str
    image_size: int
    mean: tuple[float, float, float]
    std: tuple[float, float, float]


@dataclass(frozen=True)
class Siglip2TextAsset:
    model_path: Path
    model_sha256: str
    external_data: tuple[tuple[str, str], ...]
    projection: str
    input_names: tuple[str, ...]
    output_name: str
    input_dtype: str
    tokenizer_path: Path
    tokenizer_sha256: str
    max_length: int
    padding: str
    truncation: bool


@dataclass(frozen=True)
class Siglip2EmbeddingManifest:
    model_dir: Path
    model_id: str
    model_revision: str
    dimension: int
    embedding_space_id: str
    supported_modalities: tuple[str, ...]
    image: Siglip2ImageAsset | None
    text: Siglip2TextAsset | None
    schema_version: int


class Siglip2EmbeddingBackend(Protocol):
    def run_image(self, pixel_values: object) -> list[float]: ...

    def run_text(self, token_inputs: dict[str, object]) -> list[float]: ...


class Siglip2ImagePreprocessor(Protocol):
    def to_pixel_values(
        self, observation: ImageObservation, manifest: Siglip2ImageAsset
    ) -> object: ...


class Siglip2TextPreprocessor(Protocol):
    def to_token_inputs(
        self, observation: TextObservation, manifest: Siglip2TextAsset
    ) -> dict[str, object]: ...


class PillowSiglip2ImagePreprocessor:
    def to_pixel_values(
        self, observation: ImageObservation, manifest: Siglip2ImageAsset
    ) -> object:
        try:
            import numpy as np
            from PIL import Image
        except ImportError as exc:
            raise LocalSiglip2Error(
                "local_model_unavailable", "local SigLIP2 image dependencies are unavailable"
            ) from exc
        try:
            with Image.open(observation.image_ref) as source:
                image = source.convert("RGB").resize(
                    (manifest.image_size, manifest.image_size), Image.Resampling.BICUBIC
                )
                values = np.asarray(image, dtype=np.float16) / np.float16(255.0)
        except Exception as exc:
            raise LocalSiglip2Error(
                "local_model_unsupported_input", "local SigLIP2 could not read the image"
            ) from exc
        mean = np.asarray(manifest.mean, dtype=np.float16)
        std = np.asarray(manifest.std, dtype=np.float16)
        return np.transpose((values - mean) / std, (2, 0, 1))[None, ...]


class TokenizersSiglip2TextPreprocessor:
    """Use the manifest-pinned tokenizer without network or model downloads."""

    def __init__(self) -> None:
        self._tokenizers: dict[tuple[str, int], Any] = {}
        self._lock = Lock()

    def to_token_inputs(
        self, observation: TextObservation, manifest: Siglip2TextAsset
    ) -> dict[str, object]:
        try:
            import numpy as np
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise LocalSiglip2Error(
                "local_model_unavailable", "local SigLIP2 text dependencies are unavailable"
            ) from exc
        key = (str(manifest.tokenizer_path), manifest.max_length)
        with self._lock:
            tokenizer = self._tokenizers.get(key)
            if tokenizer is None:
                try:
                    tokenizer = Tokenizer.from_file(str(manifest.tokenizer_path))
                    tokenizer.enable_truncation(max_length=manifest.max_length)
                    tokenizer.enable_padding(length=manifest.max_length)
                except Exception as exc:
                    raise LocalSiglip2Error(
                        "local_model_unavailable", "local SigLIP2 tokenizer is unavailable"
                    ) from exc
                self._tokenizers[key] = tokenizer
        try:
            encoded = tokenizer.encode(observation.text)
            return {
                "input_ids": np.asarray([encoded.ids], dtype=np.int64),
                "attention_mask": np.asarray([encoded.attention_mask], dtype=np.int64),
            }
        except Exception as exc:
            raise LocalSiglip2Error(
                "local_model_unsupported_input", "local SigLIP2 could not tokenize text"
            ) from exc


class OnnxSiglip2EmbeddingBackend:
    """CUDA-only ONNX sessions; CPU execution fallback is explicitly disabled."""

    def __init__(self, manifest: Siglip2EmbeddingManifest, *, cuda_device_id: int) -> None:
        self.cpu_fallback_disabled = True
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise LocalSiglip2Error(
                "local_model_unavailable", "local SigLIP2 ONNX Runtime is unavailable"
            ) from exc
        if "CUDAExecutionProvider" not in ort.get_available_providers():
            raise LocalSiglip2Error(
                "local_model_unavailable", "local SigLIP2 CUDA execution provider is unavailable"
            )

        def session(path: Path):
            options = ort.SessionOptions()
            options.add_session_config_entry("session.disable_cpu_ep_fallback", "1")
            created = ort.InferenceSession(
                str(path),
                sess_options=options,
                providers=[("CUDAExecutionProvider", {"device_id": cuda_device_id})],
            )
            if not created.get_providers() or created.get_providers()[0] != "CUDAExecutionProvider":
                raise LocalSiglip2Error(
                    "local_model_unavailable", "local SigLIP2 CUDA session initialization failed"
                )
            return created

        try:
            preload_dlls = getattr(ort, "preload_dlls", None)
            if preload_dlls is not None:
                preload_dlls(directory="")
            self._image_asset = manifest.image
            self._text_asset = manifest.text
            self._image_session = session(manifest.image.model_path) if manifest.image else None
            self._text_session = session(manifest.text.model_path) if manifest.text else None
        except LocalSiglip2Error:
            raise
        except Exception as exc:
            raise LocalSiglip2Error(
                "local_model_unavailable", "local SigLIP2 CUDA session initialization failed"
            ) from exc

    def run_image(self, pixel_values: object) -> list[float]:
        if self._image_session is None or self._image_asset is None:
            raise LocalSiglip2Error("modality_unavailable", "image embedding is unavailable")
        try:
            output = self._image_session.run(
                [self._image_asset.output_name], {self._image_asset.input_name: pixel_values}
            )[0][0]
            return [float(value) for value in output]
        except Exception as exc:
            raise LocalSiglip2Error(
                "local_model_inference_failed", "local SigLIP2 image inference failed"
            ) from exc

    def run_text(self, token_inputs: dict[str, object]) -> list[float]:
        if self._text_session is None or self._text_asset is None:
            raise LocalSiglip2Error("modality_unavailable", "text embedding is unavailable")
        inputs = {name: token_inputs[name] for name in self._text_asset.input_names}
        try:
            output = self._text_session.run([self._text_asset.output_name], inputs)[0][0]
            return [float(value) for value in output]
        except Exception as exc:
            raise LocalSiglip2Error(
                "local_model_inference_failed", "local SigLIP2 text inference failed"
            ) from exc


_BACKEND_CACHE: dict[tuple[str, int], OnnxSiglip2EmbeddingBackend] = {}
_BACKEND_LOCK = Lock()


class LocalSiglip2EmbeddingProvider:
    provider = "local_siglip2"
    model_id = SIGLIP2_MODEL_ID

    def __init__(
        self,
        config: LocalSiglip2EmbeddingConfig,
        *,
        backend: Siglip2EmbeddingBackend | None = None,
        image_preprocessor: Siglip2ImagePreprocessor | None = None,
        text_preprocessor: Siglip2TextPreprocessor | None = None,
    ) -> None:
        self.config = config
        self._backend = backend
        self._image_preprocessor = image_preprocessor or PillowSiglip2ImagePreprocessor()
        self._text_preprocessor = text_preprocessor or TokenizersSiglip2TextPreprocessor()
        self._manifest: Siglip2EmbeddingManifest | None = None
        self._manifest_lock = Lock()

    def _validated_manifest(self) -> Siglip2EmbeddingManifest:
        if self._manifest is not None:
            return self._manifest
        with self._manifest_lock:
            if self._manifest is None:
                self._manifest = load_siglip2_embedding_manifest(self.config.model_dir)
            return self._manifest

    def _inference_backend(self, manifest: Siglip2EmbeddingManifest) -> Siglip2EmbeddingBackend:
        if self._backend is not None:
            return self._backend
        key = (str(manifest.model_dir), self.config.cuda_device_id)
        with _BACKEND_LOCK:
            backend = _BACKEND_CACHE.get(key)
            if backend is None:
                backend = OnnxSiglip2EmbeddingBackend(
                    manifest, cuda_device_id=self.config.cuda_device_id
                )
                _BACKEND_CACHE[key] = backend
            self._backend = backend
            return backend

    def embed_image(self, observation: ImageObservation) -> EmbeddingOutcome:
        started = perf_counter()
        manifest: Siglip2EmbeddingManifest | None = None
        try:
            manifest = self._validated_manifest()
            if manifest.image is None:
                raise LocalSiglip2Error("modality_unavailable", "image embedding is unavailable")
            values = self._image_preprocessor.to_pixel_values(observation, manifest.image)
            vector = _validated_unit_vector(
                self._inference_backend(manifest).run_image(values), manifest.dimension
            )
            return _success_event("image", observation, manifest, vector, started)
        except LocalSiglip2Error as exc:
            return _failure_event("image", observation, manifest, exc, started)
        except Exception:
            return _failure_event(
                "image",
                observation,
                manifest,
                LocalSiglip2Error("local_model_inference_failed", "local SigLIP2 image inference failed"),
                started,
            )

    def embed_text(self, observation: TextObservation) -> EmbeddingOutcome:
        started = perf_counter()
        manifest: Siglip2EmbeddingManifest | None = None
        try:
            manifest = self._validated_manifest()
            if manifest.text is None:
                raise LocalSiglip2Error("modality_unavailable", "text embedding is unavailable")
            values = self._text_preprocessor.to_token_inputs(observation, manifest.text)
            vector = _validated_unit_vector(
                self._inference_backend(manifest).run_text(values), manifest.dimension
            )
            return _success_event("text", observation, manifest, vector, started)
        except LocalSiglip2Error as exc:
            return _failure_event("text", observation, manifest, exc, started)
        except Exception:
            return _failure_event(
                "text",
                observation,
                manifest,
                LocalSiglip2Error("local_model_inference_failed", "local SigLIP2 text inference failed"),
                started,
            )

    def readiness(self) -> EmbeddingReadiness:
        try:
            manifest = self._validated_manifest()
        except LocalSiglip2Error as exc:
            return EmbeddingReadiness(
                provider=self.provider, model_id=self.model_id, issues=[exc.code]
            )
        return EmbeddingReadiness(
            provider=self.provider,
            model_id=manifest.model_id,
            model_revision=manifest.model_revision,
            embedding_space_id=manifest.embedding_space_id,
            dimension=manifest.dimension,
            image_ready=manifest.image is not None,
            text_ready=manifest.text is not None,
            issues=[] if manifest.text is not None else ["text_modality_unavailable"],
        )


def load_siglip2_embedding_manifest(model_dir: Path | None) -> Siglip2EmbeddingManifest:
    if model_dir is None:
        raise LocalSiglip2Error("provider_unconfigured", "local SigLIP2 model directory is not configured")
    root = model_dir.expanduser().resolve()
    try:
        raw = json.loads((root / SIGLIP2_MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalSiglip2Error("local_model_unavailable", "local SigLIP2 model manifest is unavailable") from exc
    schema = raw.get("schema_version")
    if schema == 1:
        return _load_legacy_image_manifest(root, raw)
    if schema != SIGLIP2_JOINT_SCHEMA_VERSION:
        raise LocalSiglip2Error("local_model_integrity_failed", "local SigLIP2 manifest schema is invalid")
    try:
        model_id = str(raw["model_id"])
        revision = str(raw["model_revision"])
        dimension = int(raw["dimension"])
        space = str(raw["embedding_space_id"])
        modalities = tuple(str(value) for value in raw["supported_modalities"])
        image_raw = raw["image"]
        text_raw = raw["text"]
        image_revision = str(image_raw.get("model_revision", revision))
        text_revision = str(text_raw.get("model_revision", revision))
    except (KeyError, TypeError, ValueError) as exc:
        raise LocalSiglip2Error("local_model_integrity_failed", "local SigLIP2 model manifest is invalid") from exc
    if image_revision != revision or text_revision != revision:
        raise LocalSiglip2Error(
            "manifest_model_revision_mismatch", "manifest_model_revision_mismatch"
        )
    expected_space = f"siglip2-base-p16-224@{revision}:joint-projection-v1"
    if (
        model_id != SIGLIP2_MODEL_ID
        or not _lower_hex(revision, 40)
        or dimension <= 0
        or space != expected_space
        or modalities != ("image", "text")
    ):
        raise LocalSiglip2Error("local_model_integrity_failed", "local SigLIP2 model manifest is invalid")
    image = _load_image_asset(root, image_raw)
    text = _load_text_asset(root, text_raw)
    return Siglip2EmbeddingManifest(
        model_dir=root,
        model_id=model_id,
        model_revision=revision,
        dimension=dimension,
        embedding_space_id=space,
        supported_modalities=modalities,
        image=image,
        text=text,
        schema_version=2,
    )


def _load_legacy_image_manifest(root: Path, raw: dict[str, Any]) -> Siglip2EmbeddingManifest:
    try:
        revision = str(raw["model_revision"])
        dimension = int(raw["dimension"])
        image_raw = {
            "model_file": raw["model_file"],
            "model_sha256": raw["model_sha256"],
            "external_data": raw["external_data"],
            "projection": raw["projection"],
            "input_name": raw.get("input_name", "pixel_values"),
            "output_name": raw.get("output_name", "image_embeds"),
            "input_dtype": raw["input_dtype"],
            "preprocessing": raw["preprocessing"],
        }
        space = str(raw["embedding_space_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise LocalSiglip2Error("local_model_integrity_failed", "local SigLIP2 model manifest is invalid") from exc
    if (
        raw.get("model_id") != SIGLIP2_MODEL_ID
        or not _lower_hex(revision, 40)
        or dimension <= 0
        or space != f"siglip2-base-p16-224@{revision}:image-projection-v1"
    ):
        raise LocalSiglip2Error("local_model_integrity_failed", "local SigLIP2 model manifest is invalid")
    return Siglip2EmbeddingManifest(
        model_dir=root,
        model_id=SIGLIP2_MODEL_ID,
        model_revision=revision,
        dimension=dimension,
        embedding_space_id=space,
        supported_modalities=("image",),
        image=_load_image_asset(root, image_raw),
        text=None,
        schema_version=1,
    )


def _load_image_asset(root: Path, raw: dict[str, Any]) -> Siglip2ImageAsset:
    try:
        preprocessing = raw["preprocessing"]
        asset = Siglip2ImageAsset(
            model_path=_safe_asset_path(root, raw["model_file"]),
            model_sha256=str(raw["model_sha256"]),
            external_data=_external_entries(raw["external_data"]),
            projection=str(raw["projection"]),
            input_name=str(raw.get("input_name", "pixel_values")),
            output_name=str(raw.get("output_name", "image_embeds")),
            input_dtype=str(raw["input_dtype"]),
            image_size=int(preprocessing["size"]),
            mean=_three_floats(preprocessing["mean"]),
            std=_three_floats(preprocessing["std"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LocalSiglip2Error("local_model_integrity_failed", "local SigLIP2 image manifest is invalid") from exc
    if (
        asset.projection != "visual_projection"
        or asset.input_dtype != "float16"
        or asset.image_size <= 0
        or any(not isfinite(value) for value in (*asset.mean, *asset.std))
        or any(value == 0 for value in asset.std)
    ):
        raise LocalSiglip2Error("local_model_integrity_failed", "local SigLIP2 image manifest is invalid")
    _validate_asset(root, asset.model_path, asset.model_sha256, asset.external_data)
    return asset


def _load_text_asset(root: Path, raw: dict[str, Any]) -> Siglip2TextAsset:
    try:
        preprocessing = raw["preprocessing"]
        asset = Siglip2TextAsset(
            model_path=_safe_asset_path(root, raw["model_file"]),
            model_sha256=str(raw["model_sha256"]),
            external_data=_external_entries(raw["external_data"]),
            projection=str(raw["projection"]),
            input_names=tuple(str(value) for value in raw["input_names"]),
            output_name=str(raw.get("output_name", "text_embeds")),
            input_dtype=str(raw["input_dtype"]),
            tokenizer_path=_safe_asset_path(root, raw["tokenizer_file"]),
            tokenizer_sha256=str(raw["tokenizer_sha256"]),
            max_length=int(preprocessing["max_length"]),
            padding=str(preprocessing["padding"]),
            truncation=bool(preprocessing["truncation"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LocalSiglip2Error("local_model_integrity_failed", "local SigLIP2 text manifest is invalid") from exc
    if (
        asset.projection != "text_projection"
        or asset.input_names != ("input_ids", "attention_mask")
        or asset.input_dtype != "int64"
        or asset.max_length <= 0
        or asset.padding != "max_length"
        or not asset.truncation
    ):
        raise LocalSiglip2Error("local_model_integrity_failed", "local SigLIP2 text manifest is invalid")
    _validate_asset(root, asset.model_path, asset.model_sha256, asset.external_data)
    _validate_checksum(asset.tokenizer_path, asset.tokenizer_sha256)
    return asset


def _validate_asset(
    root: Path, path: Path, checksum: str, external_data: tuple[tuple[str, str], ...]
) -> None:
    _validate_checksum(path, checksum)
    if onnx_external_data_locations(path) != {name for name, _ in external_data}:
        raise LocalSiglip2Error(
            "local_model_integrity_failed", "local SigLIP2 external data manifest does not match the ONNX graph"
        )
    for name, expected in external_data:
        external_path = _safe_asset_path(root, name)
        _validate_checksum(external_path, expected)


def _validate_checksum(path: Path, expected: str) -> None:
    if not _lower_hex(expected, 64):
        raise LocalSiglip2Error("local_model_integrity_failed", "local SigLIP2 checksum is invalid")
    try:
        actual = _sha256_file(path)
    except OSError as exc:
        raise LocalSiglip2Error("local_model_unavailable", "local SigLIP2 model asset is unavailable") from exc
    if actual != expected:
        raise LocalSiglip2Error("local_model_integrity_failed", "local SigLIP2 model checksum mismatch")


def _safe_asset_path(root: Path, value: object) -> Path:
    path = (root / str(value)).resolve()
    if root not in path.parents:
        raise ValueError("asset path escapes model directory")
    return path


def _external_entries(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        raise ValueError("external_data must be an object")
    entries = tuple(sorted((str(name), str(checksum)) for name, checksum in value.items()))
    for name, checksum in entries:
        if Path(name).is_absolute() or not name or not _lower_hex(checksum, 64):
            raise ValueError("invalid external_data entry")
    return entries


def onnx_external_data_locations(model_path: Path) -> set[str]:
    try:
        import onnx
        model = onnx.load_model(str(model_path), load_external_data=False)
    except ImportError as exc:
        raise LocalSiglip2Error("local_model_unavailable", "local SigLIP2 ONNX validation is unavailable") from exc
    except Exception as exc:
        raise LocalSiglip2Error("local_model_integrity_failed", "local SigLIP2 ONNX graph is invalid") from exc
    locations: set[str] = set()
    for tensor in model.graph.initializer:
        entry = {item.key: item.value for item in tensor.external_data}.get("location")
        if entry:
            locations.add(str(entry))
    return locations


def _validated_unit_vector(values: list[float], dimension: int) -> list[float]:
    if len(values) != dimension or any(not isfinite(float(value)) for value in values):
        raise LocalSiglip2Error("local_model_inference_failed", "local SigLIP2 output is unusable")
    norm = sqrt(sum(float(value) ** 2 for value in values))
    if not isfinite(norm) or norm <= 0:
        raise LocalSiglip2Error("local_model_inference_failed", "local SigLIP2 output is unusable")
    return [float(value) / norm for value in values]


def _success_event(modality: str, observation: Any, manifest: Siglip2EmbeddingManifest, vector: list[float], started: float) -> EmbeddingEvent:
    digest = hashlib.sha256(
        f"{observation.session_id}:{observation.observation_id}:{modality}".encode()
    ).hexdigest()[:24]
    return EmbeddingEvent(
        event_id=f"embedding-{digest}",
        modality=modality,
        vector=vector,
        embedding_space_id=manifest.embedding_space_id,
        model_id=manifest.model_id,
        model_revision=manifest.model_revision,
        dimension=manifest.dimension,
        normalized=True,
        session_id=observation.session_id,
        source_observation_id=observation.observation_id,
        video_id=getattr(observation, "video_id", None),
        frame_sequence=getattr(observation, "frame_sequence", None),
        captured_at_ms=getattr(observation, "captured_at_ms", None),
        text_source=getattr(observation, "source", None) if modality == "text" else None,
        occurred_at_ms=getattr(observation, "occurred_at_ms", None),
        latency_ms=max(0, int((perf_counter() - started) * 1000)),
    )


def _failure_event(modality: str, observation: Any, manifest: Siglip2EmbeddingManifest | None, error: LocalSiglip2Error, started: float) -> EmbeddingFailureEvent:
    return EmbeddingFailureEvent(
        modality=modality,
        session_id=observation.session_id,
        source_observation_id=observation.observation_id,
        code=error.code,
        safe_message=error.safe_message,
        recoverable=error.code in {"local_model_unavailable", "local_model_inference_failed"},
        latency_ms=max(0, int((perf_counter() - started) * 1000)),
        model_id=manifest.model_id if manifest else SIGLIP2_MODEL_ID,
        model_revision=manifest.model_revision if manifest else None,
        embedding_space_id=manifest.embedding_space_id if manifest else None,
    )


def _three_floats(value: object) -> tuple[float, float, float]:
    if not isinstance(value, list | tuple) or len(value) != 3:
        raise ValueError("expected three values")
    result = tuple(float(item) for item in value)
    return result[0], result[1], result[2]


def _lower_hex(value: str, length: int) -> bool:
    return len(value) == length and all(character in "0123456789abcdef" for character in value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
