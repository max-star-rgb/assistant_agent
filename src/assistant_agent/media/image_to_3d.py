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

from assistant_agent.runtime.generated_artifacts import (
    GENERATED_ARTIFACT_PUBLIC_PREFIX,
    generated_artifact_payload,
)
from assistant_agent.media.image_to_3d_completion import (
    ImageTo3DDeliveryTarget,
    ImageTo3DJobRegistry,
    get_image_to_3d_job_registry,
)

RequestJson = Callable[[str, str, bytes | None, dict[str, str]], dict[str, Any]]


@dataclass(frozen=True)
class ImageTo3DSettings:
    td_gen_url: str
    public_base_url: str
    generated_artifact_path: Path
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
        request_json: RequestJson | None = None,
        job_registry: ImageTo3DJobRegistry | None = None,
    ) -> None:
        self.settings = settings
        self._request_json = request_json or self._urlopen_json
        self._job_registry = job_registry or get_image_to_3d_job_registry()

    def start(
        self,
        *,
        user_id: str | None = None,
        session_id: str,
        src_image: str,
        output_format: str = "mp4",
        delivery_target: ImageTo3DDeliveryTarget = "none",
    ) -> ImageTo3DSubmission:
        image_id = src_image.strip()
        if not image_id or Path(image_id).name != image_id:
            raise ImageTo3DError(f"图片不存在：{src_image}")
        artifact = self._resolve_artifact(image_id)
        job = self._job_registry.register(
            user_id=user_id or session_id,
            session_id=session_id,
            source_image_id=image_id,
            delivery_target=delivery_target,
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
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
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

    def _resolve_artifact(self, image_id: str):
        root = self.settings.generated_artifact_path.resolve()
        for suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            filename = f"{image_id}{suffix}"
            candidate = (root / filename).resolve()
            if candidate.parent != root or not candidate.is_file():
                continue
            output_ref = f"{GENERATED_ARTIFACT_PUBLIC_PREFIX}/{filename}"
            artifact = generated_artifact_payload(output_ref, artifact_dir=root)
            if artifact is not None:
                return artifact
        raise ImageTo3DError(f"图片不存在：{image_id}")

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
