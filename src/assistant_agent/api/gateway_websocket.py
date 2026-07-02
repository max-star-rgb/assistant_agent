"""Gateway WebSocket entry adapters."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import uuid
from collections.abc import Callable, Iterable
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from assistant_agent.api.auth import get_websocket_auth_context, require_auth_bound_identity
from assistant_agent.api.gateway_runtime import get_gateway_bridge
from assistant_agent.api.routes_agent import get_trial_access_gate
from assistant_agent.gateway import CALL_HANGUP, CALL_INCOMING, CONFIG_UPDATE, Frame, dumps_frame, frame, loads_frame
from assistant_agent.gateway.ws import WsProtocolError
from assistant_agent.services.api_identity import (
    IdentityPolicyError,
    ResolvedRequestIdentity,
    enforce_identity_policy,
    resolve_request_identity,
)

router = APIRouter()

GatewayFrameMapper = Callable[[str], list[Frame]]


@router.websocket("/ws/gateway")
async def gateway_websocket(
    websocket: WebSocket,
    user_id: str = Query(default="gateway_user"),
    session_id: str | None = Query(default=None),
    client_kind: str = Query(default="gateway", alias="client"),
) -> None:
    """Accept Gateway JSON frames from product-neutral realtime clients."""

    identity = await _authorize_gateway_websocket(
        websocket,
        user_id=user_id,
        session_id=session_id,
        client_kind=client_kind,
    )
    if identity is None:
        return
    await websocket.accept()
    endpoint = _FastAPIGatewayEndpoint(
        websocket=websocket,
        identity=identity,
        default_session_id=session_id or identity.identity.session_id,
        source="gateway_websocket",
        mapper=_gateway_frame_mapper,
    )
    await get_gateway_bridge().bridge(
        client_id=f"gateway-ws-{uuid.uuid4()}",
        client_ep=endpoint,  # type: ignore[arg-type]
        user_id=identity.identity.user_id,
        session_id=identity.identity.session_id or session_id,
    )


@router.websocket("/ws/realtime/media")
async def realtime_media_websocket(
    websocket: WebSocket,
    user_id: str = Query(default="media_user"),
    session_id: str | None = Query(default=None),
    client_kind: str = Query(default="media_service", alias="client"),
) -> None:
    """Accept media-service events and adapt them to Gateway frames."""

    identity = await _authorize_gateway_websocket(
        websocket,
        user_id=user_id,
        session_id=session_id,
        client_kind=client_kind,
    )
    if identity is None:
        return
    await websocket.accept()
    endpoint = _FastAPIGatewayEndpoint(
        websocket=websocket,
        identity=identity,
        default_session_id=session_id or identity.identity.session_id,
        source="realtime_media_websocket",
        mapper=_media_event_mapper,
    )
    await get_gateway_bridge().bridge(
        client_id=f"media-ws-{uuid.uuid4()}",
        client_ep=endpoint,  # type: ignore[arg-type]
        user_id=identity.identity.user_id,
        session_id=identity.identity.session_id or session_id,
    )


class _FastAPIGatewayEndpoint:
    """Present a FastAPI WebSocket as a Gateway Endpoint."""

    def __init__(
        self,
        *,
        websocket: WebSocket,
        identity: ResolvedRequestIdentity,
        default_session_id: str | None,
        source: str,
        mapper: GatewayFrameMapper,
    ) -> None:
        self._websocket = websocket
        self._identity = identity
        self._default_session_id = default_session_id
        self._source = source
        self._mapper = mapper
        self._send_lock = asyncio.Lock()
        self._pending: asyncio.Queue[Frame] = asyncio.Queue()

    async def send(self, f: Frame) -> None:
        async with self._send_lock:
            await self._websocket.send_text(dumps_frame(f))

    def _inject(self, f: Frame) -> None:
        self._pending.put_nowait(f)

    async def close(self) -> None:
        await self._websocket.close()

    async def __aiter__(self):
        while True:
            while not self._pending.empty():
                yield self._pending.get_nowait()
            try:
                raw = await self._websocket.receive_text()
            except WebSocketDisconnect:
                return
            try:
                frames = self._mapper(raw)
            except WsProtocolError as exc:
                await self.send(_error_frame("invalid_frame", str(exc), user_id=self._identity.identity.user_id))
                continue
            for inbound in frames:
                normalized = await self._normalize_frame(inbound)
                if normalized is not None:
                    yield normalized

    async def _normalize_frame(self, f: Frame) -> Frame | None:
        identity = self._identity.identity
        frame_user_id = _optional_string(f.get("user_id"))
        if frame_user_id and frame_user_id != identity.user_id:
            await self.send(
                _error_frame(
                    "identity_mismatch",
                    "frame user_id does not match websocket identity",
                    user_id=identity.user_id,
                )
            )
            return None

        normalized: Frame = dict(f)  # type: ignore[assignment]
        normalized["user_id"] = identity.user_id
        if self._default_session_id and not normalized.get("session_id"):
            normalized["session_id"] = self._default_session_id
        if normalized.get("type") == "message.user":
            normalized["payload"] = _message_payload_with_metadata(
                normalized.get("payload"),
                source=self._source,
                identity=self._identity,
            )
        return normalized


async def _authorize_gateway_websocket(
    websocket: WebSocket,
    *,
    user_id: str,
    session_id: str | None,
    client_kind: str,
) -> ResolvedRequestIdentity | None:
    auth_context = get_websocket_auth_context(websocket)
    try:
        identity_resolution = resolve_request_identity(
            user_id=user_id,
            session_id=session_id,
            source="websocket_query",
            auth_context=auth_context,
        )
        enforce_identity_policy(
            identity_resolution,
            production_required=require_auth_bound_identity(),
        )
    except IdentityPolicyError as exc:
        await _send_gateway_error_and_close(
            websocket,
            code=exc.code,
            message=str(exc),
            detail=exc.detail(),
        )
        return None
    except ValueError as exc:
        await _send_gateway_error_and_close(
            websocket,
            code="ACCESS_DENIED",
            message=str(exc),
            detail={"user_id": user_id},
        )
        return None

    access = identity_resolution.trial_access(get_trial_access_gate())
    if not access.allowed and not _can_bypass_trial_access(websocket, client_kind):
        await _send_gateway_error_and_close(
            websocket,
            code="ACCESS_DENIED",
            message=access.reason or "trial user is not allowed",
            user_id=identity_resolution.identity.user_id,
            detail={"user_id": identity_resolution.identity.user_id},
        )
        return None
    return identity_resolution


async def _send_gateway_error_and_close(
    websocket: WebSocket,
    *,
    code: str,
    message: str,
    user_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    await websocket.accept()
    await websocket.send_text(
        dumps_frame(
            frame(
                type="error",
                user_id=user_id,
                error={
                    "code": code,
                    "message": message,
                    "detail": detail or {},
                    "recoverable": True,
                },
            )
        )
    )
    await websocket.close(code=1008)


def _gateway_frame_mapper(raw: str) -> list[Frame]:
    return [loads_frame(raw)]


def _media_event_mapper(raw: str) -> list[Frame]:
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WsProtocolError(f"invalid json: {exc}") from exc
    if not isinstance(event, dict):
        raise WsProtocolError("media event must be a JSON object")

    event_type = str(event.get("type") or "").strip()
    payload = _event_payload(event)
    session_id = _optional_string(event.get("session_id") or payload.get("session_id"))
    user_id = _optional_string(event.get("user_id") or payload.get("user_id"))

    if event_type in {"call.incoming", "session.start", "media.start"}:
        config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
        return [
            frame(
                type=CALL_INCOMING,
                session_id=session_id,
                user_id=user_id,
                payload={"config": dict(config), "session_id": session_id},
            )
        ]

    if event_type in {"call.hangup", "session.end", "media.end"}:
        return [frame(type=CALL_HANGUP, session_id=session_id, user_id=user_id)]

    if event_type in {"run.cancel", "cancel"}:
        return [
            frame(
                type="run.cancel",
                session_id=session_id,
                run_id=_optional_string(event.get("run_id") or payload.get("run_id")),
                user_id=user_id,
            )
        ]

    if event_type == "config.update":
        return [frame(type=CONFIG_UPDATE, session_id=session_id, user_id=user_id, payload=payload)]

    if event_type == "ping":
        return [frame(type="ping", user_id=user_id)]

    if event_type in {"message.user", "text", "message.text", "transcript", "transcript.final", "video"}:
        if event.get("final") is False or payload.get("final") is False:
            return []
        text = _optional_string(event.get("text") or payload.get("text"))
        video_ids = _ids_from_event(event, payload, names=("video_ids", "video_id", "video_ref"))
        image_ids = _ids_from_event(event, payload, names=("image_ids", "image_id", "image_ref"))
        audio_id = _optional_string(event.get("audio_id") or payload.get("audio_id"))
        media_payload: dict[str, Any] = {
            "text": text or _default_media_text(video_ids=video_ids, image_ids=image_ids, audio_id=audio_id),
            "modality": "text",
            "image_ids": image_ids,
            "video_ids": video_ids,
        }
        if audio_id:
            media_payload["audio_id"] = audio_id
        media_metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        if media_metadata:
            media_payload["metadata"] = dict(media_metadata)
        return [
            frame(
                type="message.user",
                session_id=session_id,
                user_id=user_id,
                payload=media_payload,
            )
        ]

    raise WsProtocolError(f"unknown media event type: {event_type}")


def _message_payload_with_metadata(
    payload: Any,
    *,
    source: str,
    identity: ResolvedRequestIdentity,
) -> dict[str, Any]:
    normalized = dict(payload) if isinstance(payload, dict) else {}
    metadata = dict(normalized.get("metadata") or {})
    metadata.setdefault("source", source)
    metadata["transport"] = "websocket"
    metadata["request_identity"] = identity.metadata()
    normalized["metadata"] = metadata
    return normalized


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


def _ids_from_event(
    event: dict[str, Any],
    payload: dict[str, Any],
    *,
    names: Iterable[str],
) -> list[str]:
    values: list[str] = []
    for name in names:
        values.extend(_string_list(event.get(name)))
        values.extend(_string_list(payload.get(name)))
    deduped: list[str] = []
    for value in values:
        if value and value not in deduped:
            deduped.append(value)
    return deduped


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _default_media_text(
    *,
    video_ids: list[str],
    image_ids: list[str],
    audio_id: str | None,
) -> str:
    if video_ids:
        return "请结合这段视频继续处理用户请求。"
    if image_ids:
        return "请结合这张图片继续处理用户请求。"
    if audio_id:
        return "请结合这段音频继续处理用户请求。"
    return ""


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _error_frame(code: str, message: str, *, user_id: str | None = None) -> Frame:
    return frame(
        type="error",
        user_id=user_id,
        error={"code": code, "message": message, "recoverable": True},
    )


def _can_bypass_trial_access(websocket: WebSocket, client_kind: str) -> bool:
    if client_kind != "cli":
        return False
    host = websocket.client.host if websocket.client is not None else ""
    return _is_local_client_host(host)


def _is_local_client_host(host: str) -> bool:
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
