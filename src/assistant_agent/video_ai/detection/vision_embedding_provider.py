"""Vision embedding providers for realtime video semantic change detection."""

from __future__ import annotations

import base64
import json
import struct
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from assistant_agent.services.provider_errors import ProviderAdapterError, build_provider_error, sanitize_error_message
from assistant_agent.services.real_vision_adapter import image_to_data_url
from assistant_agent.video_ai.detection.frame_difference import grayscale_fingerprint
from assistant_agent.video_ai.types import VideoFrame

if TYPE_CHECKING:
    from assistant_agent.config import ProviderConfig


DEFAULT_DASHSCOPE_VISION_EMBEDDING_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
DEFAULT_DASHSCOPE_VISION_EMBEDDING_ENDPOINT = (
    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
    "multimodal-embedding/multimodal-embedding"
)
DEFAULT_DASHSCOPE_VISION_EMBEDDING_MODEL = "tongyi-embedding-vision-flash-2026-03-06"
DEFAULT_DASHSCOPE_VISION_EMBEDDING_DIMENSION = 768


@dataclass(frozen=True)
class VisionEmbeddingResult:
    """Structured result from an optional vision embedding provider."""

    embedding: list[float] = field(default_factory=list)
    provider: str = "mock"
    model: str = "mock-vision-embedding"
    request_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)


class VisionEmbeddingProvider(Protocol):
    """Provider contract used by the semantic detector."""

    def embed(self, frame: VideoFrame) -> VisionEmbeddingResult:
        """Return a structured image embedding result for one frame."""


class MockVisionEmbeddingProvider:
    """Deterministic local embedding provider for config-selected mock paths."""

    provider = "mock"
    model = "mock-vision-embedding"

    def embed(self, frame: VideoFrame) -> VisionEmbeddingResult:
        value = frame.metadata.get("embedding")
        if isinstance(value, list | tuple) and all(isinstance(item, int | float) for item in value):
            embedding = [float(item) for item in value]
        else:
            embedding = []
        return VisionEmbeddingResult(embedding=embedding, provider=self.provider, model=self.model)


@dataclass(frozen=True)
class DashScopeVisionEmbeddingConfig:
    """Configuration for DashScope multimodal image embeddings."""

    api_key: str | None
    base_url: str = DEFAULT_DASHSCOPE_VISION_EMBEDDING_ENDPOINT
    model: str = DEFAULT_DASHSCOPE_VISION_EMBEDDING_MODEL
    dimension: int = DEFAULT_DASHSCOPE_VISION_EMBEDDING_DIMENSION
    timeout_seconds: float = 30.0


class DashScopeVisionEmbeddingProvider:
    """HTTP adapter for DashScope multimodal image embeddings."""

    provider = "dashscope"

    def __init__(self, config: DashScopeVisionEmbeddingConfig) -> None:
        self.config = config

    def embed(self, frame: VideoFrame) -> VisionEmbeddingResult:
        if not self.config.api_key:
            return _failed_result(
                provider=self.provider,
                model=self.config.model,
                code="provider_unconfigured",
                message=(
                    "dashscope vision embedding provider requires QWEN_API_KEY or DASHSCOPE_API_KEY "
                    "(legacy QWEN_VISION_API_KEY is also accepted)."
                ),
            )

        try:
            image = image_reference_for_dashscope(frame)
            payload = build_dashscope_vision_embedding_payload(
                image=image,
                model=self.config.model,
                dimension=self.config.dimension,
            )
            request = urllib.request.Request(
                dashscope_multimodal_embedding_url(self.config.base_url),
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
                status = getattr(response, "status", 200)
        except ProviderAdapterError as exc:
            return _failed_result(provider=self.provider, model=self.config.model, code=exc.code, message=exc.message)
        except TimeoutError as exc:
            return _failed_result(
                provider=self.provider,
                model=self.config.model,
                code="provider_timeout",
                message=str(exc),
            )
        except urllib.error.HTTPError as exc:
            return _failed_result_from_http_error(provider=self.provider, model=self.config.model, exc=exc)
        except urllib.error.URLError as exc:
            return _failed_result(
                provider=self.provider,
                model=self.config.model,
                code="provider_unavailable",
                message=str(exc.reason),
            )
        except json.JSONDecodeError:
            return _failed_result(
                provider=self.provider,
                model=self.config.model,
                code="provider_bad_response",
                message="response JSON decode failed",
            )
        except Exception as exc:
            return _failed_result(
                provider=self.provider,
                model=self.config.model,
                code="provider_execution_failed",
                message=str(exc),
            )

        provider_error = _provider_error_from_response(data, status=status)
        if provider_error is not None:
            return _failed_result(
                provider=self.provider,
                model=self.config.model,
                code=provider_error.code,
                message=provider_error.message,
            )

        try:
            embedding = parse_dashscope_vision_embedding_response(data)
        except (KeyError, TypeError, ValueError) as exc:
            return _failed_result(
                provider=self.provider,
                model=self.config.model,
                code="provider_bad_response",
                message=str(exc),
            )
        if not embedding:
            return _failed_result(
                provider=self.provider,
                model=self.config.model,
                code="provider_empty_response",
                message="DashScope response did not include an image embedding.",
            )
        return VisionEmbeddingResult(
            embedding=embedding,
            provider=self.provider,
            model=self.config.model,
            request_id=_request_id(data),
            usage=_usage(data),
        )


def create_vision_embedding_provider(config: ProviderConfig | None = None) -> VisionEmbeddingProvider:
    """Create the configured vision embedding provider without enabling real calls by default."""

    if config is None:
        from assistant_agent.config import ProviderConfig

        config = ProviderConfig.from_env()
    if config.vision_embedding_provider == "dashscope":
        return DashScopeVisionEmbeddingProvider(
            DashScopeVisionEmbeddingConfig(
                api_key=config.vision_embedding_api_key,
                base_url=config.vision_embedding_base_url,
                model=config.vision_embedding_model,
                dimension=config.vision_embedding_dimension,
                timeout_seconds=config.vision_embedding_timeout_seconds,
            )
        )
    return MockVisionEmbeddingProvider()


def build_dashscope_vision_embedding_payload(*, image: str, model: str, dimension: int) -> dict[str, Any]:
    """Build the native DashScope multimodal embedding HTTP payload."""

    return {
        "model": model,
        "input": {"contents": [{"image": image}]},
        "parameters": {"dimension": dimension},
    }


def dashscope_multimodal_embedding_url(base_url: str) -> str:
    """Return the DashScope multimodal embedding endpoint for a base URL or full endpoint."""

    normalized = (base_url or DEFAULT_DASHSCOPE_VISION_EMBEDDING_ENDPOINT).rstrip("/")
    suffix = "/services/embeddings/multimodal-embedding/multimodal-embedding"
    if normalized.endswith(suffix):
        return normalized
    return f"{normalized}{suffix}"


def parse_dashscope_vision_embedding_response(data: dict[str, Any]) -> list[float]:
    """Extract the first image embedding from a DashScope multimodal embedding response."""

    output = data.get("output") or {}
    embeddings = output.get("embeddings") or []
    if not isinstance(embeddings, list):
        raise ValueError("output.embeddings is not a list")
    fallback: list[float] | None = None
    for item in embeddings:
        if not isinstance(item, dict):
            continue
        embedding = _numeric_vector(item.get("embedding"))
        if embedding is None:
            continue
        if fallback is None:
            fallback = embedding
        if item.get("type") in {"image", "vl", "fused", "fusion"}:
            return embedding
    return fallback or []


def image_reference_for_dashscope(frame: VideoFrame) -> str:
    """Return an HTTP URL or Data URI image reference for DashScope."""

    if frame.uri:
        if frame.uri.startswith(("http://", "https://", "data:")):
            return frame.uri
        try:
            return image_to_data_url(frame.uri)
        except Exception:
            if frame.pixels is None and frame.metadata.get("pixel_signature") is None:
                raise ProviderAdapterError("provider_unsupported_input", "frame URI is not a readable image")
    data_url = frame_to_bmp_data_url(frame)
    if data_url:
        return data_url
    raise ProviderAdapterError("provider_unsupported_input", "frame does not contain an embeddable image reference")


def frame_to_bmp_data_url(frame: VideoFrame) -> str:
    """Encode frame pixels as a small BMP Data URI without external image dependencies."""

    width, height = 160, 90
    values = grayscale_fingerprint(frame, (width, height))
    if not values:
        return ""
    row_padding = (4 - (width * 3) % 4) % 4
    row_size = width * 3 + row_padding
    pixel_data_size = row_size * height
    file_size = 54 + pixel_data_size
    header = b"".join(
        [
            b"BM",
            struct.pack("<I", file_size),
            b"\x00\x00\x00\x00",
            struct.pack("<I", 54),
            struct.pack("<I", 40),
            struct.pack("<i", width),
            struct.pack("<i", height),
            struct.pack("<H", 1),
            struct.pack("<H", 24),
            struct.pack("<I", 0),
            struct.pack("<I", pixel_data_size),
            struct.pack("<i", 2835),
            struct.pack("<i", 2835),
            struct.pack("<I", 0),
            struct.pack("<I", 0),
        ]
    )
    rows: list[bytes] = []
    for row_index in range(height - 1, -1, -1):
        row_values = values[row_index * width : (row_index + 1) * width]
        row = bytearray()
        for value in row_values:
            channel = int(max(0.0, min(1.0, value)) * 255)
            row.extend((channel, channel, channel))
        row.extend(b"\x00" * row_padding)
        rows.append(bytes(row))
    encoded = base64.b64encode(header + b"".join(rows)).decode("ascii")
    return f"data:image/bmp;base64,{encoded}"


def _numeric_vector(value: Any) -> list[float] | None:
    if not isinstance(value, list):
        return None
    vector: list[float] = []
    for item in value:
        if not isinstance(item, int | float):
            return None
        vector.append(float(item))
    return vector


def _provider_error_from_response(data: dict[str, Any], *, status: int) -> ProviderAdapterError | None:
    code = data.get("code")
    message = data.get("message")
    if status < 400 and not code and not message:
        return None
    return ProviderAdapterError(
        _http_status_to_error_code(status),
        _format_provider_error(status=status, code=code, message=message, request_id=_request_id(data)),
    )


def _failed_result_from_http_error(
    *,
    provider: str,
    model: str,
    exc: urllib.error.HTTPError,
) -> VisionEmbeddingResult:
    body = _read_http_error_body(exc)
    return _failed_result(
        provider=provider,
        model=model,
        code=_http_status_to_error_code(exc.code),
        message=_format_provider_error(
            status=exc.code,
            code=body.get("code"),
            message=body.get("message") or f"HTTP {exc.code}",
            request_id=_request_id(body) or exc.headers.get("X-Request-Id"),
        ),
    )


def _failed_result(*, provider: str, model: str, code: str, message: object) -> VisionEmbeddingResult:
    error = build_provider_error(
        code,
        sanitize_error_message(message),
        provider=provider,
        capability="vision_embedding",
    )
    return VisionEmbeddingResult(
        provider=provider,
        model=model,
        errors=[
            {
                "code": error.code,
                "message": error.message,
                "recoverable": error.recoverable,
                "provider": error.provider,
                "capability": error.capability,
            }
        ],
    )


def _read_http_error_body(exc: urllib.error.HTTPError) -> dict[str, Any]:
    try:
        raw = exc.read().decode("utf-8")
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _format_provider_error(
    *,
    status: int,
    code: object | None,
    message: object | None,
    request_id: object | None,
) -> str:
    parts = [f"status={status}"]
    if code:
        parts.append(f"code={sanitize_error_message(code)}")
    if message:
        parts.append(f"message={sanitize_error_message(message)}")
    if request_id:
        parts.append(f"request_id={sanitize_error_message(request_id)}")
    return ", ".join(parts)


def _request_id(data: dict[str, Any]) -> str | None:
    value = data.get("request_id") or data.get("requestId") or (data.get("output") or {}).get("request_id")
    return value if isinstance(value, str) else None


def _usage(data: dict[str, Any]) -> dict[str, Any]:
    usage = data.get("usage")
    return usage if isinstance(usage, dict) else {}


def _http_status_to_error_code(status: int) -> str:
    if status == 401:
        return "provider_auth_failed"
    if status == 403:
        return "provider_permission_denied"
    if status == 429:
        return "provider_rate_limited"
    if status >= 500:
        return "provider_bad_gateway"
    if status >= 400:
        return "provider_bad_response"
    return "provider_execution_failed"
