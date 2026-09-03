"""Submit an Agent-owned generated image to the image-to-3D service."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from assistant_agent.media.image_to_3d_jobs import (
    ImageTo3DJobRegistry,
    get_image_to_3d_job_registry,
)
from assistant_agent.media.generated_artifacts import GeneratedArtifactPayload

RequestJson = Callable[[str, str, bytes | None, dict[str, str]], dict[str, Any]]
ArtifactPayloadResolver = Callable[[str, str, str], GeneratedArtifactPayload | None]


@dataclass(frozen=True)
class ImageTo3DSettings:
    td_gen_url: str
    public_base_url: str
    timeout_seconds: float = 5.0


@dataclass(frozen=True)
class ImageTo3DSubmission:
    status: str
    source_image_id: str
    job_id: str | None = None


class ImageTo3DError(RuntimeError):
    """Stable, user-safe failure raised by the 3D service adapter."""


class ImageTo3DAdapter:
    def __init__(
        self,
        settings: ImageTo3DSettings,
        *,
        artifact_payload_resolver: ArtifactPayloadResolver,
        request_json: RequestJson | None = None,
        job_registry: ImageTo3DJobRegistry | None = None,
    ) -> None:
        self.settings = settings
        self._artifact_payload_resolver = artifact_payload_resolver
        self._request_json = request_json or self._urlopen_json
        self._job_registry = job_registry or get_image_to_3d_job_registry()

    def start(
        self,
        *,
        user_id: str | None = None,
        session_id: str,
        src_image: str,
        output_format: str = "mp4",
    ) -> ImageTo3DSubmission:
        output_ref = src_image.strip()
        if not output_ref:
            raise ImageTo3DError(f"图片不存在：{src_image}")
        artifact = self._artifact_payload_resolver(
            user_id or session_id,
            session_id,
            output_ref,
        )
        if artifact is None:
            raise ImageTo3DError(f"图片不存在：{src_image}")
        image_id = Path(artifact.image_id).stem
        job = self._job_registry.register(
            user_id=user_id or session_id,
            session_id=session_id,
            source_image_id=image_id,
        )
        callback_url = (
            f"{self.settings.public_base_url.rstrip('/')}/calling-agent-service/v1/"
            f"{urllib.parse.quote(job.job_id, safe='')}/0/3d-gen-back"
        )
        payload = {
            "sessionId": session_id,
            "image": artifact.base64_data,
            "pre_cb_url": callback_url,
            "cb_url": callback_url,
            "format": output_format,
        }
        try:
            self._request_json(
                "POST",
                self.settings.td_gen_url,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                ),
                {
                    "Content-Type": "application/json",
                    "User-Agent": "AgentService/1.0",
                },
            )
        except Exception as exc:
            self._job_registry.fail(job.job_id, error=type(exc).__name__)
            raise
        return ImageTo3DSubmission(
            job_id=job.job_id,
            status="generating",
            source_image_id=image_id,
        )

    def _urlopen_json(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.settings.timeout_seconds,
            ) as response:
                response.read()
        except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            raise ImageTo3DError("无法生成，请检查网络~") from exc
        return {}
