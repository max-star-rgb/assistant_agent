"""Local artifact storage for generated media returned by providers."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from langchain_core.messages import HumanMessage, ToolMessage

from assistant_agent.tools.plugins.builtin.image_generation.models import (
    ImageGenerationResult,
)
from assistant_agent.providers.provider_errors import ProviderAdapterError
from assistant_agent.tools.ids import IMAGE_GENERATION_TOOL_NAME
from assistant_agent.runtime.thread_resources import (
    ThreadResourceError,
    ThreadResourceManager,
)


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


@dataclass(frozen=True)
class GeneratedArtifactFile:
    """Validated generated image file safe to serve from the custom route."""

    path: Path
    media_type: str


def generated_artifact_location(
    output_ref: str,
    manager: ThreadResourceManager,
) -> GeneratedArtifactFile | None:
    parsed = _thread_generated_artifact_ref(output_ref)
    if parsed is None:
        return None
    thread_ref, filename = parsed
    try:
        root = manager.resolve_artifact_root(thread_ref) / "generated"
    except ThreadResourceError:
        return None
    return generated_artifact_file(filename, artifact_dir=root)


def generated_artifact_payload_for_ref(
    output_ref: str,
    manager: ThreadResourceManager,
) -> GeneratedArtifactPayload | None:
    parsed = _thread_generated_artifact_ref(output_ref)
    if parsed is None:
        return None
    thread_ref, _filename = parsed
    try:
        root = manager.resolve_artifact_root(thread_ref) / "generated"
    except ThreadResourceError:
        return None
    return generated_artifact_payload(
        output_ref,
        artifact_dir=root,
        public_prefix=f"/artifacts/{thread_ref}/generated",
    )


def generated_image_output_refs(messages: Sequence[Any]) -> list[str]:
    """Return managed image refs produced by successful tools in the latest turn."""

    refs: list[str] = []
    for message in reversed(messages):
        if _is_human_message(message):
            break
        data = _message_data(message)
        if not _is_successful_image_tool_message(message, data):
            continue
        artifact = data.get("artifact")
        if not isinstance(artifact, Mapping):
            continue
        candidates = _artifact_output_ref_candidates(artifact)
        for candidate in candidates:
            if not _is_managed_generated_ref(candidate) or candidate in refs:
                continue
            refs.append(candidate)
            if len(refs) >= MAX_DELIVERED_IMAGE_COUNT:
                return refs
    return refs


def materialize_image_generation_result(
    result: ImageGenerationResult,
    *,
    artifact_dir: Path,
    public_prefix: str,
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
    artifact_dir: Path,
    public_prefix: str,
    max_bytes: int = MAX_ARTIFACT_BYTES,
) -> str | None:
    """Read one backend-owned generated image as a bounded data URL."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    parsed = urlparse(output_ref)
    prefix = public_prefix.rstrip("/") + "/"
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

    root = artifact_dir.resolve()
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


def generated_artifact_file(
    filename: str,
    *,
    artifact_dir: Path,
    max_bytes: int = MAX_ARTIFACT_BYTES,
) -> GeneratedArtifactFile | None:
    """Resolve one bounded image filename inside the managed artifact root."""

    if max_bytes <= 0 or not filename or Path(filename).name != filename:
        return None
    root = artifact_dir.resolve()
    path = (root / filename).resolve()
    if path.parent != root:
        return None
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            return None
        with path.open("rb") as file:
            header = file.read(16)
    except OSError:
        return None
    media_type = _image_media_type(header)
    if media_type is None:
        return None
    return GeneratedArtifactFile(path=path, media_type=media_type)


def generated_artifact_payload(
    output_ref: str,
    *,
    artifact_dir: Path,
    public_prefix: str,
    max_bytes: int = MAX_ARTIFACT_BYTES,
) -> GeneratedArtifactPayload | None:
    """Read one backend-owned image for the IMAGE rendering detail."""

    data_url = generated_artifact_data_url(
        output_ref,
        artifact_dir=artifact_dir,
        public_prefix=public_prefix,
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


def store_remote_artifact(
    url: str,
    *,
    artifact_dir: Path,
    public_prefix: str,
    filename_seed: str,
    timeout_seconds: float = 120.0,
) -> StoredArtifact:
    """Download one remote artifact into local storage and return its public URL."""

    artifact_dir.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url, headers={"User-Agent": "multimodal-agent-artifact-fetcher/1.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            content_type = response.headers.get("Content-Type", "")
            payload = response.read(MAX_ARTIFACT_BYTES + 1)
    except TimeoutError as exc:
        raise ProviderAdapterError(
            "provider_timeout", "generated image download timed out"
        ) from exc
    except urllib.error.HTTPError as exc:
        raise ProviderAdapterError(
            "provider_bad_response",
            f"generated image download failed: HTTP {exc.code}",
        ) from exc
    except urllib.error.URLError as exc:
        raise ProviderAdapterError(
            "provider_unavailable", f"generated image download failed: {exc.reason}"
        ) from exc

    if len(payload) > MAX_ARTIFACT_BYTES:
        raise ProviderAdapterError(
            "provider_bad_response",
            "generated image exceeded local artifact size limit",
        )
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


def _thread_generated_artifact_ref(output_ref: str) -> tuple[str, str] | None:
    parsed = urlparse(output_ref)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    parts = Path(parsed.path).parts
    if (
        len(parts) != 5
        or parts[0] != "/"
        or parts[1] != "artifacts"
        or not _valid_thread_ref(parts[2])
        or parts[3] != "generated"
        or not parts[4]
        or Path(parts[4]).name != parts[4]
    ):
        return None
    return parts[2], parts[4]


def _artifact_output_ref_candidates(artifact: Mapping[str, Any]) -> list[Any]:
    images = artifact.get("images")
    if isinstance(images, list):
        return [
            image.get("output_ref") for image in images if isinstance(image, Mapping)
        ]
    legacy_urls = artifact.get("download_urls")
    if isinstance(legacy_urls, (list, tuple)) and legacy_urls:
        return list(legacy_urls)
    return [artifact.get("output_ref")]


def _message_data(message: Any) -> Mapping[str, Any]:
    if isinstance(message, Mapping):
        nested = message.get("data")
        return nested if isinstance(nested, Mapping) else message
    return {
        "type": getattr(message, "type", None),
        "name": getattr(message, "name", None),
        "status": getattr(message, "status", None),
        "artifact": getattr(message, "artifact", None),
    }


def _is_human_message(message: Any) -> bool:
    if isinstance(message, HumanMessage):
        return True
    data = _message_data(message)
    return data.get("type") == "human" or data.get("role") == "user"


def _is_successful_image_tool_message(
    message: Any,
    data: Mapping[str, Any],
) -> bool:
    if not isinstance(message, ToolMessage) and data.get("type") != "tool":
        return False
    if data.get("name") != IMAGE_GENERATION_TOOL_NAME:
        return False
    if data.get("status") == "error":
        return False
    artifact = data.get("artifact")
    return isinstance(artifact, Mapping) and artifact.get("status") == "succeeded"


def _is_managed_generated_ref(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parts = Path(value).parts
    if (
        len(parts) != 5
        or parts[0] != "/"
        or parts[1] != "artifacts"
        or not _valid_thread_ref(parts[2])
        or parts[3] != "generated"
    ):
        return False
    filename = parts[4]
    return bool(filename) and Path(filename).name == filename


def _valid_thread_ref(value: str) -> bool:
    return len(value) == 32 and all(
        character in "0123456789abcdef" for character in value
    )


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
