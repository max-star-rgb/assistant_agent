"""Local browser capture diagnostics; independent of the video-call protocol."""

import asyncio
import json
import math
from pathlib import Path
import struct
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

router = APIRouter()
_ASSETS = Path(__file__).with_name("browser_media_assets")
_HEADER = struct.Struct("<BId")
_MAX_FRAME = 256 * 1024


def _require_local(request, *, websocket=False):
    user = request.scope.get("user")
    if not user or not getattr(user, "is_authenticated", False):
        raise HTTPException(401, "authenticated identity required")
    host = urlsplit("http://" + request.headers.get("host", ""))
    if request.client is None or request.client.host not in {"127.0.0.1", "::1"} or host.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise HTTPException(403, "capture lab is local only")
    origin = request.headers.get("origin")
    if websocket and (not origin or origin not in {"http://" + host.netloc, "https://" + host.netloc}):
        raise HTTPException(403, "same-origin browser required")


@router.get("/media-lab", include_in_schema=False)
async def capture_page(request: Request):
    _require_local(request)
    return FileResponse(_ASSETS / "index.html", headers={"Cache-Control": "no-store"})


@router.get("/media-lab/assets/{name}", include_in_schema=False)
async def capture_asset(request: Request, name: str):
    _require_local(request)
    if name not in {"client.js", "capture-worklet.js"}:
        raise HTTPException(404)
    return FileResponse(_ASSETS / name, media_type="text/javascript", headers={"Cache-Control": "no-store"})


@router.websocket("/media-lab/ws")
async def voice_socket(socket: WebSocket):
    try:
        _require_local(socket, websocket=True)
    except HTTPException:
        await socket.close(code=1008)
        return
    await socket.accept()
    from assistant_agent.agent_server.browser_voice import serve_voice
    await serve_voice(socket)


@router.websocket("/media-lab/capture/ws")
async def capture_socket(socket: WebSocket):
    try:
        _require_local(socket, websocket=True)
    except HTTPException:
        await socket.close(code=1008)
        return
    await socket.accept()
    await socket.send_json({"type": "session.ready", "session_id": uuid4().hex,
        "mode": "capture_only", "audio_sample_rate": 16000, "max_image_bytes": _MAX_FRAME})
    audio = loopback = False
    samples = images = 0
    sequences = {}
    window = monotonic()
    received = 0
    try:
        while True:
            packet = await asyncio.wait_for(socket.receive(), timeout=120)
            if packet["type"] == "websocket.disconnect":
                return
            now = monotonic()
            if now - window >= 1:
                window, received = now, 0
            received += 1
            if received > 80:
                raise ValueError("发送过快，请降低采集频率。")
            raw = packet.get("bytes")
            if raw is not None:
                if not _HEADER.size < len(raw) <= _MAX_FRAME + _HEADER.size:
                    raise ValueError("媒体包大小无效。")
                kind, seq, timestamp = _HEADER.unpack_from(raw)
                payload = memoryview(raw)[_HEADER.size:]
                if not math.isfinite(timestamp) or timestamp < 0 or seq <= sequences.get(kind, -1):
                    raise ValueError("媒体包序号或时间戳无效。")
                if kind == 1:
                    if not audio or len(payload) % 2 or len(payload) > 6400:
                        raise ValueError("请先开启 16kHz 单声道 PCM16 采集；每包最多 200ms。")
                    samples += len(payload) // 2
                    if loopback:
                        await socket.send_bytes(raw)
                elif kind == 2:
                    # Transport signature only: this receiver does not decode or infer on images.
                    if payload[:2] != b"\xff\xd8" or payload[-2:] != b"\xff\xd9":
                        raise ValueError("图像必须使用 JPEG 格式。")
                    images += 1
                else:
                    raise ValueError("未知媒体类型。")
                sequences[kind] = seq
                await socket.send_json({"type": "media.received", "kind": kind, "sequence": seq,
                    "captured_at_ms": timestamp, "bytes": len(payload),
                    "audio_samples": samples, "image_frames": images})
                continue
            text = packet.get("text", "")
            if len(text) > 2048:
                raise ValueError("控制消息过大。")
            event = json.loads(text)
            if not isinstance(event, dict):
                raise ValueError("控制消息必须是对象。")
            kind = event.get("type")
            if kind == "audio.start":
                if event.get("sample_rate") != 16000 or event.get("channels") != 1 or event.get("format") != "pcm_s16le" or not isinstance(event.get("loopback", False), bool):
                    raise ValueError("音频格式必须是 16kHz 单声道 PCM16。")
                audio, loopback = True, event.get("loopback", False)
                await socket.send_json({"type": "audio.started", "loopback": loopback})
            elif kind == "audio.stop":
                audio = loopback = False
                await socket.send_json({"type": "audio.stopped"})
            elif kind == "ping":
                await socket.send_json({"type": "pong", "time": event.get("time")})
            else:
                raise ValueError("未知控制消息。")
    except WebSocketDisconnect:
        pass
    except (ValueError, TimeoutError) as exc:
        reason = "连接空闲超时。" if isinstance(exc, TimeoutError) else "媒体协议无效。"
        # Never return the parser's exception text, which may contain media data.
        await socket.send_json({"type": "error", "message": reason})
        await socket.close(code=1008)
