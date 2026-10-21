"""Connection-scoped audio/video ↔ Qwen Gateway protocol adapter."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
from io import BytesIO
from pathlib import Path
from collections import deque
from contextlib import suppress
from uuid import uuid4
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from PIL import Image

from .media_protocol import MediaProtocolError, envelope

logger = logging.getLogger(__name__)


def _realtime_jpeg(path: str) -> str:
    """Bound a decoded local frame to the Gateway's 256 KiB image contract."""
    with Image.open(Path(path)) as source:
        image = source.convert("RGB")
    image.thumbnail((1280, 720))
    for quality in (80, 60, 40):
        output = BytesIO()
        image.save(output, format="JPEG", quality=quality)
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        if len(encoded) <= 256 * 1024:
            return encoded
        image.thumbnail((max(1, image.width // 2), max(1, image.height // 2)))
    raise MediaProtocolError("realtime video frame exceeds image size limit")


def parse_pcm_audio(body: dict) -> bytes:
    config = body.get("audioConfig")
    if not isinstance(config, dict) or any(
        config.get(k) != v for k, v in
        {"codec": "pcm_s16le", "sampleRate": 16000, "channels": 1}.items()
    ):
        raise MediaProtocolError("audio requires pcm_s16le, 16000 Hz, mono; mislabeled opus is not accepted")
    items = body.get("contents")
    if not isinstance(items, list) or not 1 <= len(items) <= 10:
        raise MediaProtocolError("audio contents must contain 1..10 items")
    chunks = []
    for item in items:
        value = item.get("audioContent") if isinstance(item, dict) else None
        if not isinstance(value, str) or not 0 < len(value) <= 64000:
            raise MediaProtocolError("audioContent must be bounded hexadecimal PCM")
        try:
            chunk = bytes.fromhex(value)
        except ValueError:
            raise MediaProtocolError("invalid audio hexadecimal data") from None
        if not chunk or len(chunk) % 2:
            raise MediaProtocolError("audio must contain complete PCM16 samples")
        chunks.append(chunk)
    data = b"".join(chunks)
    if len(data) > 32000:
        raise MediaProtocolError("audio frame exceeds one second")
    return data


class AudioDownlink:
    """Project public Gateway events; retain only bounded playback metadata."""

    def __init__(self):
        self.streams: dict[str, dict] = {}
        self.closed = deque(maxlen=128)

    @staticmethod
    def ids(response_id):
        return {"taskId": f"voice:{response_id}", "streamId": f"audio:{response_id}"}

    def project(self, event: dict) -> list[tuple[str, dict]]:
        kind = event.get("type")
        response_id = event.get("responseId")
        if kind == "playback.clear":
            return self.interrupt("NEW_SPEECH")
        if kind == "response.interrupted":
            return self.interrupt("NEW_SPEECH", response_id)
        if kind not in {"audio.delta", "audio.done"}:
            return []
        # Gateway also emits a global audio.done when cancelling before a response exists.
        if kind == "audio.done" and not response_id:
            return []
        if not isinstance(response_id, str) or not 0 < len(response_id) <= 128:
            raise MediaProtocolError("gateway audio missing responseId")
        if response_id in self.closed:
            return []
        state = self.streams.get(response_id)
        if kind == "audio.done":
            if not state or state["ended"]:
                return []
            state["ended"] = True
            return [("audioEnd", {**self.ids(response_id), "lastSequence": state["sequence"] - 1})]
        if state and state["ended"]:
            return []
        rate = event.get("sampleRate")
        if rate != 24000:
            raise MediaProtocolError("gateway output must be 24000 Hz PCM16")
        encoded = event.get("audio")
        if not isinstance(encoded, str) or not 0 < len(encoded) <= 262144:
            raise MediaProtocolError("invalid gateway audio size")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise MediaProtocolError("invalid gateway audio encoding") from None
        if not data or len(data) % 2:
            raise MediaProtocolError("invalid gateway PCM16 samples")
        messages = []
        if state is None:
            if len(self.streams) >= 16:
                raise MediaProtocolError("too many unacknowledged audio streams")
            state = {"sequence": 0, "ended": False}
            self.streams[response_id] = state
            messages.append(("audioStart", {**self.ids(response_id), "codec": "pcm_s16le",
                                           "sampleRate": rate, "channels": 1, "encoding": "base64"}))
        # <= 100 ms per media chunk; no unbounded audio queue or retained payload.
        for offset in range(0, len(data), 4800):
            messages.append(("audioChunk", {**self.ids(response_id), "sequence": state["sequence"],
                                            "audioContent": base64.b64encode(data[offset:offset + 4800]).decode()}))
            state["sequence"] += 1
        return messages

    def interrupt(self, reason: str, response_id: str | None = None):
        messages = []
        for rid in self.streams:
            if (response_id is None or response_id == rid) and rid not in self.closed:
                self.closed.append(rid)
                messages.append(("audioInterrupt", {**self.ids(rid), "reason": reason}))
        return messages

    def receipt(self, body: dict) -> dict | None:
        stream_id = body.get("streamId")
        if not isinstance(stream_id, str) or not stream_id.startswith("audio:"):
            raise MediaProtocolError("invalid audioStatus streamId")
        rid = stream_id[6:]
        if body.get("taskId") != self.ids(rid)["taskId"]:
            raise MediaProtocolError("audioStatus taskId mismatch")
        status = body.get("status")
        if status not in {"accepted", "playing", "completed", "interrupted", "failed"}:
            raise MediaProtocolError("invalid audioStatus status")
        state = self.streams.get(rid)
        if not state:
            if rid in self.closed:
                return None  # Duplicate terminal receipt.
            raise MediaProtocolError("unknown audioStatus streamId")
        if status == "accepted":
            return None
        if status == "completed" and (not state["ended"] or rid in self.closed):
            raise MediaProtocolError("audioStatus completed requires uninterrupted audioEnd")
        if status == "playing" and rid in self.closed:
            return None
        result = {"type": {"playing": "playback.started", "completed": "playback.ended",
                           "interrupted": "playback.cancelled", "failed": "playback.cancelled"}[status],
                  "responseId": rid}
        if status in {"interrupted", "failed"}:
            result["reason"] = "user_interruption" if status == "interrupted" else "media_playback_failed"
        if status != "playing":
            self.closed.append(rid)
            del self.streams[rid]
        return result


class MediaAudioBridge:
    """Use the existing personal Gateway's GCP 7 transport, with no takeover."""

    def __init__(self, *, send, session, gateway_url="ws://127.0.0.1:3101/api/realtime"):
        self.send = send
        self.session = session
        self.gateway_url = gateway_url
        self.downlink = AudioDownlink()
        self.socket = None
        self.reader = None
        self.ready = asyncio.Event()
        self.failed = False
        self.video_active = False
        self.last_video_at = None
        self.video_idle_task = None

    async def emit(self, message, body):
        await asyncio.wait_for(self.send(envelope(message=message,
                              session_id=self.session.protocol_session_id, body=body)), 5)

    async def start(self):
        if os.environ.get("MULTIMODAL_AGENT_PROVIDER_MODE") != "real":
            raise MediaProtocolError("media audio requires explicitly enabled real mode")
        try:
            endpoint = urlsplit(self.gateway_url)
            self.socket = await connect(self.gateway_url, origin=f"http://{endpoint.netloc}",
                                        open_timeout=5, close_timeout=2, max_size=524288, max_queue=8)
            await self.write({"type": "session.hello", "protocol": {"min": "7.0.0", "max": "7.0.0"},
                              "client": {"type": "external-media", "instance_id": self.session.connection_id},
                              "capabilities": ["input.audio", "playback.receipts", *(
                                  ["input.image_buffer", "client.events"]
                                  if getattr(self.session, "call_type", None) == "VIDEO" else []
                              )],
                              "connection": {"provider": "dashscope", "takeover": False}})
            self.reader = asyncio.create_task(self.read())
            await asyncio.wait_for(self.ready.wait(), 30)
            if self.failed:
                raise MediaProtocolError("Qwen Gateway unavailable or occupied; reconnect after releasing voice")
            if getattr(self.session, "call_type", None) == "VIDEO":
                await self._publish_video_state(False)
        except Exception:
            await self.close()
            raise MediaProtocolError("Qwen Gateway audio initialization failed; check Gateway availability/voice ownership") from None

    async def write(self, event):
        if self.socket is None or self.failed:
            raise MediaProtocolError("Qwen Gateway audio is unavailable; reconnect media session")
        try:
            await asyncio.wait_for(self.socket.send(json.dumps({**event, "event_id": str(uuid4())})), 5)
        except Exception:
            raise MediaProtocolError("Qwen Gateway audio send failed; reconnect media session") from None

    async def append(self, body):
        data = parse_pcm_audio(body)
        if not self.ready.is_set():
            raise MediaProtocolError("Qwen Gateway audio is not ready")
        await self.write({"type": "input_audio_buffer.append", "audio": base64.b64encode(data).decode()})

    async def _publish_video_state(self, active):
        await self.write({
            "type": "client.event.publish", "name": "media.visual_input.changed",
            "delivery_hint": "context",
            "text": (
                "客户端正在提供实时画面，仅根据实际收到的最新画面描述当前环境。这是能力状态，无需主动播报。"
                if active else
                "客户端当前没有提供实时画面，不能描述当前环境。此前画面仅代表历史。这是能力状态，无需主动播报。"
            ),
        })

    async def append_video(self, frame):
        if getattr(self.session, "call_type", None) != "VIDEO" or not self.ready.is_set():
            raise MediaProtocolError("realtime video requires a ready VIDEO audioStreamV1 session")
        now = asyncio.get_running_loop().time()
        if self.last_video_at is not None and now - self.last_video_at < 1.0:
            return False
        try:
            image = await asyncio.to_thread(_realtime_jpeg, frame.uri)
        except (OSError, ValueError, Image.DecompressionBombError):
            raise MediaProtocolError("realtime video JPEG conversion failed") from None
        await self.write({"type": "input_image_buffer.append", "image": image,
                          "media_type": "image/jpeg",
                          **({"occurred_at": frame.timestamp_ms}
                             if frame.timestamp_ms is not None and frame.timestamp_ms >= 0 else {})})
        self.last_video_at = asyncio.get_running_loop().time()
        if not self.video_active:
            await self._publish_video_state(True)
            self.video_active = True
        if self.video_idle_task is not None:
            self.video_idle_task.cancel()
            await asyncio.gather(self.video_idle_task, return_exceptions=True)
        self.video_idle_task = asyncio.create_task(self._expire_video(), name="media-video-idle")
        return True

    async def _expire_video(self):
        try:
            await asyncio.sleep(3)
            await self.clear_video()
        except MediaProtocolError:
            logger.warning("media_video_state_delivery_failed")

    async def clear_video(self, *, notify=True):
        task, self.video_idle_task = self.video_idle_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.video_active:
            self.video_active = False
            self.last_video_at = None
            if notify:
                await self.write({"type": "input_image_buffer.clear"})
                await self._publish_video_state(False)

    async def interrupt(self):
        for message, body in self.downlink.interrupt("MEDIA_INTERRUPT"):
            await self.emit(message, body)
        await self.write({"type": "response.cancel"})

    async def receipt(self, body):
        event = self.downlink.receipt(body)
        if event:
            await self.write(event)

    async def read(self):
        failure_code = "GATEWAY_UNAVAILABLE"
        try:
            async for raw in self.socket:
                event = json.loads(raw)
                kind = event.get("type")
                if kind == "session.ping":
                    await self.write({"type": "session.pong", "request_event_id": event["event_id"]})
                elif kind == "voice.ready":
                    if event.get("inputSampleRate") != 16000 or event.get("provider") != "dashscope":
                        raise MediaProtocolError("unsupported Gateway audio provider/format")
                    self.ready.set()
                elif kind == "error" or (kind == "voice.ownership" and event.get("state") == "busy") or kind == "voice.deactivated":
                    if event.get("error", {}).get("code") == "client_occupied" or event.get("state") == "busy":
                        failure_code = "GATEWAY_OCCUPIED"
                    raise MediaProtocolError("gateway audio unavailable")
                elif kind == "voice.connection" and event.get("state") in {"disconnected", "reconnecting", "unavailable"}:
                    raise MediaProtocolError("gateway audio connection interrupted")
                elif kind == "transcript.final" and event.get("role") == "assistant":
                    rid = event.get("responseId")
                    text = event.get("content")
                    if isinstance(rid, str) and isinstance(text, str) and text:
                        await self.emit("chatResponse", {**self.downlink.ids(rid), "number": self.session.user_id,
                            "message": {"type": "BRIEF", "chatIndex": self.downlink.ids(rid)["taskId"],
                                        "content": {"intentResult": {"description": text, "status": "SUCCESS"}}},
                            "displayOnly": True, "display_only": True, "audioSource": "model",
                            "sequence": 1, "final": True})
                for message, body in self.downlink.project(event):
                    await self.emit(message, body)
            raise MediaProtocolError("gateway disconnected")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failed = True
            self.ready.set()
            logger.warning("media_audio_failed code=%s error_type=%s detail=%s", failure_code,
                           type(exc).__name__, str(exc) if isinstance(exc, MediaProtocolError) else "transport_failure")
            with suppress(Exception):
                for message, body in self.downlink.interrupt("GATEWAY_ERROR"):
                    await self.emit(message, body)
                await self.emit("audioError", {"code": failure_code, "message": "Gateway 语音被占用，请释放后重连" if failure_code == "GATEWAY_OCCUPIED" else "音频连接失败，请重新连接媒体会话"})
            if self.socket:
                await self.socket.close()

    async def close(self):
        # The owning media transport may already be closed.
        await self.clear_video(notify=False)
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.socket:
            await self.socket.close()
        self.downlink.streams.clear()


class GatewayVisualBridge(MediaAudioBridge):
    """Return only decoded visual events to the owning Gateway; no model connection."""

    async def start(self):
        self.ready.set()
        await self._publish_video_state(False)

    async def write(self, event):
        if event.get("type") not in {
            "input_image_buffer.append", "input_image_buffer.clear", "client.event.publish"
        }:
            raise MediaProtocolError("internal visual channel only accepts visual events")
        await self.emit("gatewayVisualEvent", event)
