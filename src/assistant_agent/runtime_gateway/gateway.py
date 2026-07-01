"""Transport-agnostic client gateway for assistant runtime streams."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Optional

from assistant_agent.runtime_gateway.protocol import (
    CALL_HANGUP,
    CALL_HANGUP_ACK,
    CALL_INCOMING,
    CONFIG_UPDATE,
    SUPPORTED_MODALITIES,
    Frame,
    frame,
)
from assistant_agent.runtime_gateway.transport import Endpoint


def _forward_event_to_external_client() -> bool:
    return (os.environ.get("GATEWAY_FORWARD_EVENT_TOOL") or "0").strip() == "1"


def _should_send_runtime_frame_to_client(f: dict[str, Any]) -> bool:
    frame_type = f.get("type", "")
    if frame_type.startswith("_"):
        return False
    if frame_type in {"event.skill", "event.tool"} and not _forward_event_to_external_client():
        return False
    if frame_type == "error":
        err = f.get("error") or {}
        if err.get("code") == "run_not_found":
            return False
    return True


@dataclass
class ClientConn:
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    active_run_id: Optional[str] = None
    _cancel_event: Optional[asyncio.Event] = None


class GatewayService:
    """Bridge external client frames to a runtime endpoint."""

    def __init__(self) -> None:
        self._clients: dict[str, ClientConn] = {}
        self._lock = asyncio.Lock()

    async def bridge(
        self,
        *,
        client_id: str,
        client_ep: Endpoint,
        runtime_ep: Endpoint,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> None:
        cancel_event = asyncio.Event()
        async with self._lock:
            if user_id:
                for cid, conn in list(self._clients.items()):
                    if conn.user_id == user_id and cid != client_id and conn._cancel_event:
                        conn._cancel_event.set()
            self._clients[client_id] = ClientConn(
                user_id=user_id,
                session_id=session_id,
                _cancel_event=cancel_event,
            )
        if user_id:
            try:
                runtime_ep._inject(frame(type="_evict", user_id=user_id))  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - transport may not support injection.
                pass

        async def _client_to_runtime() -> None:
            async for incoming in client_ep:
                await self._handle_client_frame(
                    client_id=client_id,
                    client_ep=client_ep,
                    runtime_ep=runtime_ep,
                    incoming=incoming,
                    user_id=user_id,
                )

        async def _runtime_to_client() -> None:
            async for outbound in runtime_ep:
                if cancel_event.is_set():
                    return
                await self._handle_runtime_frame(
                    client_id=client_id,
                    client_ep=client_ep,
                    outbound=outbound,
                    user_id=user_id,
                )

        t1 = asyncio.create_task(_client_to_runtime())
        t2 = asyncio.create_task(_runtime_to_client())

        try:
            _, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()

            async with self._lock:
                conn = self._clients.get(client_id)
                run_id = conn.active_run_id if conn else None
                sid = conn.session_id if conn else None
                self._clients.pop(client_id, None)

            if not cancel_event.is_set() and (run_id or sid):
                await runtime_ep.send(
                    frame(type="run.cancel", run_id=run_id, session_id=sid, user_id=user_id)
                )
        finally:
            t1.cancel()
            t2.cancel()

    async def _handle_client_frame(
        self,
        *,
        client_id: str,
        client_ep: Endpoint,
        runtime_ep: Endpoint,
        incoming: Frame,
        user_id: Optional[str],
    ) -> None:
        frame_type = incoming.get("type")
        uid = incoming.get("user_id") or user_id

        if frame_type == CALL_INCOMING:
            return

        if frame_type == CALL_HANGUP:
            await client_ep.send(frame(type=CALL_HANGUP_ACK, user_id=uid))
            return

        if frame_type == CONFIG_UPDATE:
            return

        if frame_type in {"session.open", "session.resume"}:
            sid = (incoming.get("payload") or {}).get("session_id") or incoming.get("session_id")
            async with self._lock:
                self._clients[client_id].session_id = sid
            await runtime_ep.send(
                frame(type=frame_type, session_id=sid, user_id=uid, payload=incoming.get("payload"))
            )
            return

        if frame_type == "message.user":
            payload = incoming.get("payload") or {}
            modality = payload.get("modality") or "text"
            if modality not in SUPPORTED_MODALITIES:
                await client_ep.send(
                    frame(
                        type="error",
                        user_id=uid,
                        error={
                            "code": "unsupported_modality",
                            "message": f"{modality} modality not yet supported",
                        },
                    )
                )
                return

            enriched = dict(incoming)
            if uid:
                enriched["user_id"] = uid
            await runtime_ep.send(enriched)  # type: ignore[arg-type]
            sid = incoming.get("session_id") or payload.get("session_id")
            async with self._lock:
                conn = self._clients.get(client_id)
                if conn:
                    conn.session_id = sid or conn.session_id
            return

        if frame_type == "run.cancel":
            await runtime_ep.send(incoming)
            return

        if frame_type == "ping":
            await client_ep.send(frame(type="pong", user_id=uid))
            return

        await client_ep.send(
            frame(
                type="error",
                user_id=uid,
                error={"code": "unknown_frame", "message": f"unknown type: {frame_type}"},
            )
        )

    async def _handle_runtime_frame(
        self,
        *,
        client_id: str,
        client_ep: Endpoint,
        outbound: Frame,
        user_id: Optional[str],
    ) -> None:
        if outbound.get("type") == "run.started":
            async with self._lock:
                conn = self._clients.get(client_id)
                if conn:
                    conn.active_run_id = outbound.get("run_id")
                    if outbound.get("session_id"):
                        conn.session_id = outbound.get("session_id")

        if outbound.get("type") == "run.end":
            async with self._lock:
                conn = self._clients.get(client_id)
                if conn and conn.active_run_id == outbound.get("run_id"):
                    conn.active_run_id = None

        if not _should_send_runtime_frame_to_client(outbound):
            return

        if user_id and not outbound.get("user_id"):
            enriched = dict(outbound)
            enriched["user_id"] = user_id
            await client_ep.send(enriched)  # type: ignore[arg-type]
        else:
            await client_ep.send(outbound)

        if outbound.get("type") == "stream.chunk":
            await asyncio.sleep(0.01)
