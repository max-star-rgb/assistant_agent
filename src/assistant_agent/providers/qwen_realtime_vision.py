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
PCM_SAMPLE_RATE = 16_000
PCM_SILENCE_MILLISECONDS = 200
DEFAULT_CLOSE_TIMEOUT_SECONDS = 1.0


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
        self._session_generation = 0
        self._connect_attempts = 0
        self._reconnect_count = 0
        self._connection_reused = False
        self._observation_started_at = 0.0
        self._first_delta_latency_ms: int | None = None
        self._target_sequence: int | None = None
        self._last_observation_diagnostics: dict[str, Any] = {}
        self._closed = False

    @property
    def connection_failures(self) -> int:
        return self._connection_failures

    @property
    def successful_observations(self) -> int:
        return self._successful_observations

    @property
    def last_observation_diagnostics(self) -> dict[str, Any]:
        return dict(self._last_observation_diagnostics)

    def understand_video(self, request: VideoUnderstandingRequest) -> VideoUnderstandingResult:
        started_at = perf_counter()
        self._observation_started_at = started_at
        self._first_delta_latency_ms = None
        self._target_sequence = _safe_sequence(request.metadata.get("frame_sequence"))
        self._connection_reused = False
        if not self.config.api_key:
            return self._failure("provider_unconfigured", "Qwen realtime vision is not configured.", started_at)
        if len(request.frame_refs) != 1:
            return self._failure("invalid_frame_count", "Qwen realtime vision requires exactly one frame.", started_at)
        try:
            image = _jpeg_base64(request.frame_refs[0])
        except (OSError, ValueError) as exc:
            code = "frame_too_large" if "256KB" in str(exc) else "invalid_frame"
            return self._failure(code, str(exc), started_at)

        try:
            deadline = self._clock() + self.config.timeout_seconds
            socket = self._ensure_connection(deadline)
            socket.send(json.dumps(_session_update(instructions=_instructions(request)), ensure_ascii=False))
            self._expect_event(socket, "session.updated", deadline)
            for event in _media_events(image=image):
                socket.send(json.dumps(event))
            self._expect_event(socket, "input_audio_buffer.committed", deadline)
            socket.send(json.dumps({"type": "response.create"}))
            text = self._receive_response(socket, deadline)
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
        except _IncompleteResponse:
            return self._failure(
                "provider_incomplete_response",
                "Qwen realtime vision response did not complete.",
                started_at,
            )
        except Exception:
            self._discard_connection()
            return self._failure("provider_bad_response", "Qwen realtime vision request failed.", started_at)
        self._successful_observations += 1
        self._connection_failures = 0
        self._publish_diagnostics(started_at, completed_sequence=self._target_sequence)
        return result

    def close(self) -> None:
        self._closed = True
        self._discard_connection()

    def _ensure_connection(self, deadline: float) -> Any:
        if self._closed:
            raise RuntimeError("Qwen realtime vision adapter is closed.")
        now = self._clock()
        if self._socket is not None and (
            self._successful_observations >= 20 or now - self._connected_at >= 60.0
        ):
            self._discard_connection()
        if self._socket is not None:
            self._connection_reused = True
            return self._socket
        if self._connection_failures:
            self._sleep(
                min(
                    _backoff_seconds(self._connection_failures),
                    self._remaining(deadline),
                )
            )
            self._remaining(deadline)
        socket: Any | None = None
        try:
            if self._connect_attempts > 0:
                self._reconnect_count += 1
            self._connect_attempts += 1
            socket = self._connect(
                _model_url(self.config.base_url, self.config.model),
                additional_headers={"Authorization": f"Bearer {self.config.api_key}"},
                open_timeout=self._remaining(deadline),
            )
            created = _receive_json(socket, self._remaining(deadline))
            if created.get("type") != "session.created":
                raise ValueError("Expected session.created.")
            socket.send(json.dumps(_session_update()))
            updated = _receive_json(socket, self._remaining(deadline))
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
        self._session_generation += 1
        self._connected_at = now
        self._successful_observations = 0
        self._connection_failures = 0
        return socket

    def _expect_event(self, socket: Any, expected_type: str, deadline: float) -> dict[str, Any]:
        while True:
            event = _receive_json(socket, self._remaining(deadline))
            event_type = event.get("type")
            if event_type == expected_type:
                return event
            if event_type == "error":
                raise RuntimeError("Provider returned an error.")

    def _receive_response(self, socket: Any, deadline: float) -> str:
        deltas: list[str] = []
        while True:
            event = _receive_json(socket, self._remaining(deadline))
            event_type = event.get("type")
            if event_type in {"response.text.delta", "response.output_text.delta"}:
                delta = event.get("delta")
                if isinstance(delta, str):
                    if self._first_delta_latency_ms is None:
                        self._first_delta_latency_ms = max(
                            0,
                            int((perf_counter() - self._observation_started_at) * 1000),
                        )
                    deltas.append(delta)
            elif event_type == "error":
                raise RuntimeError("Provider returned an error.")
            elif event_type == "response.done":
                response = event.get("response")
                if not isinstance(response, dict) or response.get("status") != "completed":
                    raise _IncompleteResponse
                return "".join(deltas)

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError("Qwen realtime vision round deadline exceeded.")
        return remaining

    def _discard_connection(self) -> None:
        socket, self._socket = self._socket, None
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass

    def _failure(self, code: str, message: str, started_at: float) -> VideoUnderstandingResult:
        self._publish_diagnostics(started_at, completed_sequence=None)
        return VideoUnderstandingResult(
            summary="Qwen realtime vision observation failed.",
            provider=self.provider,
            model=self.config.model,
            output_ref="provider://video/qwen-realtime/error",
            errors=[{"code": code, "message": message, "recoverable": True}],
            latency_ms=int((perf_counter() - started_at) * 1000),
        )

    def _publish_diagnostics(
        self,
        started_at: float,
        *,
        completed_sequence: int | None,
    ) -> None:
        self._last_observation_diagnostics = {
            "transport": "websocket",
            "session_generation": self._session_generation or None,
            "connection_reused": self._connection_reused,
            "reconnect_count": self._reconnect_count,
            "target_sequence": self._target_sequence,
            "completed_sequence": completed_sequence,
            "first_delta_latency_ms": self._first_delta_latency_ms,
            "total_observation_latency_ms": max(0, int((perf_counter() - started_at) * 1000)),
        }


def _default_connect(url: str, **kwargs: Any) -> Any:
    from websockets.sync.client import connect

    return connect(url, close_timeout=DEFAULT_CLOSE_TIMEOUT_SECONDS, **kwargs)


def _model_url(base_url: str, model: str) -> str:
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{urlencode({'model': model})}"


def _session_update(*, instructions: str | None = None) -> dict[str, Any]:
    session: dict[str, Any] = {
        "modalities": ["text"],
        "input_audio_format": "pcm",
        "output_audio_format": "pcm",
        "turn_detection": None,
    }
    if instructions is not None:
        session["instructions"] = instructions
    return {
        "type": "session.update",
        "session": session,
    }


def _media_events(*, image: str) -> list[dict[str, Any]]:
    silence_bytes = PCM_SAMPLE_RATE * 2 * PCM_SILENCE_MILLISECONDS // 1000
    silence = base64.b64encode(bytes(silence_bytes)).decode("ascii")
    return [
        {"type": "input_audio_buffer.append", "audio": silence},
        {"type": "input_image_buffer.append", "image": image},
        {"type": "input_audio_buffer.commit"},
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


def _jpeg_base64(frame_ref: str) -> str:
    data = Path(frame_ref).read_bytes()
    if not (data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")):
        raise ValueError("Frame must be a JPEG image.")
    encoded = base64.b64encode(data)
    if len(encoded) > MAX_BASE64_JPEG_BYTES:
        raise ValueError("Base64 JPEG exceeds 256KB.")
    return encoded.decode("ascii")


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


def _safe_sequence(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class _ConnectionFailed(RuntimeError):
    pass


class _IncompleteResponse(RuntimeError):
    pass
