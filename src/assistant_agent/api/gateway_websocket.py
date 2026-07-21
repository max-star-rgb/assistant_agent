"""Normalized Gateway WebSocket entry adapter."""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from assistant_agent.api.auth import get_websocket_auth_context, require_auth_bound_identity
from assistant_agent.api.gateway_runtime import get_gateway_bridge
from assistant_agent.api.routes_agent import get_trial_access_gate
from assistant_agent.gateway import (
    GATEWAY_WEBSOCKET_CAPABILITIES,
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
from assistant_agent.services.identifiers import new_prefixed_uuid7

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
        client_id=new_prefixed_uuid7("gateway-ws", separator="-"),
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


def _message_payload_with_metadata(
    payload: Any,
    *,
    source: str,
    identity: ResolvedRequestIdentity,
) -> dict[str, Any]:
    normalized = dict(payload) if isinstance(payload, dict) else {}
    metadata = dict(normalized.get("metadata") or {})
    inbound_source = _optional_string(metadata.get("source"))
    if inbound_source and inbound_source != source:
        metadata.setdefault("source_detail", inbound_source)
    metadata["source"] = source
    metadata["transport"] = "websocket"
    metadata["request_identity"] = identity.metadata()
    if source == "gateway_websocket":
        gateway_metadata = dict(metadata.get("gateway") or {})
        gateway_metadata["entry_capabilities"] = GATEWAY_WEBSOCKET_CAPABILITIES.to_metadata()
        metadata["gateway"] = gateway_metadata
    normalized["metadata"] = metadata
    return normalized


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
