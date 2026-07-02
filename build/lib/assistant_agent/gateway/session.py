"""Gateway session service and per-user session manager."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from assistant_agent.gateway.event_mapping import realtime_event_to_frame
from assistant_agent.gateway.protocol import Frame, frame
from assistant_agent.gateway.transport import Endpoint, InMemoryDuplex
from assistant_agent.realtime import (
    AgentGraphRealtimeBackend,
    RealtimeAgentBackend,
    RealtimeAgentEvent,
    RealtimeAgentRequest,
    RealtimeAgentResult,
)


@dataclass
class ActiveRun:
    run_id: str
    turn_id: str
    cancel: "CancelToken"
    task: "asyncio.Task[None]"


class CancelToken:
    """Cooperative cancellation token passed into realtime backends."""

    def __init__(self) -> None:
        self._evt = asyncio.Event()

    def cancel(self) -> None:
        self._evt.set()

    async def cancelled(self) -> None:
        await self._evt.wait()

    def is_cancelled(self) -> bool:
        return self._evt.is_set()


class GatewaySessionService:
    """Gateway-managed session side of the Gateway<->agent stream.

    This service owns session history, active run lifecycle, cooperative
    cancellation, and event mapping. Agent execution is delegated to a
    RealtimeAgentBackend; by default this remains AgentGraphRealtimeBackend.
    """

    def __init__(
        self,
        *,
        user_id: str = "default",
        backend: RealtimeAgentBackend | None = None,
        backend_factory: Callable[[], RealtimeAgentBackend] | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        self._user_id = user_id
        self._backend = backend
        self._backend_factory = backend_factory
        self._config: dict[str, Any] = dict(config or {})
        self._active_by_session: dict[str, ActiveRun] = {}
        self._history_by_session: dict[str, list[str]] = {}
        self._lock = asyncio.Lock()

    @property
    def config(self) -> dict[str, Any]:
        return dict(self._config)

    def update_config(self, values: Mapping[str, Any]) -> None:
        self._config.update({str(key): value for key, value in values.items()})

    async def serve(self, ep: Endpoint) -> None:
        async for f in ep:
            frame_type = f.get("type")
            if frame_type == "message.user":
                await self._handle_user_message(ep, f)
            elif frame_type == "run.cancel":
                await self._handle_cancel(ep, f)
            elif frame_type == "ping":
                await ep.send(frame(type="pong"))
            else:
                await ep.send(
                    frame(
                        type="error",
                        error={"code": "unknown_frame", "message": f"unknown type: {frame_type}"},
                    )
                )

    async def _handle_user_message(self, ep: Endpoint, f: Frame) -> None:
        session_id = f.get("session_id")
        payload = _payload_dict(f)
        user_text = str(payload.get("text", ""))
        user_id = str(f.get("user_id") or self._user_id)

        if not session_id:
            await ep.send(frame(type="error", error={"code": "missing_session_id"}))
            return

        turn_id = str(payload.get("turn_id") or uuid.uuid4())
        run_id = str(payload.get("run_id") or uuid.uuid4())

        await self._interrupt_if_needed(session_id=session_id)

        async with self._lock:
            hist = self._history_by_session.setdefault(session_id, [])
            hist.append(user_text)
            history_snapshot = list(hist)

        cancel = CancelToken()
        registered = asyncio.Event()

        async def _runner() -> None:
            await registered.wait()
            await self._run_backend_turn(
                ep=ep,
                session_id=session_id,
                turn_id=turn_id,
                run_id=run_id,
                user_id=user_id,
                user_text=user_text,
                history=history_snapshot,
                payload=payload,
                cancel=cancel,
            )

        task = asyncio.create_task(_runner())
        async with self._lock:
            self._active_by_session[session_id] = ActiveRun(
                run_id=run_id,
                turn_id=turn_id,
                cancel=cancel,
                task=task,
            )
        registered.set()

    async def _run_backend_turn(
        self,
        *,
        ep: Endpoint,
        session_id: str,
        turn_id: str,
        run_id: str,
        user_id: str,
        user_text: str,
        history: list[str],
        payload: dict[str, Any],
        cancel: CancelToken,
    ) -> None:
        expects_reply = True
        end_reason = "error"
        try:
            await ep.send(
                frame(type="run.started", session_id=session_id, turn_id=turn_id, run_id=run_id)
            )
            request = self._build_request(
                user_id=user_id,
                session_id=session_id,
                turn_id=turn_id,
                run_id=run_id,
                user_text=user_text,
                history=history,
                payload=payload,
            )
            result = await self._run_backend(request, ep=ep, turn_id=turn_id, cancel=cancel)
            expects_reply = bool(result.expects_reply)

            if cancel.is_cancelled() or result.status == "cancelled":
                end_reason = "cancelled"
                expects_reply = True
            elif result.status == "error":
                end_reason = "error"
            else:
                end_reason = "completed"

            if end_reason == "error":
                await ep.send(
                    frame(
                        type="run.end",
                        session_id=session_id,
                        turn_id=turn_id,
                        run_id=run_id,
                        reason="error",
                        error=_result_error(result),
                        payload={"expects_reply": True},
                    )
                )
            else:
                await ep.send(
                    frame(
                        type="run.end",
                        session_id=session_id,
                        turn_id=turn_id,
                        run_id=run_id,
                        reason=end_reason,
                        payload={"expects_reply": expects_reply},
                    )
                )
        except Exception as exc:  # noqa: BLE001 - protocol boundary.
            end_reason = "error"
            await ep.send(
                frame(
                    type="run.end",
                    session_id=session_id,
                    turn_id=turn_id,
                    run_id=run_id,
                    reason="error",
                    error={"message": str(exc), "error_type": type(exc).__name__},
                    payload={"expects_reply": True},
                )
            )
        finally:
            async with self._lock:
                cur = self._active_by_session.get(session_id)
                if cur and cur.run_id == run_id:
                    self._active_by_session.pop(session_id, None)

    async def _run_backend(
        self,
        request: RealtimeAgentRequest,
        *,
        ep: Endpoint,
        turn_id: str,
        cancel: CancelToken,
    ) -> RealtimeAgentResult:
        queue: asyncio.Queue[Frame] = asyncio.Queue()

        async def event_sink(event: RealtimeAgentEvent) -> None:
            mapped = realtime_event_to_frame(
                event,
                session_id=request.session_id,
                turn_id=turn_id,
                run_id=request.run_id or "",
            )
            if mapped is not None:
                await queue.put(mapped)

        task = asyncio.create_task(
            self._resolve_backend().run_turn(
                request,
                event_sink=event_sink,
                cancel_token=cancel,
            )
        )
        while not task.done() or not queue.empty():
            try:
                outbound = await asyncio.wait_for(queue.get(), timeout=0.05)
            except asyncio.TimeoutError:
                continue
            if not cancel.is_cancelled():
                await ep.send(outbound)

        return await task

    async def _interrupt_if_needed(self, *, session_id: str) -> None:
        async with self._lock:
            cur = self._active_by_session.get(session_id)
            if not cur:
                return
            cur.cancel.cancel()

    async def _handle_cancel(self, ep: Endpoint, f: Frame) -> None:
        run_id = f.get("run_id")
        session_id = f.get("session_id")
        did_cancel = False

        async with self._lock:
            if session_id:
                cur = self._active_by_session.get(session_id)
                if cur and (run_id is None or cur.run_id == run_id):
                    cur.cancel.cancel()
                    did_cancel = True
            elif run_id:
                for cur in self._active_by_session.values():
                    if cur.run_id == run_id:
                        cur.cancel.cancel()
                        did_cancel = True
                        break

        if did_cancel:
            return

        await ep.send(
            frame(
                type="error",
                error={"code": "run_not_found", "run_id": run_id, "session_id": session_id},
            )
        )

    def _resolve_backend(self) -> RealtimeAgentBackend:
        if self._backend is not None:
            return self._backend
        if self._backend_factory is not None:
            self._backend = self._backend_factory()
        else:
            self._backend = AgentGraphRealtimeBackend()
        return self._backend

    def _build_request(
        self,
        *,
        user_id: str,
        session_id: str,
        turn_id: str,
        run_id: str,
        user_text: str,
        history: list[str],
        payload: dict[str, Any],
    ) -> RealtimeAgentRequest:
        metadata = dict(payload.get("metadata") or {})
        gateway_metadata = metadata.get("gateway")
        gateway_payload = dict(gateway_metadata) if isinstance(gateway_metadata, dict) else {}
        gateway_payload["history"] = list(history)
        gateway_payload["session_config"] = dict(self._config)
        metadata["gateway"] = gateway_payload
        metadata["runtime"] = gateway_payload

        return RealtimeAgentRequest(
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            text=user_text,
            image_ids=_string_list(payload.get("image_ids")),
            video_ids=_string_list(payload.get("video_ids")),
            audio_id=_optional_string(payload.get("audio_id")),
            metadata=metadata,
        )


@dataclass
class GatewaySessionHandle:
    user_id: str
    endpoint: Endpoint
    created: bool
    resumed: bool
    active_count: int
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class GatewayConfigUpdateResult:
    user_id: str
    online: bool
    config: dict[str, Any] = field(default_factory=dict)


class _TouchableEndpoint:
    """Proxy endpoint that refreshes session activity on every frame."""

    def __init__(self, inner: Endpoint, touch_fn: Callable[[], None]) -> None:
        self._inner = inner
        self._touch = touch_fn

    async def send(self, f: Frame) -> None:
        self._touch()
        await self._inner.send(f)

    def _inject(self, f: Frame) -> None:
        self._touch()
        self._inner._inject(f)

    async def close(self) -> None:
        await self._inner.close()

    async def __aiter__(self) -> AsyncIterator[Frame]:
        async for f in self._inner:
            self._touch()
            yield f


class _GatewaySessionEntry:
    def __init__(
        self,
        *,
        user_id: str,
        service: GatewaySessionService,
        gateway_ep: Endpoint,
        session_ep: Endpoint,
    ) -> None:
        self.user_id = user_id
        self.service = service
        self.gateway_ep = _TouchableEndpoint(gateway_ep, self.touch)
        self.session_ep = session_ep
        self.last_active = time.monotonic()
        self.hung_up_at: float | None = None
        self.task: asyncio.Task[None] | None = None

    def touch(self) -> None:
        self.last_active = time.monotonic()

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_active

    def hangup_seconds(self) -> float | None:
        if self.hung_up_at is None:
            return None
        return time.monotonic() - self.hung_up_at

    def start(self) -> None:
        self.task = asyncio.create_task(
            self.service.serve(self.session_ep),
            name=f"gateway-session-{self.user_id}",
        )

    def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()


class GatewaySessionManager:
    """Manage per-user GatewaySessionService instances.

    The manager keeps one in-process session service per user, reuses it across
    reconnects, marks hangups for a grace period, updates live session config,
    and evicts idle sessions.
    """

    def __init__(
        self,
        *,
        max_sessions: int = 20,
        idle_timeout_s: float = 300.0,
        hangup_grace_s: float | None = None,
        reaper_interval_s: float = 30.0,
        backend_factory: Callable[[], RealtimeAgentBackend] | None = None,
        service_factory: Callable[[str, Mapping[str, Any]], GatewaySessionService] | None = None,
        start_reaper: bool = True,
    ) -> None:
        self.max_sessions = max_sessions
        self.idle_timeout_s = idle_timeout_s
        self.hangup_grace_s = idle_timeout_s if hangup_grace_s is None else hangup_grace_s
        self.reaper_interval_s = reaper_interval_s
        self.backend_factory = backend_factory
        self.service_factory = service_factory
        self._entries: dict[str, _GatewaySessionEntry] = {}
        self._deferred_config: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None
        self._start_reaper = start_reaper

    async def get_or_create(
        self,
        user_id: str,
        config: Mapping[str, Any] | None = None,
    ) -> Endpoint:
        """Return the Gateway-side endpoint for a user session."""

        return (await self.acquire(user_id=user_id, config=config)).endpoint

    async def acquire(
        self,
        *,
        user_id: str,
        config: Mapping[str, Any] | None = None,
    ) -> GatewaySessionHandle:
        async with self._lock:
            if user_id in self._entries:
                entry = self._entries[user_id]
                if config:
                    entry.service.update_config(config)
                resumed = entry.hung_up_at is not None
                if resumed:
                    entry.hung_up_at = None
                entry.touch()
                return GatewaySessionHandle(
                    user_id=user_id,
                    endpoint=entry.gateway_ep,  # type: ignore[arg-type]
                    created=False,
                    resumed=resumed,
                    active_count=len(self._entries),
                    config=entry.service.config,
                )

            if len(self._entries) >= self.max_sessions:
                raise RuntimeError(
                    f"gateway_session_limit_reached: max {self.max_sessions} sessions already running"
                )

            merged_config = dict(self._deferred_config.pop(user_id, {}))
            if config:
                merged_config.update(dict(config))
            entry = self._new_entry(user_id=user_id, config=merged_config)
            entry.start()
            self._entries[user_id] = entry
            self._ensure_reaper()
            return GatewaySessionHandle(
                user_id=user_id,
                endpoint=entry.gateway_ep,  # type: ignore[arg-type]
                created=True,
                resumed=False,
                active_count=len(self._entries),
                config=entry.service.config,
            )

    async def mark_hangup(self, user_id: str) -> bool:
        async with self._lock:
            entry = self._entries.get(user_id)
            if entry is None:
                return False
            if entry.hung_up_at is None:
                entry.hung_up_at = time.monotonic()
            return True

    async def update_config(
        self,
        user_id: str,
        values: Mapping[str, Any],
    ) -> GatewayConfigUpdateResult:
        payload = {str(key): value for key, value in values.items()}
        async with self._lock:
            entry = self._entries.get(user_id)
            if entry is not None:
                entry.service.update_config(payload)
                return GatewayConfigUpdateResult(
                    user_id=user_id,
                    online=True,
                    config=entry.service.config,
                )
            deferred = self._deferred_config.setdefault(user_id, {})
            deferred.update(payload)
            return GatewayConfigUpdateResult(
                user_id=user_id,
                online=False,
                config=dict(deferred),
            )

    async def destroy(self, user_id: str) -> bool:
        async with self._lock:
            entry = self._entries.pop(user_id, None)
        if entry is None:
            return False
        entry.stop()
        await entry.gateway_ep.close()
        await entry.session_ep.close()
        return True

    async def reap_once(self) -> list[str]:
        evict: list[str] = []
        async with self._lock:
            for user_id, entry in self._entries.items():
                hung_s = entry.hangup_seconds()
                if hung_s is not None:
                    if hung_s >= self.hangup_grace_s and entry.idle_seconds() >= self.hangup_grace_s:
                        evict.append(user_id)
                elif entry.idle_seconds() >= self.idle_timeout_s:
                    evict.append(user_id)
        for user_id in evict:
            await self.destroy(user_id)
        return evict

    def active_count(self) -> int:
        return len(self._entries)

    def session_config(self, user_id: str) -> dict[str, Any] | None:
        entry = self._entries.get(user_id)
        if entry is not None:
            return entry.service.config
        deferred = self._deferred_config.get(user_id)
        return dict(deferred) if deferred is not None else None

    def _new_entry(self, *, user_id: str, config: Mapping[str, Any]) -> _GatewaySessionEntry:
        gateway_ep, session_ep = InMemoryDuplex.create_pair()
        if self.service_factory is not None:
            service = self.service_factory(user_id, config)
        else:
            service = GatewaySessionService(
                user_id=user_id,
                backend_factory=self.backend_factory,
                config=config,
            )
        return _GatewaySessionEntry(
            user_id=user_id,
            service=service,
            gateway_ep=gateway_ep,
            session_ep=session_ep,
        )

    def _ensure_reaper(self) -> None:
        if not self._start_reaper:
            return
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(
                self._reaper_loop(),
                name="gateway-session-reaper",
            )

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(self.reaper_interval_s)
            await self.reap_once()


def _payload_dict(f: Frame) -> dict[str, Any]:
    payload = f.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _result_error(result: RealtimeAgentResult) -> dict[str, Any]:
    metadata = dict(result.metadata)
    return {
        "message": metadata.get("error_message") or "assistant_agent backend error",
        "error_type": metadata.get("error_type"),
        "metadata": metadata,
    }
