"""Runtime service backed directly by assistant_agent realtime backends."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from assistant_agent.realtime import (
    AgentGraphRealtimeBackend,
    RealtimeAgentBackend,
    RealtimeAgentEvent,
    RealtimeAgentRequest,
    RealtimeAgentResult,
)
from assistant_agent.runtime_gateway.event_mapping import realtime_event_to_frame
from assistant_agent.runtime_gateway.protocol import Frame, frame
from assistant_agent.runtime_gateway.transport import Endpoint


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


class RuntimeService:
    """Runtime side of the Gateway<->Runtime stream.

    This service owns session history, run lifecycle, cooperative cancellation,
    and event mapping. Agent execution is delegated directly to a
    RealtimeAgentBackend; by default this is AgentGraphRealtimeBackend.
    """

    def __init__(
        self,
        *,
        user_id: str = "default",
        backend: RealtimeAgentBackend | None = None,
        backend_factory: Callable[[], RealtimeAgentBackend] | None = None,
    ) -> None:
        self._user_id = user_id
        self._backend = backend
        self._backend_factory = backend_factory
        self._active_by_session: dict[str, ActiveRun] = {}
        self._history_by_session: dict[str, list[str]] = {}
        self._lock = asyncio.Lock()

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
        runtime_metadata = metadata.get("runtime")
        if isinstance(runtime_metadata, dict):
            runtime = dict(runtime_metadata)
        else:
            runtime = {}
        runtime["history"] = list(history)
        metadata["runtime"] = runtime

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
