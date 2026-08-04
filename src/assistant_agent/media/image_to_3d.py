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

RequestJson = Callable[[str, str, bytes | None, dict[str, str]], dict[str, Any]]


@dataclass(frozen=True)
class ImageTo3DSettings:
    td_gen_url: str
    public_base_url: str
    generated_artifact_path: Path
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class ImageTo3DSubmission:
    status: str
    source_image_id: str


class ImageTo3DError(RuntimeError):
    """Stable, user-safe failure raised by the 3D service adapter."""


class ImageTo3DAdapter:
    def __init__(
        self,
        settings: ImageTo3DSettings,
        *,
        request_json: RequestJson | None = None,
    ) -> None:
        self.settings = settings
        self._request_json = request_json or self._urlopen_json

    def start(
        self,
        *,
        session_id: str,
        chat_index: str = "0",
        src_image: str,
        output_format: str = "mp4",
    ) -> ImageTo3DSubmission:
        image_id = src_image.strip()
        if not image_id or Path(image_id).name != image_id:
            raise ImageTo3DError(f"图片不存在：{src_image}")
        artifact = self._resolve_artifact(image_id)
        callback_url = (
            f"{self.settings.public_base_url.rstrip('/')}/calling-agent-service/v1/"
            f"{urllib.parse.quote(session_id, safe='')}/"
            f"{urllib.parse.quote(chat_index, safe='')}/3d-gen-back"
        )
        payload = {
            "sessionId": session_id,
            "image": artifact.base64_data,
            "pre_cb_url": callback_url,
            "cb_url": callback_url,
            "format": output_format,
        }
        response = self._request_json(
            "POST",
            self.settings.td_gen_url,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            {
                "Content-Type": "application/json",
                "User-Agent": "AgentService/1.0",
            },
        )
        status = _submission_status(response)
        if not _response_accepts_submission(response, status=status):
            raise ImageTo3DError("3D生成服务响应解析失败")
        return ImageTo3DSubmission(
            status=status,
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
                payload = response.read()
        except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            raise ImageTo3DError("无法生成，请检查网络~") from exc
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImageTo3DError("3D生成服务响应解析失败") from exc
        if not isinstance(decoded, dict):
            raise ImageTo3DError("3D生成服务响应解析失败")
        return decoded


def _submission_status(response: dict[str, Any]) -> str:
    response_data = response.get("data")
    candidates: list[Any] = [response.get("status")]
    if isinstance(response_data, dict):
        candidates.append(response_data.get("status"))
        nested = response_data.get("json")
        if isinstance(nested, dict):
            candidates.append(nested.get("status"))
    for candidate in candidates:
        status = str(candidate or "").strip()
        if status:
            return status
    return ""


def _response_accepts_submission(
    response: dict[str, Any],
    *,
    status: str,
) -> bool:
    if not status:
        return False
    if "errCode" in response or "errMessage" in response:
        return (
            str(response.get("errCode")) == "0"
            and str(response.get("errMessage") or "").strip().lower() == "success"
        )
    return status.lower() in {
        "queued",
        "generating",
        "accepted",
        "processing",
        "success",
        "succeeded",
    }
