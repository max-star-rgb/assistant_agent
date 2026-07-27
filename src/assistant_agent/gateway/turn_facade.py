"""Synchronous turn facade over Gateway session lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from assistant_agent.gateway.protocol import Frame, frame
from assistant_agent.gateway.session import GatewaySessionManager
from assistant_agent.identifiers import new_run_id, new_turn_id

GatewayStreamChunkConsumer = Callable[[str, Frame], Awaitable[None]]
GatewayTurnCorrelationObserver = Callable[["GatewayTurnCorrelation"], None]


@dataclass(frozen=True)
class GatewayTurnCorrelation:
    """Prompt-safe identifiers known while a Gateway turn is still running."""

    turn_id: str
    run_id: str
    trace_id: str | None = None


class GatewayTurnError(RuntimeError):
    """Raised when a Gateway turn cannot reach a terminal frame."""

    def __init__(
        self,
        message: str,
        *,
        correlation: GatewayTurnCorrelation | None = None,
    ) -> None:
        super().__init__(message)
        self.correlation = correlation


class GatewayTurnTimeout(GatewayTurnError):
    """Raised when a Gateway turn does not finish before the caller timeout."""


class _GatewayTurnDispatcher:
    """Sole endpoint reader that routes frames to facade-owned run inboxes."""

    def __init__(self, endpoint) -> None:
        self.endpoint = endpoint
        self._inboxes: dict[str, asyncio.Queue[Frame | None]] = {}
        self._lock = asyncio.Lock()
        self._reader = asyncio.create_task(
            self._read_loop(),
            name="gateway-turn-dispatcher",
        )

    async def register(self, run_id: str) -> asyncio.Queue[Frame | None]:
        async with self._lock:
            if run_id in self._inboxes:
                raise GatewayTurnError(f"duplicate facade run id: {run_id}")
            inbox: asyncio.Queue[Frame | None] = asyncio.Queue()
            self._inboxes[run_id] = inbox
            return inbox

    async def unregister(self, run_id: str) -> None:
        async with self._lock:
            self._inboxes.pop(run_id, None)

    async def close(self) -> None:
        if not self._reader.done():
            self._reader.cancel()
        await asyncio.gather(self._reader, return_exceptions=True)

    async def _read_loop(self) -> None:
        try:
            async for received in self.endpoint:
                run_id = received.get("run_id")
                if not isinstance(run_id, str):
                    continue
                async with self._lock:
                    inbox = self._inboxes.get(run_id)
                if inbox is not None:
                    await inbox.put(dict(received))
        finally:
            async with self._lock:
                inboxes = list(self._inboxes.values())
                self._inboxes.clear()
            for inbox in inboxes:
                await inbox.put(None)


@dataclass(frozen=True)
class GatewayTurnRequest:
    """Input for one Gateway-normalized user turn."""

    user_id: str
    session_id: str
    text: str
    mode: Literal["followup", "replace"] = "followup"
    image_ids: list[str] = field(default_factory=list)
    video_ids: list[str] = field(default_factory=list)
    audio_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    timeout_s: float = 30.0
    cancel_source: str = "gateway_cancel"
    cancel_reason: str = "caller_cancelled"


@dataclass(frozen=True)
class GatewayTurnResult:
    """Collected result for one Gateway turn."""

    frames: list[Frame]
    terminal_frame: Frame
    response_text: str
    status: str
    reason: str
    run_id: str | None
    turn_id: str | None
    trace_id: str | None
    payload: dict[str, Any]


class GatewayTurnFacade:
    """Run one request/response turn through Gateway frame semantics."""

    def __init__(self, *, manager: GatewaySessionManager) -> None:
        self._manager = manager
        self._dispatchers: dict[str, _GatewayTurnDispatcher] = {}
        self._dispatcher_lock = asyncio.Lock()

    async def _dispatcher_for(self, user_id: str, endpoint) -> _GatewayTurnDispatcher:
        async with self._dispatcher_lock:
            current = self._dispatchers.get(user_id)
            if current is not None and current.endpoint is endpoint:
                return current
            dispatcher = _GatewayTurnDispatcher(endpoint)
            self._dispatchers[user_id] = dispatcher
            return dispatcher

    async def close(self) -> None:
        """Stop facade-owned endpoint readers without closing manager sessions."""

        async with self._dispatcher_lock:
            dispatchers = list(self._dispatchers.values())
            self._dispatchers.clear()
        if dispatchers:
            await asyncio.gather(
                *(dispatcher.close() for dispatcher in dispatchers),
                return_exceptions=True,
            )

    async def run_turn(
        self,
        request: GatewayTurnRequest,
        *,
        on_stream_chunk: GatewayStreamChunkConsumer | None = None,
        on_correlation: GatewayTurnCorrelationObserver | None = None,
    ) -> GatewayTurnResult:
        if not request.user_id:
            raise ValueError("GatewayTurnRequest.user_id is required")
        if not request.session_id:
            raise ValueError("GatewayTurnRequest.session_id is required")
        if request.timeout_s <= 0:
            raise ValueError("GatewayTurnRequest.timeout_s must be positive")
        if request.mode not in {"followup", "replace"}:
            raise ValueError("GatewayTurnRequest.mode must be followup or replace")

        handle = await self._manager.acquire(
            user_id=request.user_id,
            config=request.config,
        )
        initialize_session = getattr(self._manager, "initialize_session", None)
        if callable(initialize_session):
            await initialize_session(
                user_id=request.user_id,
                session_id=request.session_id,
                config=request.config,
            )
        turn_id = new_turn_id()
        run_id = new_run_id()
        dispatcher = await self._dispatcher_for(request.user_id, handle.endpoint)
        inbox = await dispatcher.register(run_id)
        correlation = GatewayTurnCorrelation(turn_id=turn_id, run_id=run_id)
        _notify_correlation(on_correlation, correlation)
        try:
            await handle.endpoint.send(
                frame(
                    type="message.user",
                    session_id=request.session_id,
                    user_id=request.user_id,
                    payload=_message_payload(request, turn_id=turn_id, run_id=run_id),
                )
            )
            return await self._collect_turn(
                inbox,
                handle.endpoint,
                session_id=request.session_id,
                turn_id=turn_id,
                run_id=run_id,
                timeout_s=request.timeout_s,
                cancel_source=request.cancel_source,
                cancel_reason=request.cancel_reason,
                on_stream_chunk=on_stream_chunk,
                on_correlation=on_correlation,
                initial_correlation=correlation,
            )
        finally:
            await dispatcher.unregister(run_id)

    async def _collect_turn(
        self,
        inbox: asyncio.Queue[Frame | None],
        endpoint,
        *,
        session_id: str,
        turn_id: str,
        run_id: str,
        timeout_s: float,
        cancel_source: str,
        cancel_reason: str,
        on_stream_chunk: GatewayStreamChunkConsumer | None,
        on_correlation: GatewayTurnCorrelationObserver | None,
        initial_correlation: GatewayTurnCorrelation,
    ) -> GatewayTurnResult:
        frames: list[Frame] = []
        chunks: list[str] = []
        correlation = initial_correlation

        async def _read_until_terminal() -> GatewayTurnResult:
            nonlocal correlation
            while True:
                received = await inbox.get()
                if received is None:
                    raise GatewayTurnError(
                        "Gateway endpoint closed before run.end",
                        correlation=correlation,
                    )
                frames.append(received)
                updated = _correlation_from_frame(received, current=correlation)
                if updated != correlation:
                    correlation = updated
                    _notify_correlation(on_correlation, correlation)
                if received.get("type") == "error":
                    error = received.get("error")
                    code = error.get("code") if isinstance(error, Mapping) else None
                    raise GatewayTurnError(
                        f"Gateway turn rejected: {code or 'unknown_error'}",
                        correlation=correlation,
                    )
                if received.get("type") == "stream.chunk":
                    chunk = _chunk_text(received)
                    chunks.append(chunk)
                    if chunk and on_stream_chunk is not None:
                        try:
                            await on_stream_chunk(chunk, dict(received))
                        except Exception:
                            await _best_effort_cancel(
                                endpoint,
                                session_id=session_id,
                                turn_id=turn_id,
                                run_id=run_id,
                                source=cancel_source,
                                reason="stream_consumer_failed",
                            )
                            raise
                    continue
                if received.get("type") == "run.end":
                    return _turn_result(frames=frames, terminal=received, chunks=chunks)

        try:
            return await asyncio.wait_for(_read_until_terminal(), timeout=timeout_s)
        except TimeoutError as exc:
            await _best_effort_cancel(
                endpoint,
                session_id=session_id,
                turn_id=turn_id,
                run_id=run_id,
                source="gateway_cancel",
                reason="facade_timeout",
            )
            raise GatewayTurnTimeout(
                f"Gateway turn timed out after {timeout_s:.3g}s before run.end",
                correlation=correlation,
            ) from exc
        except asyncio.CancelledError:
            await _best_effort_cancel(
                endpoint,
                session_id=session_id,
                turn_id=turn_id,
                run_id=run_id,
                source=cancel_source,
                reason=cancel_reason,
            )
            raise


def _correlation_from_frame(
    received: Frame,
    *,
    current: GatewayTurnCorrelation,
) -> GatewayTurnCorrelation:
    if received.get("type") != "event.progress":
        return current
    payload = received.get("payload")
    if not isinstance(payload, Mapping) or payload.get("agent_event_type") != "task_started":
        return current
    trace_id = _optional_string(payload.get("trace_id"))
    if not trace_id:
        return current
    return GatewayTurnCorrelation(
        turn_id=current.turn_id,
        run_id=current.run_id,
        trace_id=trace_id or current.trace_id,
    )


def _notify_correlation(
    observer: GatewayTurnCorrelationObserver | None,
    correlation: GatewayTurnCorrelation,
) -> None:
    if observer is None:
        return
    try:
        observer(correlation)
    except Exception:
        return


async def _best_effort_cancel(
    endpoint,
    *,
    session_id: str,
    turn_id: str,
    run_id: str,
    source: str,
    reason: str,
) -> None:
    try:
        await endpoint.send(
            frame(
                type="run.cancel",
                session_id=session_id,
                turn_id=turn_id,
                run_id=run_id,
                payload={"source": source, "reason": reason},
            )
        )
    except Exception:
        pass


def _message_payload(
    request: GatewayTurnRequest,
    *,
    turn_id: str,
    run_id: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "text": request.text,
        "turn_id": turn_id,
        "run_id": run_id,
        "mode": request.mode,
    }
    if request.image_ids:
        payload["image_ids"] = list(request.image_ids)
    if request.video_ids:
        payload["video_ids"] = list(request.video_ids)
    if request.audio_id is not None:
        payload["audio_id"] = request.audio_id
    if request.metadata:
        payload["metadata"] = dict(request.metadata)
    return payload


def _chunk_text(received: Mapping[str, Any]) -> str:
    payload = received.get("payload")
    if not isinstance(payload, Mapping):
        return ""
    text = payload.get("text")
    return text if isinstance(text, str) else ""


def _turn_result(
    *,
    frames: list[Frame],
    terminal: Frame,
    chunks: list[str],
) -> GatewayTurnResult:
    payload = _payload_dict(terminal)
    reason = str(terminal.get("reason") or "")
    return GatewayTurnResult(
        frames=list(frames),
        terminal_frame=dict(terminal),
        response_text="".join(chunks),
        status=_status_from_terminal(terminal),
        reason=reason,
        run_id=terminal.get("run_id"),
        turn_id=terminal.get("turn_id"),
        trace_id=_optional_string(payload.get("trace_id")),
        payload=payload,
    )


def _payload_dict(received: Mapping[str, Any]) -> dict[str, Any]:
    payload = received.get("payload")
    return dict(payload) if isinstance(payload, Mapping) else {}


def _status_from_terminal(terminal: Mapping[str, Any]) -> str:
    reason = terminal.get("reason")
    if reason in {"completed", "cancelled", "error"}:
        return str(reason)
    if terminal.get("error") is not None:
        return "error"
    return "completed"


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
