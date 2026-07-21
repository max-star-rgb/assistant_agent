"""Transport-agnostic external Gateway bridge for assistant streams."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Optional

from assistant_agent.gateway.protocol import (
    CALL_HANGUP,
    CALL_HANGUP_ACK,
    CALL_INCOMING,
    CALL_READY,
    CONFIG_UPDATE,
    SUPPORTED_MODALITIES,
    Frame,
    frame,
)
from assistant_agent.gateway.session import GatewaySessionManager
from assistant_agent.gateway.transport import Endpoint


def _forward_event_to_external_client() -> bool:
    return (os.environ.get("GATEWAY_FORWARD_EVENT_TOOL") or "0").strip() == "1"


def _should_send_session_frame_to_client(f: dict[str, Any]) -> bool:
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
    endpoint: Endpoint
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    active_run_id: Optional[str] = None
    _cancel_event: Optional[asyncio.Event] = None
    generation: int = 0


@dataclass
class _SessionRelay:
    """Single reader for one managed session's outbound frame stream."""

    user_id: str
    endpoint: Endpoint
    task: asyncio.Task[None]
    session_id: Optional[str] = None
    active_run_id: Optional[str] = None


class GatewayBridge:
    """Bridge external client frames to a GatewaySessionService endpoint.

    The bridge owns external connection lifecycle behavior: `call.incoming`,
    `call.hangup`, `config.update`, WebSocket-style forwarding, stale bridge
    eviction, and best-effort cancel on disconnect.
    """

    def __init__(self, *, session_manager: GatewaySessionManager | None = None) -> None:
        self._clients: dict[str, ClientConn] = {}
        self._owner_by_user: dict[str, str] = {}
        self._relays_by_user: dict[str, _SessionRelay] = {}
        self._next_generation = 0
        self._lock = asyncio.Lock()
        self._session_manager = session_manager

    async def bridge(
        self,
        *,
        client_id: str,
        client_ep: Endpoint,
        runtime_ep: Endpoint | None = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> None:
        cancel_event = asyncio.Event()
        runtime_ep_ref: dict[str, Endpoint | None] = {"endpoint": runtime_ep}

        async with self._lock:
            self._next_generation += 1
            self._clients[client_id] = ClientConn(
                endpoint=client_ep,
                user_id=user_id,
                session_id=session_id,
                _cancel_event=cancel_event,
                generation=self._next_generation,
            )
            if user_id:
                self._claim_user_locked(client_id=client_id, user_id=user_id)

        if user_id and runtime_ep is not None:
            await self._ensure_session_relay(user_id=user_id, runtime_ep=runtime_ep)

        async def ensure_runtime_endpoint(
            uid: str | None,
            config: dict[str, Any] | None = None,
        ) -> Endpoint | None:
            existing_endpoint = runtime_ep_ref["endpoint"]
            target_uid = uid or user_id or "default"
            if existing_endpoint is not None:
                if not await self._claim_user(client_id=client_id, user_id=target_uid):
                    return None
                await self._ensure_session_relay(
                    user_id=target_uid,
                    runtime_ep=existing_endpoint,
                )
                return existing_endpoint
            if self._session_manager is None:
                return None
            if not await self._claim_user(client_id=client_id, user_id=target_uid):
                return None
            handle = await self._session_manager.acquire(user_id=target_uid, config=config)
            runtime_ep_ref["endpoint"] = handle.endpoint
            await self._ensure_session_relay(
                user_id=target_uid,
                runtime_ep=handle.endpoint,
            )
            return handle.endpoint

        def current_runtime_endpoint() -> Endpoint | None:
            return runtime_ep_ref["endpoint"]

        async def _client_to_session() -> None:
            async for incoming in client_ep:
                if cancel_event.is_set():
                    return
                endpoint = await self._handle_client_frame(
                    client_id=client_id,
                    client_ep=client_ep,
                    incoming=incoming,
                    user_id=user_id,
                    ensure_runtime_endpoint=ensure_runtime_endpoint,
                    current_runtime_endpoint=current_runtime_endpoint,
                )
                if cancel_event.is_set():
                    return
                if endpoint is not None:
                    runtime_ep_ref["endpoint"] = endpoint

        t1 = asyncio.create_task(_client_to_session())
        t3 = asyncio.create_task(cancel_event.wait())

        try:
            _, pending = await asyncio.wait({t1, t3}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()

            async with self._lock:
                conn = self._clients.get(client_id)
                run_id = conn.active_run_id if conn else None
                sid = conn.session_id if conn else None
                if conn and conn.user_id and self._owner_by_user.get(conn.user_id) == client_id:
                    self._owner_by_user.pop(conn.user_id, None)
                self._clients.pop(client_id, None)

            endpoint = runtime_ep_ref["endpoint"]
            if not cancel_event.is_set() and endpoint is not None and (run_id or sid):
                await endpoint.send(
                    frame(
                        type="run.cancel",
                        run_id=run_id,
                        session_id=sid,
                        user_id=user_id,
                        payload={
                            "source": "gateway_disconnect",
                            "reason": "client_disconnected",
                        },
                    )
                )
        finally:
            t1.cancel()
            t3.cancel()

    async def _ensure_session_relay(
        self,
        *,
        user_id: str,
        runtime_ep: Endpoint,
    ) -> None:
        async with self._lock:
            current = self._relays_by_user.get(user_id)
            if (
                current is not None
                and current.endpoint is runtime_ep
                and not current.task.done()
            ):
                return
            if current is not None and not current.task.done():
                current.task.cancel()
            task = asyncio.create_task(
                self._relay_session_frames(user_id=user_id, runtime_ep=runtime_ep),
                name=f"gateway-session-relay-{user_id}",
            )
            self._relays_by_user[user_id] = _SessionRelay(
                user_id=user_id,
                endpoint=runtime_ep,
                task=task,
            )

    async def _relay_session_frames(
        self,
        *,
        user_id: str,
        runtime_ep: Endpoint,
    ) -> None:
        try:
            async for outbound in runtime_ep:
                await self._deliver_session_frame(user_id=user_id, outbound=outbound)
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        finally:
            current_task = asyncio.current_task()
            async with self._lock:
                relay = self._relays_by_user.get(user_id)
                if relay is not None and relay.task is current_task:
                    self._relays_by_user.pop(user_id, None)

    async def _deliver_session_frame(
        self,
        *,
        user_id: str,
        outbound: Frame,
    ) -> None:
        async with self._lock:
            relay = self._relays_by_user.get(user_id)
            if relay is not None:
                if outbound.get("type") == "run.started":
                    relay.active_run_id = outbound.get("run_id")
                    relay.session_id = outbound.get("session_id") or relay.session_id
                elif (
                    outbound.get("type") == "run.end"
                    and relay.active_run_id == outbound.get("run_id")
                ):
                    relay.active_run_id = None

            owner_id = self._owner_by_user.get(user_id)
            conn = self._clients.get(owner_id) if owner_id else None
            if conn is None or conn._cancel_event is None or conn._cancel_event.is_set():
                return
            conn.active_run_id = relay.active_run_id if relay is not None else None
            if relay is not None and relay.session_id:
                conn.session_id = relay.session_id
            client_ep = conn.endpoint

        try:
            await self._handle_session_frame(
                client_id=owner_id,
                client_ep=client_ep,
                outbound=outbound,
                user_id=user_id,
            )
        except Exception:
            # A failed connection sink must not terminate the session-owned
            # relay; the connection receive loop still owns disconnect cleanup.
            return

    async def _claim_user(self, *, client_id: str, user_id: str) -> bool:
        async with self._lock:
            return self._claim_user_locked(client_id=client_id, user_id=user_id)

    async def _client_cancelled(self, client_id: str) -> bool:
        async with self._lock:
            conn = self._clients.get(client_id)
            return bool(conn and conn._cancel_event and conn._cancel_event.is_set())

    def _claim_user_locked(self, *, client_id: str, user_id: str) -> bool:
        conn = self._clients.get(client_id)
        if conn is None or conn._cancel_event is None:
            return False
        if conn._cancel_event.is_set():
            return False

        previous_user_id = conn.user_id
        if (
            previous_user_id
            and previous_user_id != user_id
            and self._owner_by_user.get(previous_user_id) == client_id
        ):
            self._owner_by_user.pop(previous_user_id, None)
        conn.user_id = user_id
        owner_id = self._owner_by_user.get(user_id)
        owner = self._clients.get(owner_id) if owner_id else None
        if owner_id == client_id:
            return not conn._cancel_event.is_set()
        if owner is not None and owner.generation > conn.generation:
            conn._cancel_event.set()
            return False

        self._owner_by_user[user_id] = client_id
        relay = self._relays_by_user.get(user_id)
        if relay is not None:
            conn.active_run_id = relay.active_run_id
            conn.session_id = relay.session_id or conn.session_id
        for cid, other in list(self._clients.items()):
            if cid != client_id and other.user_id == user_id and other._cancel_event:
                other._cancel_event.set()
        return not conn._cancel_event.is_set()

    async def _handle_client_frame(
        self,
        *,
        client_id: str,
        client_ep: Endpoint,
        incoming: Frame,
        user_id: Optional[str],
        ensure_runtime_endpoint,
        current_runtime_endpoint,
    ) -> Endpoint | None:
        frame_type = incoming.get("type")
        payload = incoming.get("payload") if isinstance(incoming.get("payload"), dict) else {}
        uid = incoming.get("user_id") or user_id

        if frame_type == CALL_INCOMING:
            session_id = incoming.get("session_id") or payload.get("session_id")
            endpoint = await ensure_runtime_endpoint(
                _optional_string(uid),
                _config_from_payload(payload),
            )
            if await self._client_cancelled(client_id):
                return None
            async with self._lock:
                conn = self._clients.get(client_id)
                if conn:
                    conn.user_id = _optional_string(uid) or conn.user_id
                    conn.session_id = _optional_string(session_id) or conn.session_id
            await client_ep.send(
                frame(
                    type=CALL_READY,
                    session_id=_optional_string(session_id),
                    user_id=_optional_string(uid),
                    payload={"session_managed": endpoint is not None},
                )
            )
            return endpoint

        if frame_type == CALL_HANGUP:
            session_id = incoming.get("session_id") or payload.get("session_id")
            run_id = incoming.get("run_id") or payload.get("run_id")
            async with self._lock:
                conn = self._clients.get(client_id)
                if conn is not None:
                    session_id = session_id or conn.session_id
                    run_id = run_id or conn.active_run_id
            endpoint = current_runtime_endpoint()
            if (
                endpoint is None
                and self._session_manager is not None
                and uid
                and self._session_manager.has_active_session(str(uid))
            ):
                endpoint = await ensure_runtime_endpoint(_optional_string(uid), None)
            cancel_requested = endpoint is not None and run_id is not None
            session_cancel_requested = endpoint is not None and session_id is not None
            if cancel_requested or session_cancel_requested:
                await endpoint.send(
                    frame(
                        type="run.cancel",
                        session_id=_optional_string(session_id),
                        run_id=_optional_string(run_id),
                        user_id=_optional_string(uid),
                        payload={
                            "source": "gateway_hangup",
                            "reason": "call_hangup",
                        },
                    )
                )
            await client_ep.send(
                frame(
                    type=CALL_HANGUP_ACK,
                    session_id=_optional_string(session_id),
                    user_id=_optional_string(uid),
                    payload={"cancelled_active_run": cancel_requested},
                )
            )
            if self._session_manager is not None and uid:
                await self._session_manager.mark_hangup(str(uid))
            return endpoint

        if frame_type == CONFIG_UPDATE:
            if self._session_manager is not None and uid:
                await self._session_manager.update_config(str(uid), _config_update_values(payload))
            return None

        if frame_type in {"session.open", "session.resume"}:
            endpoint = await ensure_runtime_endpoint(_optional_string(uid), None)
            if endpoint is None:
                await client_ep.send(
                    frame(type="error", user_id=_optional_string(uid), error={"code": "missing_runtime_endpoint"})
                )
                return None
            sid = payload.get("session_id") or incoming.get("session_id")
            async with self._lock:
                self._clients[client_id].session_id = _optional_string(sid)
            await endpoint.send(
                frame(
                    type=frame_type,
                    session_id=_optional_string(sid),
                    user_id=_optional_string(uid),
                    payload=incoming.get("payload"),
                )
            )
            return endpoint

        if frame_type == "message.user":
            endpoint = await ensure_runtime_endpoint(_optional_string(uid), None)
            if endpoint is None:
                await client_ep.send(
                    frame(type="error", user_id=_optional_string(uid), error={"code": "missing_runtime_endpoint"})
                )
                return None
            modality = payload.get("modality") or "text"
            if modality not in SUPPORTED_MODALITIES:
                await client_ep.send(
                    frame(
                        type="error",
                        user_id=_optional_string(uid),
                        error={
                            "code": "unsupported_modality",
                            "message": f"{modality} modality not yet supported",
                        },
                    )
                )
                return endpoint

            enriched = dict(incoming)
            if uid:
                enriched["user_id"] = str(uid)
            await endpoint.send(enriched)  # type: ignore[arg-type]
            sid = incoming.get("session_id") or payload.get("session_id")
            async with self._lock:
                conn = self._clients.get(client_id)
                if conn:
                    conn.user_id = _optional_string(uid) or conn.user_id
                    conn.session_id = _optional_string(sid) or conn.session_id
            return endpoint

        if frame_type == "run.cancel":
            endpoint = await ensure_runtime_endpoint(_optional_string(uid), None)
            if endpoint is not None:
                await endpoint.send(incoming)
            return endpoint

        if frame_type == "ping":
            await client_ep.send(frame(type="pong", user_id=_optional_string(uid)))
            return None

        await client_ep.send(
            frame(
                type="error",
                user_id=_optional_string(uid),
                error={"code": "unknown_frame", "message": f"unknown type: {frame_type}"},
            )
        )
        return None

    async def _handle_session_frame(
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

        if not _should_send_session_frame_to_client(outbound):
            return

        if user_id and not outbound.get("user_id"):
            enriched = dict(outbound)
            enriched["user_id"] = user_id
            await client_ep.send(enriched)  # type: ignore[arg-type]
        else:
            await client_ep.send(outbound)

        if outbound.get("type") == "stream.chunk":
            await asyncio.sleep(0.01)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _config_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    config = payload.get("config")
    return dict(config) if isinstance(config, dict) else {}


def _config_update_values(payload: dict[str, Any]) -> dict[str, Any]:
    config = payload.get("config")
    if isinstance(config, dict):
        return dict(config)
    key = payload.get("key")
    if key is None:
        return {}
    return {str(key): payload.get("value")}
