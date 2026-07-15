"""Native WebSocket adapter for Qwen realtime vision observations."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, perf_counter, sleep as blocking_sleep
from typing import Any
from urllib.parse import urlencode

from assistant_agent.schemas.perception import VideoUnderstandingRequest, VideoUnderstandingResult


DEFAULT_QWEN_REALTIME_VISION_BASE_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
DEFAULT_QWEN_REALTIME_VISION_MODEL = "qwen3.5-omni-flash-realtime"
MAX_BASE64_JPEG_BYTES = 256 * 1024
PCM_SAMPLE_RATE = 24_000
PCM_SILENCE_MILLISECONDS = 200


@dataclass(frozen=True)
class QwenRealtimeVisionConfig:
    api_key: str | None = None
    base_url: str = DEFAULT_QWEN_REALTIME_VISION_BASE_URL
    model: str = DEFAULT_QWEN_REALTIME_VISION_MODEL
    timeout_seconds: float = 30.0


class QwenRealtimeVisionAdapter:
    """Persistent, single-in-flight Qwen realtime vision adapter."""

    provider = "qwen"

    def __init__(
        self,
        config: QwenRealtimeVisionConfig,
        *,
        connect: Callable[..., Any] | None = None,
        clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.config = config
        self._connect = connect or _default_connect
        self._clock = clock
        self._sleep = sleep or blocking_sleep
        self._socket: Any | None = None
        self._connected_at = 0.0
        self._successful_observations = 0
        self._connection_failures = 0
        self._closed = False

    @property
    def connection_failures(self) -> int:
        return self._connection_failures

    def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
        started_at = perf_counter()
        if not self.config.api_key:
            return self._failure("provider_unconfigured", "Qwen realtime vision is not configured.", started_at)
        if len(request.frame_refs) != 1:
            return self._failure("invalid_frame_count", "Qwen realtime vision requires exactly one frame.", started_at)
        try:
            image = _jpeg_data_url(request.frame_refs[0])
        except (OSError, ValueError) as exc:
            code = "frame_too_large" if "256KB" in str(exc) else "invalid_frame"
            return self._failure(code, str(exc), started_at)

        try:
            socket = self._ensure_connection()
            for event in _turn_events(image=image, instructions=_instructions(request)):
                socket.send(json.dumps(event, ensure_ascii=False))
            text = self._receive_response(socket)
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError("Realtime response must be a JSON object.")
            result = VideoUnderstandingResult.model_validate(
                {
                    **payload,
                    "provider": self.provider,
                    "model": self.config.model,
                    "output_ref": f"provider://video/qwen-realtime/{_safe_ref(request.video_ref)}",
                    "errors": [],
                    "latency_ms": int((perf_counter() - started_at) * 1000),
                }
            )
        except TimeoutError:
            self._discard_connection()
            return self._failure("provider_timeout", "Qwen realtime vision timed out.", started_at)
        except _ConnectionFailed:
            self._discard_connection()
            return self._failure(
                "provider_connection_failed",
                "Qwen realtime vision connection failed.",
                started_at,
            )
        except (ConnectionError, EOFError):
            self._connection_failures += 1
            self._discard_connection()
            return self._failure(
                "provider_connection_failed",
                "Qwen realtime vision connection failed.",
                started_at,
            )
        except Exception:
            self._discard_connection()
            return self._failure("provider_bad_response", "Qwen realtime vision request failed.", started_at)
        self._successful_observations += 1
        self._connection_failures = 0
        return result

    def close(self) -> None:
        self._closed = True
        self._discard_connection()

    def _ensure_connection(self) -> Any:
        if self._closed:
            raise RuntimeError("Qwen realtime vision adapter is closed.")
        now = self._clock()
        if self._socket is not None and (
            self._successful_observations >= 20 or now - self._connected_at >= 60.0
        ):
            self._discard_connection()
        if self._socket is not None:
            return self._socket
        if self._connection_failures:
            self._sleep(_backoff_seconds(self._connection_failures))
        socket: Any | None = None
        try:
            socket = self._connect(
                _model_url(self.config.base_url, self.config.model),
                additional_headers={"Authorization": f"Bearer {self.config.api_key}"},
                open_timeout=self.config.timeout_seconds,
            )
            created = _receive_json(socket, self.config.timeout_seconds)
            if created.get("type") != "session.created":
                raise ValueError("Expected session.created.")
            socket.send(json.dumps(_session_update()))
            updated = _receive_json(socket, self.config.timeout_seconds)
            if updated.get("type") != "session.updated":
                raise ValueError("Expected session.updated.")
        except TimeoutError:
            self._connection_failures += 1
            if socket is not None:
                try:
                    socket.close()
                except Exception:
                    pass
            raise
        except Exception:
            self._connection_failures += 1
            if socket is not None:
                try:
                    socket.close()
                except Exception:
                    pass
            raise _ConnectionFailed from None
        self._socket = socket
        self._connected_at = now
        self._successful_observations = 0
        return socket

    def _receive_response(self, socket: Any) -> str:
        deltas: list[str] = []
        while True:
            event = _receive_json(socket, self.config.timeout_seconds)
            event_type = event.get("type")
            if event_type in {"response.text.delta", "response.output_text.delta"}:
                delta = event.get("delta")
                if isinstance(delta, str):
                    deltas.append(delta)
            elif event_type == "error":
                raise RuntimeError("Provider returned an error.")
            elif event_type == "response.done":
                return "".join(deltas)

    def _discard_connection(self) -> None:
        socket, self._socket = self._socket, None
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass

    def _failure(self, code: str, message: str, started_at: float) -> VideoUnderstandingResult:
        return VideoUnderstandingResult(
            summary="Qwen realtime vision observation failed.",
            provider=self.provider,
            model=self.config.model,
            output_ref="provider://video/qwen-realtime/error",
            errors=[{"code": code, "message": message, "recoverable": True}],
            latency_ms=int((perf_counter() - started_at) * 1000),
        )


def _default_connect(url: str, **kwargs: Any) -> Any:
    from websockets.sync.client import connect

    return connect(url, **kwargs)


def _model_url(base_url: str, model: str) -> str:
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{urlencode({'model': model})}"


def _session_update() -> dict[str, Any]:
    return {
        "type": "session.update",
        "session": {
            "modalities": ["text"],
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "turn_detection": None,
        },
    }


def _turn_events(*, image: str, instructions: str) -> list[dict[str, Any]]:
    silence_bytes = PCM_SAMPLE_RATE * 2 * PCM_SILENCE_MILLISECONDS // 1000
    silence = base64.b64encode(bytes(silence_bytes)).decode("ascii")
    return [
        {"type": "input_audio_buffer.append", "audio": silence},
        {"type": "input_image_buffer.append", "image": image},
        {"type": "input_audio_buffer.commit"},
        {
            "type": "response.create",
            "response": {"modalities": ["text"], "instructions": instructions},
        },
    ]


def _instructions(request: VideoUnderstandingRequest) -> str:
    history = request.memory_context if isinstance(request.memory_context, str) else "\n".join(request.memory_context or [])
    return (
        "Return one JSON object describing only the current JPEG frame. "
        "Allowed fields: summary, objects, people, actions, events, scene, products, brands, "
        "colors, materials, text_in_video, timestamps, style_tags, confidence.\n"
        f"Current request: {request.user_query or '更新当前画面语义。'}\n"
        f"Previous semantic summary (context only): {history[:4000] or 'none'}"
    )


def _jpeg_data_url(frame_ref: str) -> str:
    data = Path(frame_ref).read_bytes()
    if not (data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")):
        raise ValueError("Frame must be a JPEG image.")
    encoded = base64.b64encode(data)
    if len(encoded) > MAX_BASE64_JPEG_BYTES:
        raise ValueError("Base64 JPEG exceeds 256KB.")
    return "data:image/jpeg;base64," + encoded.decode("ascii")


def _receive_json(socket: Any, timeout: float) -> dict[str, Any]:
    raw = socket.recv(timeout=timeout)
    event = json.loads(raw)
    if not isinstance(event, dict):
        raise ValueError("Provider event must be a JSON object.")
    return event


def _backoff_seconds(failures: int) -> float:
    return (0.25, 0.5, 1.0, 2.0, 5.0)[min(max(failures - 1, 0), 4)]


def _safe_ref(video_ref: str | None) -> str:
    value = (video_ref or "observation").rsplit("/", maxsplit=1)[-1]
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value) or "observation"


class _ConnectionFailed(RuntimeError):
    pass
