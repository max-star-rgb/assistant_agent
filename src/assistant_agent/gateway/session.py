"""Gateway session service and per-user session manager."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from assistant_agent.gateway.event_mapping import realtime_event_to_frame
from assistant_agent.gateway.observability import (
    GatewayLifecycleSink,
    emit_gateway_lifecycle_event,
)
from assistant_agent.gateway.protocol import Frame, frame
from assistant_agent.gateway.transport import Endpoint, InMemoryDuplex
from assistant_agent.realtime import (
    GatewayAgentAdapter,
    RealtimeAgentBackend,
    RealtimeAgentEvent,
    RealtimeAgentRequest,
    RealtimeAgentResult,
)
from assistant_agent.realtime.delivery import progress_replacement_key
from assistant_agent.schemas.realtime_cancellation import (
    build_realtime_turn_cancellation_metadata,
    realtime_turn_cancellation_from_metadata,
)
from assistant_agent.services.provider_errors import sanitize_error_message


@dataclass
class ActiveRun:
    run_id: str
    turn_id: str
    cancel: "CancelToken"
    task: "asyncio.Task[None]"
    deadline_task: "asyncio.Task[None] | None" = None


@dataclass
class PendingUserMessage:
    endpoint: Endpoint
    frame: Frame


class CancelToken:
    """Cooperative cancellation token passed into realtime backends."""

    def __init__(self) -> None:
        self._evt = asyncio.Event()
        self._metadata: dict[str, Any] = {}

    def cancel(
        self,
        *,
        source: str = "gateway_cancel",
        reason: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if self._evt.is_set():
            return
        cancel_metadata = dict(metadata or {})
        cancel_metadata["cancel_source"] = source
        if reason is not None:
            cancel_metadata["cancel_reason"] = reason
        self._metadata = build_realtime_turn_cancellation_metadata(
            cancel_metadata,
            phase="final_streaming",
        )
        self._evt.set()

    async def cancelled(self) -> None:
        await self._evt.wait()

    def is_cancelled(self) -> bool:
        return self._evt.is_set()

    @property
    def cancel_metadata(self) -> dict[str, Any]:
        return dict(self._metadata)

    @property
    def metadata(self) -> dict[str, Any]:
        return self.cancel_metadata


class GatewaySessionService:
    """Gateway-managed session side of the Gateway<->agent stream.

    This service owns session history, active run lifecycle, cooperative
    cancellation, and event mapping. Agent execution is delegated to a
    RealtimeAgentBackend; by default this remains GatewayAgentAdapter.
    """

    def __init__(
        self,
        *,
        user_id: str = "default",
        backend: RealtimeAgentBackend | None = None,
        backend_factory: Callable[[], RealtimeAgentBackend] | None = None,
        config: Mapping[str, Any] | None = None,
        lifecycle_sink: GatewayLifecycleSink | None = None,
    ) -> None:
        self._user_id = user_id
        self._backend = backend
        self._backend_factory = backend_factory
        self._config: dict[str, Any] = dict(config or {})
        self._lifecycle_sink = lifecycle_sink
        self._active_by_session: dict[str, ActiveRun] = {}
        self._pending_by_session: dict[str, list[PendingUserMessage]] = {}
        self._history_by_session: dict[str, list[str]] = {}
        self._lock = asyncio.Lock()

    @property
    def config(self) -> dict[str, Any]:
        return dict(self._config)

    def update_config(self, values: Mapping[str, Any]) -> None:
        self._config.update({str(key): value for key, value in values.items()})

    def _emit_lifecycle(
        self,
        event_type: str,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        turn_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        emit_gateway_lifecycle_event(
            self._lifecycle_sink,
            type=event_type,
            user_id=self._user_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            payload=payload,
        )

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

        interrupt_requested = _message_requests_interrupt(payload, self._config)
        async with self._lock:
            has_active_run = session_id in self._active_by_session
            if has_active_run and not interrupt_requested:
                pending = self._pending_by_session.setdefault(session_id, [])
                pending.append(PendingUserMessage(endpoint=ep, frame=dict(f)))
                self._emit_lifecycle(
                    "gateway.run.queued",
                    session_id=session_id,
                    payload={"queue_depth": len(pending)},
                )
                return

        if interrupt_requested:
            await self._interrupt_if_needed(session_id=session_id)

        await self._start_user_message(
            ep=ep,
            session_id=session_id,
            payload=payload,
            user_text=user_text,
            user_id=user_id,
            runtime_interrupt=interrupt_requested and has_active_run,
        )

    async def _start_user_message(
        self,
        *,
        ep: Endpoint,
        session_id: str,
        payload: dict[str, Any],
        user_text: str,
        user_id: str,
        runtime_interrupt: bool = False,
    ) -> None:
        turn_id = str(payload.get("turn_id") or uuid.uuid4())
        run_id = str(payload.get("run_id") or uuid.uuid4())

        async with self._lock:
            hist = self._history_by_session.setdefault(session_id, [])
            hist.append(user_text)
            history_snapshot = list(hist)

        cancel = CancelToken()
        registered = asyncio.Event()
        deadline_ms = _run_timeout_ms(payload, self._config)

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
                runtime_interrupt=runtime_interrupt,
                cancel=cancel,
            )

        task = asyncio.create_task(_runner())
        deadline_task = self._start_deadline_monitor(
            session_id=session_id,
            run_id=run_id,
            cancel=cancel,
            deadline_ms=deadline_ms,
        )
        async with self._lock:
            self._active_by_session[session_id] = ActiveRun(
                run_id=run_id,
                turn_id=turn_id,
                cancel=cancel,
                task=task,
                deadline_task=deadline_task,
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
        runtime_interrupt: bool,
        cancel: CancelToken,
    ) -> None:
        expects_reply = True
        end_reason = "error"
        try:
            self._emit_lifecycle(
                "gateway.run.started",
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
            )
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
                runtime_interrupt=runtime_interrupt,
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
                        payload=_run_end_payload(
                            result=result,
                            expects_reply=True,
                            run_id=run_id,
                        ),
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
                        payload=_run_end_payload(
                            result=result,
                            expects_reply=expects_reply,
                            run_id=run_id,
                        ),
                    )
                )
        except Exception as exc:  # noqa: BLE001 - protocol boundary.
            if cancel.is_cancelled():
                end_reason = "cancelled"
                await ep.send(
                    frame(
                        type="run.end",
                        session_id=session_id,
                        turn_id=turn_id,
                        run_id=run_id,
                        reason="cancelled",
                        payload=_run_end_payload(
                            result=RealtimeAgentResult(
                                status="cancelled",
                                run_id=run_id,
                                expects_reply=True,
                                metadata={
                                    **cancel.cancel_metadata,
                                    "cancel_phase": "gateway_exception",
                                    "best_effort": True,
                                },
                            ),
                            expects_reply=True,
                            run_id=run_id,
                        ),
                    )
                )
            else:
                end_reason = "error"
                await ep.send(
                    frame(
                        type="run.end",
                        session_id=session_id,
                        turn_id=turn_id,
                        run_id=run_id,
                        reason="error",
                        error={"message": str(exc), "error_type": type(exc).__name__},
                        payload={
                            "expects_reply": True,
                            "supersedes": [progress_replacement_key(run_id)],
                        },
                    )
                )
        finally:
            deadline_task: asyncio.Task[None] | None = None
            pending: PendingUserMessage | None = None
            async with self._lock:
                cur = self._active_by_session.get(session_id)
                if cur and cur.run_id == run_id:
                    deadline_task = cur.deadline_task
                    self._active_by_session.pop(session_id, None)
                    queued = self._pending_by_session.get(session_id)
                    if queued:
                        pending = queued.pop(0)
                        if not queued:
                            self._pending_by_session.pop(session_id, None)
            if deadline_task is not None:
                deadline_task.cancel()
                await asyncio.gather(deadline_task, return_exceptions=True)
            self._emit_lifecycle(
                _terminal_lifecycle_event_type(end_reason),
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                payload={"reason": end_reason, "expects_reply": expects_reply},
            )
            if pending is not None:
                await self._handle_user_message(pending.endpoint, pending.frame)

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
            if cancel.is_cancelled():
                return
            mapped = realtime_event_to_frame(
                event,
                session_id=request.session_id,
                turn_id=turn_id,
                run_id=request.run_id or "",
            )
            if mapped is not None and not cancel.is_cancelled():
                await queue.put(mapped)

        task = asyncio.create_task(
            self._resolve_backend().run_turn(
                request,
                event_sink=event_sink,
                cancel_token=cancel,
            )
        )
        cancel_wait = asyncio.create_task(cancel.cancelled())
        queue_wait: asyncio.Task[Frame] | None = None

        try:
            while True:
                if cancel.is_cancelled():
                    _discard_queued_frames(queue)
                    _consume_background_task(task)
                    return _cancelled_realtime_result(request=request, cancel=cancel)

                while not queue.empty():
                    outbound = queue.get_nowait()
                    if cancel.is_cancelled():
                        _discard_queued_frames(queue)
                        _consume_background_task(task)
                        return _cancelled_realtime_result(request=request, cancel=cancel)
                    await ep.send(outbound)

                if task.done():
                    return await task

                if queue_wait is None or queue_wait.done():
                    queue_wait = asyncio.create_task(queue.get())

                done, _ = await asyncio.wait(
                    {task, cancel_wait, queue_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if cancel_wait in done or cancel.is_cancelled():
                    _discard_queued_frames(queue)
                    _consume_background_task(task)
                    return _cancelled_realtime_result(request=request, cancel=cancel)

                if queue_wait in done:
                    outbound = queue_wait.result()
                    queue_wait = None
                    if not cancel.is_cancelled():
                        await ep.send(outbound)
        finally:
            cancel_wait.cancel()
            pending: list[asyncio.Task[Any]] = [cancel_wait]
            if queue_wait is not None and not queue_wait.done():
                queue_wait.cancel()
                pending.append(queue_wait)
            await asyncio.gather(*pending, return_exceptions=True)

    async def _interrupt_if_needed(self, *, session_id: str) -> None:
        interrupted: ActiveRun | None = None
        async with self._lock:
            cur = self._active_by_session.get(session_id)
            if not cur:
                return
            cur.cancel.cancel(source="gateway_interrupt")
            interrupted = cur
        if interrupted is None:
            return
        self._emit_lifecycle(
            "gateway.run.cancel_requested",
            session_id=session_id,
            run_id=interrupted.run_id,
            turn_id=interrupted.turn_id,
            payload={"source": "gateway_interrupt"},
        )

    async def _handle_cancel(self, ep: Endpoint, f: Frame) -> None:
        run_id = f.get("run_id")
        session_id = f.get("session_id")
        payload = _payload_dict(f)
        cancel_source = _cancel_source_from_payload(payload)
        cancel_reason = _optional_string(payload.get("reason"))
        did_cancel = False
        cancelled_session_id: str | None = None
        cancelled_run_id: str | None = None
        cancelled_turn_id: str | None = None

        async with self._lock:
            if session_id and cancel_source in {"gateway_disconnect", "gateway_hangup"}:
                self._pending_by_session.pop(str(session_id), None)
            if session_id:
                cur = self._active_by_session.get(session_id)
                if cur and (run_id is None or cur.run_id == run_id):
                    cur.cancel.cancel(source=cancel_source, reason=cancel_reason)
                    did_cancel = True
                    cancelled_session_id = str(session_id)
                    cancelled_run_id = cur.run_id
                    cancelled_turn_id = cur.turn_id
            elif run_id:
                for active_session_id, cur in self._active_by_session.items():
                    if cur.run_id == run_id:
                        cur.cancel.cancel(source=cancel_source, reason=cancel_reason)
                        did_cancel = True
                        cancelled_session_id = active_session_id
                        cancelled_run_id = cur.run_id
                        cancelled_turn_id = cur.turn_id
                        break

        if did_cancel:
            cancel_payload: dict[str, Any] = {"source": cancel_source}
            if cancel_reason:
                cancel_payload["reason"] = cancel_reason
            self._emit_lifecycle(
                "gateway.run.cancel_requested",
                session_id=cancelled_session_id,
                run_id=cancelled_run_id,
                turn_id=cancelled_turn_id,
                payload=cancel_payload,
            )
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
            self._backend = GatewayAgentAdapter()
        return self._backend

    def _start_deadline_monitor(
        self,
        *,
        session_id: str,
        run_id: str,
        cancel: CancelToken,
        deadline_ms: int | None,
    ) -> asyncio.Task[None] | None:
        if deadline_ms is None:
            return None

        async def _monitor() -> None:
            await asyncio.sleep(deadline_ms / 1000)
            cancelled_run: ActiveRun | None = None
            async with self._lock:
                cur = self._active_by_session.get(session_id)
                if cur is None or cur.run_id != run_id or cur.cancel is not cancel:
                    return
                cur.cancel.cancel(
                    source="deadline",
                    reason="run_deadline_expired",
                    metadata={"deadline_ms": deadline_ms},
                )
                cancelled_run = cur
            if cancelled_run is None:
                return
            self._emit_lifecycle(
                "gateway.run.cancel_requested",
                session_id=session_id,
                run_id=cancelled_run.run_id,
                turn_id=cancelled_run.turn_id,
                payload={
                    "source": "deadline",
                    "reason": "run_deadline_expired",
                    "deadline_ms": deadline_ms,
                },
            )

        return asyncio.create_task(_monitor(), name=f"gateway-run-deadline-{run_id}")

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
        runtime_interrupt: bool = False,
    ) -> RealtimeAgentRequest:
        metadata = _user_message_metadata(payload)
        if runtime_interrupt:
            metadata.setdefault("control", "interrupt")
        gateway_metadata = metadata.get("gateway")
        gateway_payload = dict(gateway_metadata) if isinstance(gateway_metadata, dict) else {}
        if metadata.get("control") == "interrupt":
            gateway_payload.setdefault("control", "interrupt")
            gateway_payload.setdefault("interrupt", True)
        gateway_payload["history"] = list(history)
        gateway_payload["session_config"] = dict(self._config)
        _apply_trusted_system_prompt_config(metadata, self._config)
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


def _cancelled_realtime_result(
    *,
    request: RealtimeAgentRequest,
    cancel: CancelToken,
) -> RealtimeAgentResult:
    return RealtimeAgentResult(
        status="cancelled",
        run_id=request.run_id,
        expects_reply=True,
        metadata={
            **cancel.cancel_metadata,
            "cancel_phase": "gateway_output_gate",
            "best_effort": True,
        },
    )


def _run_end_payload(
    *,
    result: RealtimeAgentResult,
    expects_reply: bool,
    run_id: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "expects_reply": expects_reply,
        "supersedes": [progress_replacement_key(run_id)],
    }
    if result.trace_id:
        payload["trace_id"] = result.trace_id
    if result.status == "cancelled":
        cancel_payload = _run_end_cancel_payload(result.metadata)
        if cancel_payload:
            payload["cancel"] = cancel_payload
        if not result.trace_id:
            payload["trace"] = {
                "status": "not_available",
                "reason": "cancelled_before_backend_result",
            }
    return payload


def _run_end_cancel_payload(metadata: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    source = _prompt_safe_optional_string(metadata.get("cancel_source"))
    if source:
        payload["source"] = source
    reason = _prompt_safe_optional_string(metadata.get("cancel_reason"))
    if reason:
        payload["reason"] = reason
    phase = _prompt_safe_optional_string(metadata.get("cancel_phase"))
    if phase:
        payload["phase"] = phase
    best_effort = metadata.get("best_effort")
    if isinstance(best_effort, bool):
        payload["best_effort"] = best_effort
    deadline_ms = _positive_int(metadata.get("deadline_ms"))
    if deadline_ms is not None:
        payload["deadline_ms"] = deadline_ms
    contract = realtime_turn_cancellation_from_metadata(metadata)
    payload["cancelled_by"] = contract.cancelled_by
    payload["phase"] = contract.phase
    payload["stale_outputs"] = contract.stale_outputs
    payload["can_reuse_tool_result"] = contract.can_reuse_tool_result
    payload["speakable"] = contract.speakable
    return payload


def _prompt_safe_optional_string(value: Any) -> str | None:
    text = _optional_string(value)
    if text is None:
        return None
    return sanitize_error_message(text)


def _discard_queued_frames(queue: asyncio.Queue[Frame]) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return


def _consume_background_task(task: asyncio.Task[Any]) -> None:
    def _consume(done: asyncio.Task[Any]) -> None:
        try:
            done.result()
        except asyncio.CancelledError:
            return
        except Exception:
            return

    if task.done():
        _consume(task)
    else:
        task.add_done_callback(_consume)


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
        lifecycle_sink: GatewayLifecycleSink | None = None,
    ) -> None:
        self.max_sessions = max_sessions
        self.idle_timeout_s = idle_timeout_s
        self.hangup_grace_s = idle_timeout_s if hangup_grace_s is None else hangup_grace_s
        self.reaper_interval_s = reaper_interval_s
        self.backend_factory = backend_factory
        self.service_factory = service_factory
        self.lifecycle_sink = lifecycle_sink
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
                self._emit_lifecycle(
                    "gateway.session.acquired",
                    user_id=user_id,
                    payload={
                        "created": False,
                        "resumed": resumed,
                        "active_count": len(self._entries),
                    },
                )
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
            self._emit_lifecycle(
                "gateway.session.acquired",
                user_id=user_id,
                payload={
                    "created": True,
                    "resumed": False,
                    "active_count": len(self._entries),
                },
            )
            return GatewaySessionHandle(
                user_id=user_id,
                endpoint=entry.gateway_ep,  # type: ignore[arg-type]
                created=True,
                resumed=False,
                active_count=len(self._entries),
                config=entry.service.config,
            )

    async def mark_hangup(self, user_id: str) -> bool:
        marked = False
        async with self._lock:
            entry = self._entries.get(user_id)
            if entry is None:
                return False
            if entry.hung_up_at is None:
                entry.hung_up_at = time.monotonic()
                marked = True
        self._emit_lifecycle(
            "gateway.session.hangup_marked",
            user_id=user_id,
            payload={"newly_marked": marked},
        )
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
        self._emit_lifecycle(
            "gateway.session.destroyed",
            user_id=user_id,
            payload={"active_count": self.active_count()},
        )
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

    def has_active_session(self, user_id: str) -> bool:
        return user_id in self._entries

    def _emit_lifecycle(
        self,
        event_type: str,
        *,
        user_id: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        emit_gateway_lifecycle_event(
            self.lifecycle_sink,
            type=event_type,
            user_id=user_id,
            payload=payload,
        )

    async def close(self) -> None:
        """Stop the reaper and close all managed user sessions."""

        if self._reaper_task is not None and not self._reaper_task.done():
            self._reaper_task.cancel()
            await asyncio.gather(self._reaper_task, return_exceptions=True)
        self._reaper_task = None
        async with self._lock:
            user_ids = list(self._entries)
        for user_id in user_ids:
            await self.destroy(user_id)

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
                lifecycle_sink=self.lifecycle_sink,
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


def _terminal_lifecycle_event_type(reason: str) -> str:
    if reason == "completed":
        return "gateway.run.completed"
    if reason == "cancelled":
        return "gateway.run.cancelled"
    return "gateway.run.errored"


def _run_timeout_ms(payload: Mapping[str, Any], session_config: Mapping[str, Any]) -> int | None:
    metadata = payload.get("metadata")
    gateway_metadata = metadata.get("gateway") if isinstance(metadata, Mapping) else None
    if isinstance(gateway_metadata, Mapping) and "run_timeout_ms" in gateway_metadata:
        return _positive_int(gateway_metadata.get("run_timeout_ms"))
    return _positive_int(session_config.get("run_timeout_ms"))


def _user_message_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    metadata = dict(payload.get("metadata") or {})
    trusted_source = _trusted_entry_source(metadata)
    for key in ("system_prompt_profile", "channel", "source"):
        metadata.pop(key, None)
    if trusted_source is not None:
        metadata["source"] = trusted_source
    return metadata


def _trusted_entry_source(metadata: Mapping[str, Any]) -> str | None:
    source = _optional_string(metadata.get("source"))
    if source not in {"gateway_websocket", "realtime_media_websocket"}:
        return None
    if metadata.get("transport") != "websocket":
        return None
    return source if isinstance(metadata.get("request_identity"), Mapping) else None


def _apply_trusted_system_prompt_config(metadata: dict[str, Any], session_config: Mapping[str, Any]) -> None:
    profile = _optional_string(session_config.get("system_prompt_profile"))
    if profile in {"text_default", "realtime_phone"}:
        metadata["system_prompt_profile"] = profile
    channel = _optional_string(session_config.get("channel"))
    if channel in {"text", "phone", "realtime_phone"}:
        metadata["channel"] = channel


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        parsed = int(value) if value.is_integer() else None
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
    else:
        return None
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _cancel_source_from_payload(payload: Mapping[str, Any]) -> str:
    source = payload.get("source")
    if source in {"gateway_cancel", "gateway_interrupt", "gateway_hangup", "gateway_disconnect"}:
        return str(source)
    return "gateway_cancel"


def _message_requests_interrupt(payload: Mapping[str, Any], session_config: Mapping[str, Any]) -> bool:
    if payload.get("interrupt") is True:
        return True
    control = _metadata_control(payload)
    if control in {"interrupt", "barge_in", "cancel_previous"}:
        return True
    policy = _optional_string(session_config.get("interrupt_policy"))
    return policy in {"interrupt", "barge_in", "cancel_previous"}


def _metadata_control(payload: Mapping[str, Any]) -> str | None:
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        return _optional_string(metadata.get("control"))
    return None


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
