"""Bounded read projection for locally generated image artifacts."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[3]
GENERATED_ARTIFACT_DIR = REPO_ROOT / ".local" / "generated"
GENERATED_ARTIFACT_PUBLIC_PREFIX = "/artifacts/generated"
MAX_ARTIFACT_BYTES = 25 * 1024 * 1024
MAX_DELIVERED_IMAGE_COUNT = 4


@dataclass(frozen=True)
class GeneratedArtifactPayload:
    image_id: str
    media_type: str
    base64_data: str


def generated_artifact_payload(
    output_ref: str,
    *,
    artifact_dir: Path | None = None,
    max_bytes: int = MAX_ARTIFACT_BYTES,
) -> GeneratedArtifactPayload | None:
    """Read one backend-owned image without importing legacy Runtime DTOs."""

    parsed = urlparse(output_ref)
    prefix = GENERATED_ARTIFACT_PUBLIC_PREFIX.rstrip("/") + "/"
    if (
        max_bytes <= 0
        or parsed.scheme
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
    media_type = _image_media_type(payload)
    if not payload or len(payload) > max_bytes or media_type is None:
        return None
    return GeneratedArtifactPayload(
        image_id=filename,
        media_type=media_type,
        base64_data=base64.b64encode(payload).decode("ascii"),
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


__all__ = [
    "MAX_DELIVERED_IMAGE_COUNT",
    "GeneratedArtifactPayload",
    "generated_artifact_payload",
]
