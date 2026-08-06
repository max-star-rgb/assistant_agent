"""Local artifact storage for generated media returned by providers."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from assistant_agent.tools.plugins.builtin.image_generation.models import ImageGenerationResult
from assistant_agent.providers.provider_errors import ProviderAdapterError
from assistant_agent.runtime.requests import AgentResponse


REPO_ROOT = Path(__file__).resolve().parents[3]
GENERATED_ARTIFACT_DIR = REPO_ROOT / ".local" / "generated"
GENERATED_ARTIFACT_PUBLIC_PREFIX = "/artifacts/generated"
MAX_ARTIFACT_BYTES = 25 * 1024 * 1024
MAX_DELIVERED_IMAGE_COUNT = 4


@dataclass(frozen=True)
class StoredArtifact:
    """A generated artifact stored by this backend."""

    path: Path
    download_url: str
    source_url: str


@dataclass(frozen=True)
class GeneratedArtifactPayload:
    """Bounded image payload safe for the rendering WebSocket contract."""

    image_id: str
    media_type: str
    base64_data: str


def materialize_image_generation_result(
    result: ImageGenerationResult,
    *,
    artifact_dir: Path = GENERATED_ARTIFACT_DIR,
    public_prefix: str = GENERATED_ARTIFACT_PUBLIC_PREFIX,
    timeout_seconds: float = 120.0,
) -> ImageGenerationResult:
    """Download provider-hosted images and expose backend-owned download URLs."""

    image_urls = result.image_urls or ([result.image_url] if result.image_url else [])
    provider_urls = [url for url in image_urls if _is_remote_http_url(url)]
    if not provider_urls:
        return result

    artifacts = [
        store_remote_artifact(
            url,
            artifact_dir=artifact_dir,
            public_prefix=public_prefix,
            filename_seed=f"{result.request_id or result.task_id}-{index}",
            timeout_seconds=timeout_seconds,
        )
        for index, url in enumerate(provider_urls)
    ]
    download_urls = [artifact.download_url for artifact in artifacts]
    return result.model_copy(
        update={
            "provider_image_urls": image_urls,
            "download_url": download_urls[0] if download_urls else None,
            "download_urls": download_urls,
            "image_url": download_urls[0] if download_urls else result.image_url,
            "image_urls": download_urls or result.image_urls,
            "output_ref": download_urls[0] if download_urls else result.output_ref,
        }
    )


def generated_artifact_data_url(
    output_ref: str,
    *,
    artifact_dir: Path | None = None,
    max_bytes: int = MAX_ARTIFACT_BYTES,
) -> str | None:
    """Read one backend-owned generated image as a bounded data URL."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    parsed = urlparse(output_ref)
    prefix = GENERATED_ARTIFACT_PUBLIC_PREFIX.rstrip("/") + "/"
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(prefix)
    ):
        return None
    filename = parsed.path.removeprefix(prefix)
    if not filename or Path(filename).name != filename:
        return None

    root = (artifact_dir or GENERATED_ARTIFACT_DIR).resolve()
    path = (root / filename).resolve()
    if path.parent != root:
        return None
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            return None
        payload = path.read_bytes()
    except OSError:
        return None
    if not payload or len(payload) > max_bytes:
        return None
    media_type = _image_media_type(payload)
    if media_type is None:
        return None
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def generated_artifact_payload(
    output_ref: str,
    *,
    artifact_dir: Path | None = None,
    max_bytes: int = MAX_ARTIFACT_BYTES,
) -> GeneratedArtifactPayload | None:
    """Read one backend-owned image for the IMAGE rendering detail."""

    data_url = generated_artifact_data_url(
        output_ref,
        artifact_dir=artifact_dir,
        max_bytes=max_bytes,
    )
    if data_url is None:
        return None
    parsed = urlparse(output_ref)
    image_id = Path(parsed.path).name
    header, base64_data = data_url.split(",", 1)
    media_type = header.removeprefix("data:").removesuffix(";base64")
    return GeneratedArtifactPayload(
        image_id=image_id,
        media_type=media_type,
        base64_data=base64_data,
    )


def generated_artifact_public_url(
    output_ref: str,
    *,
    base_url: str | None,
) -> str | None:
    """Project one managed artifact reference onto a trusted HTTP origin."""

    if not base_url:
        return None
    parsed_ref = urlparse(output_ref)
    prefix = GENERATED_ARTIFACT_PUBLIC_PREFIX.rstrip("/") + "/"
    filename = parsed_ref.path.removeprefix(prefix)
    if (
        parsed_ref.scheme
        or parsed_ref.netloc
        or parsed_ref.query
        or parsed_ref.fragment
        or not parsed_ref.path.startswith(prefix)
        or not filename
        or Path(filename).name != filename
    ):
        return None

    normalized_base = base_url.strip()
    parsed_base = urlparse(normalized_base)
    if (
        parsed_base.scheme not in {"http", "https"}
        or not parsed_base.netloc
        or parsed_base.username is not None
        or parsed_base.password is not None
        or parsed_base.query
        or parsed_base.fragment
        or parsed_base.path not in {"", "/"}
    ):
        return None
    return f"{normalized_base.rstrip('/')}{parsed_ref.path}"


def with_generated_artifact_delivery(
    response: AgentResponse,
    *,
    base_url: str | None,
) -> AgentResponse:
    """Attach deterministic public URLs without changing internal output refs."""

    urls = list(
        dict.fromkeys(
            url
            for output_ref in response.output_refs[:MAX_DELIVERED_IMAGE_COUNT]
            if (
                url := generated_artifact_public_url(
                    output_ref,
                    base_url=base_url,
                )
            )
        )
    )
    if not urls:
        return response

    data = dict(response.data or {})
    data["artifact_urls"] = urls
    missing_urls = [url for url in urls if url not in response.message]
    message = response.message
    if missing_urls:
        message = f"{message.rstrip()}\n\n图片链接：\n" + "\n".join(missing_urls)
    return response.model_copy(update={"message": message, "data": data})


def store_remote_artifact(
    url: str,
    *,
    artifact_dir: Path = GENERATED_ARTIFACT_DIR,
    public_prefix: str = GENERATED_ARTIFACT_PUBLIC_PREFIX,
    filename_seed: str,
    timeout_seconds: float = 120.0,
) -> StoredArtifact:
    """Download one remote artifact into local storage and return its public URL."""

    artifact_dir.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "multimodal-agent-artifact-fetcher/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            content_type = response.headers.get("Content-Type", "")
            payload = response.read(MAX_ARTIFACT_BYTES + 1)
    except TimeoutError as exc:
        raise ProviderAdapterError("provider_timeout", "generated image download timed out") from exc
    except urllib.error.HTTPError as exc:
        raise ProviderAdapterError(
            "provider_bad_response",
            f"generated image download failed: HTTP {exc.code}",
        ) from exc
    except urllib.error.URLError as exc:
        raise ProviderAdapterError("provider_unavailable", f"generated image download failed: {exc.reason}") from exc

    if len(payload) > MAX_ARTIFACT_BYTES:
        raise ProviderAdapterError("provider_bad_response", "generated image exceeded local artifact size limit")
    extension = _extension_from_url_or_content_type(url, content_type)
    name = hashlib.sha256(f"{filename_seed}:{url}".encode("utf-8")).hexdigest()[:24]
    path = artifact_dir / f"{name}{extension}"
    path.write_bytes(payload)
    return StoredArtifact(
        path=path,
        download_url=f"{public_prefix.rstrip('/')}/{path.name}",
        source_url=url,
    )


def _is_remote_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _image_media_type(payload: bytes) -> str | None:
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "image/webp"
    return None


def _extension_from_url_or_content_type(url: str, content_type: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return suffix
    guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
    if guessed in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return guessed
    return ".png"
