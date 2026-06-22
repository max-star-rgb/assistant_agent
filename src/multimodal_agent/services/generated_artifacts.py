"""Local artifact storage for generated media returned by providers."""

from __future__ import annotations

import hashlib
import mimetypes
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from multimodal_agent.schemas.generation import ImageGenerationResult
from multimodal_agent.services.provider_errors import ProviderAdapterError


REPO_ROOT = Path(__file__).resolve().parents[3]
GENERATED_ARTIFACT_DIR = REPO_ROOT / ".local" / "generated"
GENERATED_ARTIFACT_PUBLIC_PREFIX = "/artifacts/generated"
MAX_ARTIFACT_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class StoredArtifact:
    """A generated artifact stored by this backend."""

    path: Path
    download_url: str
    source_url: str


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


def _extension_from_url_or_content_type(url: str, content_type: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return suffix
    guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
    if guessed in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return guessed
    return ".png"
