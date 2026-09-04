"""Media-owned access to backend-managed generated artifacts."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from assistant_agent.runtime.thread_resources import (
    ThreadResourceError,
    ThreadResourceManager,
)


MAX_ARTIFACT_BYTES = 25 * 1024 * 1024
MAX_DELIVERED_IMAGE_COUNT = 4
GENERATED_ARTIFACT_URI_AUTHORITY = "v1"


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


def generated_artifact_prefix(thread_ref: str) -> str:
    """Return the canonical URI prefix for one thread's generated images."""

    if not _valid_thread_ref(thread_ref):
        raise ValueError("invalid generated artifact thread reference")
    return f"artifact://{GENERATED_ARTIFACT_URI_AUTHORITY}/{thread_ref}/generated"


def generated_artifact_payload_for_thread(
    output_ref: str,
    manager: ThreadResourceManager,
    *,
    identity: str,
    thread_id: str,
) -> GeneratedArtifactPayload | None:
    """Resolve a generated artifact only when it belongs to the current thread."""

    parsed = _thread_generated_artifact_ref(output_ref)
    if parsed is None:
        return None
    thread_ref, filename = parsed
    try:
        resources = manager.resolve(identity, thread_id)
    except ThreadResourceError:
        return None
    if thread_ref != resources.thread_ref:
        return None
    return generated_artifact_payload(
        output_ref,
        artifact_dir=resources.artifact_root / "generated",
        public_prefix=output_ref[: -(len(filename) + 1)],
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
    if parsed.query or parsed.fragment or not output_ref.startswith(prefix):
        return None
    filename = output_ref.removeprefix(prefix)
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


def _thread_generated_artifact_ref(output_ref: str) -> tuple[str, str] | None:
    parsed = urlparse(output_ref)
    if parsed.query or parsed.fragment:
        return None
    if (
        parsed.scheme == "artifact"
        and parsed.netloc == GENERATED_ARTIFACT_URI_AUTHORITY
    ):
        parts = Path(parsed.path).parts
        if (
            len(parts) == 4
            and parts[0] == "/"
            and _valid_thread_ref(parts[1])
            and parts[2] == "generated"
            and parts[3]
            and Path(parts[3]).name == parts[3]
        ):
            return parts[1], parts[3]
        return None
    if parsed.scheme or parsed.netloc:
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
