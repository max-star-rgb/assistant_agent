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
from assistant_agent.gateway import (
    CALL_HANGUP,
    CALL_INCOMING,
    CONFIG_UPDATE,
    Frame,
    dumps_frame,
    frame,
    loads_frame,
)
from assistant_agent.gateway.ws import WsProtocolError
from assistant_agent.services.api_identity import (
    IdentityPolicyError,
    ResolvedRequestIdentity,
    enforce_identity_policy,
    resolve_request_identity,
)

router = APIRouter()

GatewayFrameMapper = Callable[[str], list[Frame]]

MEDIA_EVENT_SESSION_START = "session.start"
MEDIA_EVENT_TRANSCRIPT_FINAL = "transcript.final"
MEDIA_EVENT_RUN_CANCEL = "run.cancel"
MEDIA_EVENT_CONFIG_UPDATE = "config.update"
MEDIA_EVENT_SESSION_END = "session.end"
MEDIA_EVENT_PING = "ping"

SUPPORTED_MEDIA_EVENT_TYPES = frozenset(
    {
        MEDIA_EVENT_SESSION_START,
        MEDIA_EVENT_TRANSCRIPT_FINAL,
        MEDIA_EVENT_RUN_CANCEL,
        MEDIA_EVENT_CONFIG_UPDATE,
        MEDIA_EVENT_SESSION_END,
        MEDIA_EVENT_PING,
    }
)

MEDIA_CONFIG_STRING_KEYS = frozenset(
    {
        "language",
        "locale",
        "mode",
        "response_mode",
        "entry",
        "interrupt_policy",
        "turn_detection",
        "tts_voice",
        "stt_language",
        "call_id",
    }
)
MEDIA_CONFIG_BOOL_KEYS = frozenset({"identity_bound", "barge_in_enabled"})
MEDIA_CONFIG_INT_KEYS = frozenset(
    {"run_timeout_ms", "idle_timeout_ms", "response_timeout_ms", "timeout_ms"}
)
MEDIA_CONFIG_DICT_KEYS = frozenset({"media", "relay", "stt", "tts"})


class MediaEventProtocolError(WsProtocolError):
    """Protocol error for `/ws/realtime/media` event validation."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


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
            except MediaEventProtocolError as exc:
                await self.send(
                    _error_frame(
                        exc.code,
                        str(exc),
                        user_id=self._identity.identity.user_id,
                        detail=exc.detail,
                    )
                )
                continue
            except WsProtocolError as exc:
                await self.send(
                    _error_frame("invalid_frame", str(exc), user_id=self._identity.identity.user_id)
                )
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

        frame_session_id = _optional_string(f.get("session_id"))
        if (
            self._default_session_id
            and frame_session_id
            and frame_session_id != self._default_session_id
        ):
            await self.send(
                _error_frame(
                    "session_mismatch",
                    "frame session_id does not match websocket identity",
                    user_id=identity.user_id,
                    detail={
                        "expected_session_id": self._default_session_id,
                        "received_session_id": frame_session_id,
                    },
                )
            )
            return None

        normalized: Frame = dict(f)  # type: ignore[assignment]
        normalized["user_id"] = identity.user_id
        if self._default_session_id and not normalized.get("session_id"):
            normalized["session_id"] = self._default_session_id
        if self._source == "realtime_media_websocket":
            normalized_session_id = _optional_string(normalized.get("session_id"))
            if normalized.get("type") != "ping" and not normalized_session_id:
                await self.send(
                    _error_frame(
                        "missing_session_id",
                        "media events require session_id in the event or websocket query",
                        user_id=identity.user_id,
                    )
                )
                return None
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
        raise MediaEventProtocolError(
            "invalid_media_event",
            f"invalid json: {exc}",
        ) from exc
    if not isinstance(event, dict):
        raise MediaEventProtocolError(
            "invalid_media_event",
            "media event must be a JSON object",
        )

    event_type = str(event.get("type") or "").strip()
    if not event_type:
        raise MediaEventProtocolError(
            "invalid_media_event",
            "media event type is required",
        )
    if event_type not in SUPPORTED_MEDIA_EVENT_TYPES:
        raise MediaEventProtocolError(
            "unknown_media_event",
            f"unknown media event type: {event_type}",
            detail={"supported_types": sorted(SUPPORTED_MEDIA_EVENT_TYPES)},
        )

    payload = _event_payload(event)
    session_id = _optional_string(event.get("session_id") or payload.get("session_id"))
    user_id = _optional_string(event.get("user_id") or payload.get("user_id"))

    if event_type == MEDIA_EVENT_SESSION_START:
        return [_media_session_start_frame(event, payload, session_id=session_id, user_id=user_id)]

    if event_type == MEDIA_EVENT_SESSION_END:
        return [
            frame(
                type=CALL_HANGUP,
                session_id=session_id,
                user_id=user_id,
                payload=_media_session_end_payload(event, payload),
            )
        ]

    if event_type == MEDIA_EVENT_RUN_CANCEL:
        return [
            frame(
                type="run.cancel",
                session_id=session_id,
                run_id=_optional_string(event.get("run_id") or payload.get("run_id")),
                user_id=user_id,
            )
        ]

    if event_type == MEDIA_EVENT_CONFIG_UPDATE:
        config = _config_from_media_event(event, payload, required=True)
        return [
            frame(
                type=CONFIG_UPDATE,
                session_id=session_id,
                user_id=user_id,
                payload={"config": config, "session_id": session_id},
            )
        ]

    if event_type == MEDIA_EVENT_PING:
        return [frame(type="ping", session_id=session_id, user_id=user_id)]

    return [_media_transcript_final_frame(event, payload, session_id=session_id, user_id=user_id)]


def _media_session_start_frame(
    event: dict[str, Any],
    payload: dict[str, Any],
    *,
    session_id: str | None,
    user_id: str | None,
) -> Frame:
    call_id = _optional_string(event.get("call_id") or payload.get("call_id"))
    config = _config_from_media_event(event, payload, required=False)
    if call_id:
        config.setdefault("call_id", call_id)
    media_config = _media_technical_config(event, payload)
    if media_config:
        config.setdefault("media", media_config)
    return frame(
        type=CALL_INCOMING,
        session_id=session_id,
        user_id=user_id,
        payload={
            "config": config,
            "session_id": session_id,
            "call_id": call_id,
        },
    )


def _media_transcript_final_frame(
    event: dict[str, Any],
    payload: dict[str, Any],
    *,
    session_id: str | None,
    user_id: str | None,
) -> Frame:
    if event.get("final") is False or payload.get("final") is False:
        raise MediaEventProtocolError(
            "invalid_media_event",
            "transcript.final cannot set final=false",
        )

    text = _optional_string(event.get("text") or payload.get("text"))
    video_ids = _ids_from_event(event, payload, names=("video_ids", "video_id", "video_ref"))
    image_ids = _ids_from_event(event, payload, names=("image_ids", "image_id", "image_ref"))
    audio_id = _optional_string(event.get("audio_id") or payload.get("audio_id"))
    if not text and not video_ids and not image_ids and not audio_id:
        raise MediaEventProtocolError(
            "invalid_media_event",
            "transcript.final requires text, audio_id, video_ids, or image_ids",
        )

    media_payload: dict[str, Any] = {
        "text": text
        or _default_media_text(video_ids=video_ids, image_ids=image_ids, audio_id=audio_id),
        "modality": "text",
        "image_ids": image_ids,
        "video_ids": video_ids,
    }
    if audio_id:
        media_payload["audio_id"] = audio_id

    media_metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    metadata = dict(media_metadata)
    technical = _media_technical_config(event, payload)
    if technical:
        metadata["media"] = technical
    if metadata:
        media_payload["metadata"] = metadata
    return frame(
        type="message.user",
        session_id=session_id,
        user_id=user_id,
        payload=media_payload,
    )


def _media_session_end_payload(event: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    reason = _optional_string(event.get("reason") or payload.get("reason"))
    call_id = _optional_string(event.get("call_id") or payload.get("call_id"))
    result: dict[str, Any] = {}
    if reason:
        result["reason"] = reason
    if call_id:
        result["call_id"] = call_id
    return result


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
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise MediaEventProtocolError(
            "invalid_media_event",
            "media event payload must be an object when provided",
        )
    return dict(payload)


def _config_from_media_event(
    event: dict[str, Any],
    payload: dict[str, Any],
    *,
    required: bool,
) -> dict[str, Any]:
    raw_config = payload.get("config") if "config" in payload else event.get("config")
    if raw_config is None:
        if required:
            raise MediaEventProtocolError(
                "invalid_media_event",
                "config.update requires a config object",
            )
        return {}
    if not isinstance(raw_config, dict):
        raise MediaEventProtocolError(
            "invalid_media_event",
            "media config must be an object",
        )
    config = _normalize_media_config(raw_config)
    if required and not config:
        raise MediaEventProtocolError(
            "invalid_media_event",
            "config.update requires at least one config field",
        )
    return config


def _normalize_media_config(raw_config: dict[str, Any]) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for raw_key, raw_value in raw_config.items():
        key = str(raw_key).strip()
        if not key:
            raise MediaEventProtocolError(
                "invalid_media_event",
                "media config keys must be non-empty strings",
            )
        if key in MEDIA_CONFIG_STRING_KEYS:
            value = _optional_string(raw_value)
            if value is None:
                raise MediaEventProtocolError(
                    "invalid_media_event",
                    f"media config field {key} must be a non-empty string",
                    detail={"field": key},
                )
            config[key] = value
        elif key in MEDIA_CONFIG_BOOL_KEYS:
            if not isinstance(raw_value, bool):
                raise MediaEventProtocolError(
                    "invalid_media_event",
                    f"media config field {key} must be a boolean",
                    detail={"field": key},
                )
            config[key] = raw_value
        elif key in MEDIA_CONFIG_INT_KEYS:
            config[key] = _non_negative_int_config(raw_value, field=key)
        elif key in MEDIA_CONFIG_DICT_KEYS:
            if not isinstance(raw_value, dict):
                raise MediaEventProtocolError(
                    "invalid_media_event",
                    f"media config field {key} must be an object",
                    detail={"field": key},
                )
            config[key] = dict(raw_value)
        else:
            raise MediaEventProtocolError(
                "invalid_media_event",
                f"unsupported media config field: {key}",
                detail={
                    "field": key,
                    "supported_fields": sorted(
                        MEDIA_CONFIG_STRING_KEYS
                        | MEDIA_CONFIG_BOOL_KEYS
                        | MEDIA_CONFIG_INT_KEYS
                        | MEDIA_CONFIG_DICT_KEYS
                    ),
                },
            )
    return config


def _non_negative_int_config(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        parsed = None
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, float) and value.is_integer():
        parsed = int(value)
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            parsed = None
    else:
        parsed = None
    if parsed is None or parsed < 0:
        raise MediaEventProtocolError(
            "invalid_media_event",
            f"media config field {field} must be a non-negative integer",
            detail={"field": field},
        )
    return parsed


def _media_technical_config(event: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    media: dict[str, Any] = {}
    for source in (event, payload):
        raw_media = source.get("media")
        if isinstance(raw_media, dict):
            media.update(raw_media)
    for key in (
        "codec",
        "sample_rate_hz",
        "track_id",
        "relay_id",
        "stt_provider",
        "audio_id",
    ):
        value = event.get(key) if key in event else payload.get(key)
        if value is not None:
            media[key] = value
    return media


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
        values: list[str] = []
        for item in value:
            values.extend(_string_list(item))
        return values
    text = str(value).strip()
    if not text:
        return []
    return [item.strip() for item in text.replace("\n", ",").split(",") if item.strip()]


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


def _error_frame(
    code: str,
    message: str,
    *,
    user_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> Frame:
    return frame(
        type="error",
        user_id=user_id,
        error={"code": code, "message": message, "recoverable": True, "detail": detail or {}},
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
