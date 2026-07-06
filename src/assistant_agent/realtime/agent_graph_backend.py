"""Realtime backend adapter for the existing assistant graph run service."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from assistant_agent.agent.cancellation import cancellation_metadata
from assistant_agent.realtime.backend import RealtimeEventSink
from assistant_agent.realtime.event_mapping import (
    map_agent_event,
    map_agent_event_stream,
    map_agent_event_with_final_response_chunks,
)
from assistant_agent.realtime.progress import ProgressPolicy, ProgressTracker
from assistant_agent.realtime.types import (
    RealtimeAgentEvent,
    RealtimeAgentRequest,
    RealtimeAgentResult,
    RealtimeBackendCapabilities,
    RealtimeCancelToken,
)
from assistant_agent.schemas.events import AgentEvent
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.assistant_run_service import run_assistant_request
from assistant_agent.services.realtime_task_state import realtime_metadata_requests_interrupt


RunAssistantRequest = Callable[..., Any]

_RUN_EVENT_TYPES = {
    "task_started",
    "graph_node_started",
    "graph_node_finished",
    "tool_started",
    "tool_progress",
    "tool_finished",
    "tool_completed",
    "tool_failed",
    "agent_trace_decision",
    "agent_trace_observation",
    "response_delta",
    "agent_error",
    "task_failed",
    "task_cancelled",
}


class AgentGraphRealtimeBackend:
    """Thin Gateway realtime adapter backed by the main assistant runtime.

    This class is intentionally not the agent's "main brain". It translates
    `RealtimeAgentRequest` into normalized runtime requests, forwards
    runtime events, and maps terminal results back to realtime response types.
    Planning, tool selection, memory policy, provider policy, and optional
    agent delegation stay in `AgentGraphRuntime`/assistant loop and its
    tool-governed delegation path.
    """

    def __init__(
        self,
        *,
        run_request: RunAssistantRequest | None = None,
        load_env: bool = True,
        enable_conversation_history: bool = True,
        progress_policy: ProgressPolicy | None = None,
    ) -> None:
        self._run_request = run_request
        self._load_env = load_env
        self._enable_conversation_history = enable_conversation_history
        self._progress_policy = progress_policy or ProgressPolicy()

    @property
    def capabilities(self) -> RealtimeBackendCapabilities:
        """Return the backend's realtime capability declaration."""

        return RealtimeBackendCapabilities()

    async def run_turn(
        self,
        request: RealtimeAgentRequest,
        *,
        event_sink: RealtimeEventSink | None = None,
        cancel_token: RealtimeCancelToken | None = None,
    ) -> RealtimeAgentResult:
        """Run one realtime turn through the existing assistant run service."""

        if _is_cancelled(cancel_token):
            return RealtimeAgentResult(
                status="cancelled",
                run_id=request.run_id,
                metadata={
                    **cancellation_metadata(cancel_token),
                    "cancel_phase": "pre_run",
                    "best_effort": True,
                },
            )

        user_request = realtime_request_to_user_request(request)
        loop = asyncio.get_running_loop()
        forwarder = _RealtimeForwardingEventSink(
            loop=loop,
            event_sink=event_sink,
            progress_policy=self._progress_policy,
        )
        heartbeat_task = forwarder.start_heartbeat(cancel_token)

        try:
            await _emit_task_revision_progress_if_needed(event_sink, user_request)
            run_request = self._run_request or run_assistant_request
            try:
                artifacts = await asyncio.to_thread(
                    run_request,
                    user_request,
                    event_sink=forwarder,
                    load_env=self._load_env,
                    enable_conversation_history=self._enable_conversation_history,
                    cancel_token=cancel_token,
                )
            finally:
                await _stop_heartbeat(heartbeat_task)
            await forwarder.drain()

            state = artifacts.state
            result_run_id = request.run_id or state.run_id
            result_metadata = {"assistant_run_id": state.run_id}

            if state.status == "cancelled":
                state_cancel_metadata = _cancel_metadata_from_state(state)
                return RealtimeAgentResult(
                    status="cancelled",
                    run_id=result_run_id,
                    trace_id=state.trace_id,
                    metadata={
                        **result_metadata,
                        **state_cancel_metadata,
                        "cancel_phase": _cancel_phase_from_state(state) or "agent_run",
                        "best_effort": True,
                    },
                )

            if _is_cancelled(cancel_token):
                return RealtimeAgentResult(
                    status="cancelled",
                    run_id=result_run_id,
                    trace_id=state.trace_id,
                    metadata={
                        **result_metadata,
                        **cancellation_metadata(cancel_token),
                        "cancel_phase": "post_run",
                        "best_effort": True,
                    },
                )

            response = state.response
            response_text = response.message if response is not None else ""
            status = "error" if state.status == "failed" else "completed"

            if status == "completed" and event_sink is not None:
                await _emit_final_response_events(
                    event_sink,
                    session_id=state.session_id,
                    run_id=state.run_id,
                    response_text=response_text,
                    emit_chunks=not forwarder.response_delta_seen,
                )

            return RealtimeAgentResult(
                status=status,
                response_text=response_text,
                expects_reply=bool(response.followup_question) if response is not None else False,
                run_id=result_run_id,
                trace_id=state.trace_id,
                output_refs=list(response.output_refs) if response is not None else [],
                metadata=result_metadata,
            )
        except Exception as exc:  # pragma: no cover - exact source varies by runtime.
            await _emit_backend_error(event_sink, request=request, error=exc)
            return RealtimeAgentResult(
                status="error",
                run_id=request.run_id,
                metadata={
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )


def realtime_request_to_user_request(request: RealtimeAgentRequest) -> UserRequest:
    """Convert a realtime request into the assistant's normalized UserRequest."""

    metadata = dict(request.metadata)
    realtime_metadata = metadata.get("realtime")
    if isinstance(realtime_metadata, dict):
        realtime = dict(realtime_metadata)
    else:
        realtime = {}
    realtime["run_id"] = request.run_id
    realtime["turn_id"] = request.turn_id
    metadata["realtime"] = realtime
    metadata.setdefault("source", "realtime_agent_backend")

    return UserRequest(
        user_id=request.user_id,
        session_id=request.session_id,
        text=request.text,
        image_ids=list(request.image_ids),
        video_ids=list(request.video_ids),
        audio_id=request.audio_id,
        metadata=metadata,
    )


async def _emit_task_revision_progress_if_needed(
    event_sink: RealtimeEventSink | None,
    request: UserRequest,
) -> None:
    if event_sink is None or not realtime_metadata_requests_interrupt(request.metadata):
        return
    realtime = request.metadata.get("realtime")
    await event_sink(
        RealtimeAgentEvent(
            type="run.progress",
            text="Revising task with the latest user correction.",
            payload={
                "stage": "task_state",
                "status": "revising",
                "current_step": "intent_revision",
                "display_only": True,
                "run_id": realtime.get("run_id") if isinstance(realtime, dict) else None,
                "turn_id": realtime.get("turn_id") if isinstance(realtime, dict) else None,
            },
            display_only=True,
        )
    )


class _RealtimeForwardingEventSink:
    """Synchronous EventSink that forwards selected events to an async sink."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        event_sink: RealtimeEventSink | None,
        progress_policy: ProgressPolicy,
    ) -> None:
        self.events: list[AgentEvent] = []
        self._loop = loop
        self._event_sink = event_sink
        self._progress: ProgressTracker = progress_policy.tracker()
        self._pending: list[Future[None]] = []
        self._response_delta_seen = False

    @property
    def response_delta_seen(self) -> bool:
        return self._response_delta_seen

    def emit(self, event: AgentEvent) -> None:
        self.events.append(event)
        if self._event_sink is None or event.type not in _RUN_EVENT_TYPES:
            return
        if event.type == "response_delta":
            self._response_delta_seen = True
        mapped_events = map_agent_event_stream(event)
        if not mapped_events:
            return
        self._pending.append(
            asyncio.run_coroutine_threadsafe(self._forward_events(mapped_events), self._loop)
        )

    def start_heartbeat(
        self,
        cancel_token: RealtimeCancelToken | None,
    ) -> asyncio.Task[None] | None:
        if self._event_sink is None:
            return None
        return asyncio.create_task(self._heartbeat_loop(cancel_token))

    async def _forward_events(self, events: list[RealtimeAgentEvent]) -> None:
        if self._event_sink is None:
            return
        for event in events:
            if self._progress.should_emit(event):
                await self._event_sink(event)

    async def _heartbeat_loop(self, cancel_token: RealtimeCancelToken | None) -> None:
        while not _is_cancelled(cancel_token):
            await asyncio.sleep(self._progress.heartbeat_poll_interval_s())
            if _is_cancelled(cancel_token):
                return
            heartbeat = self._progress.heartbeat()
            if heartbeat is not None and self._event_sink is not None:
                await self._event_sink(heartbeat)

    async def drain(self) -> None:
        if not self._pending:
            return
        pending = list(self._pending)
        self._pending.clear()
        await asyncio.gather(*(asyncio.wrap_future(future) for future in pending))


async def _stop_heartbeat(task: asyncio.Task[None] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _emit_final_response_events(
    event_sink: RealtimeEventSink,
    *,
    session_id: str,
    run_id: str,
    response_text: str,
    emit_chunks: bool = True,
) -> None:
    final_event = AgentEvent(
        type="final_response",
        session_id=session_id,
        run_id=run_id,
        text=response_text,
    )
    if emit_chunks:
        realtime_events = map_agent_event_with_final_response_chunks(final_event)
    else:
        mapped = map_agent_event(final_event)
        realtime_events = [mapped] if mapped is not None else []
    for realtime_event in realtime_events:
        await event_sink(realtime_event)


async def _emit_backend_error(
    event_sink: RealtimeEventSink | None,
    *,
    request: RealtimeAgentRequest,
    error: Exception,
) -> None:
    if event_sink is None:
        return
    error_type = type(error).__name__
    error_message = str(error)
    await event_sink(
        RealtimeAgentEvent(
            type="error",
            text=error_message,
            payload={
                "error_type": error_type,
                "error_message": error_message,
                "session_id": request.session_id,
                "run_id": request.run_id,
            },
        )
    )


def _is_cancelled(cancel_token: RealtimeCancelToken | None) -> bool:
    return cancel_token is not None and cancel_token.is_cancelled()


def _cancel_phase_from_state(state: Any) -> str | None:
    errors = getattr(state, "errors", None)
    if not errors:
        return None
    details = getattr(errors[-1], "details", None)
    if not isinstance(details, dict):
        return None
    phase = details.get("cancel_phase")
    return phase if isinstance(phase, str) and phase else None


def _cancel_metadata_from_state(state: Any) -> dict[str, Any]:
    errors = getattr(state, "errors", None)
    if not errors:
        return {}
    details = getattr(errors[-1], "details", None)
    if not isinstance(details, dict):
        return {}
    metadata: dict[str, Any] = {}
    for key in ("cancel_source", "cancel_reason", "deadline_ms"):
        if key in details:
            metadata[key] = details[key]
    return metadata
