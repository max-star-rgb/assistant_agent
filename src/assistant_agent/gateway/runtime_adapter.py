"""Realtime backend adapter for the existing assistant graph run service."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from time import perf_counter
from typing import Any

from assistant_agent.runtime.event_stream import AgentRunStream, AsyncQueueEventSink
from assistant_agent.runtime.cancellation import cancellation_metadata
from assistant_agent.gateway.runtime_backend import RealtimeEventSink
from assistant_agent.gateway.runtime_event_mapping import (
    map_agent_event,
    map_agent_event_stream,
    map_agent_event_with_final_response_chunks,
)
from assistant_agent.gateway.progress import ProgressPolicy, ProgressTracker
from assistant_agent.gateway.shopping_detail import project_shopping_delivery_text
from assistant_agent.gateway.runtime_types import (
    RealtimeAgentEvent,
    RealtimeAgentRequest,
    RealtimeAgentResult,
    RealtimeBackendCapabilities,
    RealtimeCancelToken,
)
from assistant_agent.gateway.cancellation_models import (
    build_realtime_turn_cancellation_metadata,
)
from assistant_agent.runtime.events import AgentEvent
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.assistant_run_service import (
    run_assistant_request_stream,
)
from assistant_agent.runtime.realtime_task_state import realtime_metadata_requests_interrupt
from assistant_agent.observability.trace_store import append_observability_event


RunAssistantRequest = Callable[..., Any]
RunAssistantRequestStream = Callable[..., AgentRunStream[Any]]

_RUN_EVENT_TYPES = {
    "task_started",
    "graph_node_started",
    "graph_node_finished",
    "tool_started",
    "tool_progress",
    "tool_finished",
    "tool_completed",
    "tool_failed",
    "progress_message",
    "agent_trace_decision",
    "agent_trace_observation",
    "response_delta",
    "agent_error",
    "task_failed",
    "task_cancelled",
}


class GatewayRuntimeAdapter:
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
        run_request_stream: RunAssistantRequestStream | None = None,
        load_env: bool = True,
        enable_conversation_history: bool = True,
        progress_policy: ProgressPolicy | None = None,
    ) -> None:
        self._run_request = run_request
        self._run_request_stream = run_request_stream
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
                    **build_realtime_turn_cancellation_metadata(
                        cancellation_metadata(cancel_token),
                        phase="pre_run",
                    ),
                    "cancel_phase": "pre_run",
                    "best_effort": True,
                },
            )

        user_request = realtime_request_to_user_request(request)
        backend_started_at = perf_counter()
        forwarder = _RealtimeForwardingEventSink(
            event_sink=event_sink,
            progress_policy=self._progress_policy,
        )
        first_progress_task = forwarder.start_first_progress_fallback(cancel_token)
        heartbeat_task = forwarder.start_heartbeat(cancel_token)

        try:
            revision_progress = _task_revision_progress_event(user_request)
            if revision_progress is not None:
                await forwarder.forward_realtime_event(revision_progress)
            try:
                runtime_call_started_at = perf_counter()
                stream = self._assistant_request_stream(
                    user_request,
                    cancel_token=cancel_token,
                    run_id=request.run_id,
                )
                async for agent_event in stream:
                    await forwarder.forward_agent_event(agent_event)
                artifacts = await stream.result()
                runtime_call_latency_ms = int((perf_counter() - runtime_call_started_at) * 1000)
            finally:
                await _stop_task(first_progress_task)
                await _stop_task(heartbeat_task)
            await forwarder.drain()

            state = artifacts.state
            result_run_id = state.run_id
            result_metadata: dict[str, Any] = {}
            _append_realtime_backend_finished_event(
                artifacts=artifacts,
                request=request,
                state=state,
                result_run_id=result_run_id,
                backend_latency_ms=int((perf_counter() - backend_started_at) * 1000),
                runtime_call_latency_ms=runtime_call_latency_ms,
                progress_summary=forwarder.progress_summary(),
            )

            if state.status == "cancelled":
                state_cancel_metadata = _cancel_metadata_from_state(state)
                cancel_phase = _cancel_phase_from_state(state) or "agent_run"
                return RealtimeAgentResult(
                    status="cancelled",
                    run_id=result_run_id,
                    trace_id=state.trace_id,
                    metadata={
                        **result_metadata,
                        "realtime_progress": forwarder.progress_summary(),
                        **build_realtime_turn_cancellation_metadata(
                            state_cancel_metadata,
                            phase=cancel_phase,
                        ),
                        "cancel_phase": cancel_phase,
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
                        "realtime_progress": forwarder.progress_summary(),
                        **build_realtime_turn_cancellation_metadata(
                            cancellation_metadata(cancel_token),
                            phase="post_run",
                        ),
                        "cancel_phase": "post_run",
                        "best_effort": True,
                    },
                )

            response = state.response
            response_text = response.message if response is not None else ""
            status = "error" if state.status == "failed" else "completed"
            delivered_text = response_text
            shopping_detail = ""
            if status == "completed":
                delivered_text, shopping_detail = project_shopping_delivery_text(
                    response_text,
                    state.tool_results,
                    metadata=user_request.metadata,
                )
            delivery_source = (
                "shopping_detail_v1" if shopping_detail else "assistant_response"
            )

            if status == "completed" and event_sink is not None:
                if shopping_detail and forwarder.response_delta_seen:
                    await forwarder.forward_realtime_event(
                        RealtimeAgentEvent(
                            type="response.chunk",
                            text=f"\n{shopping_detail}",
                            payload={
                                "shopping_detail_version": "v1",
                                "token_streaming": False,
                            },
                            content_type="detail",
                        )
                    )
                await _emit_final_response_events(
                    forwarder,
                    session_id=state.session_id,
                    run_id=state.run_id,
                    response_text=delivered_text,
                    emit_chunks=not forwarder.response_delta_seen,
                )

            result_metadata["realtime_progress"] = forwarder.progress_summary()
            result_metadata["response_delivery_source"] = delivery_source
            return RealtimeAgentResult(
                status=status,
                response_text=delivered_text,
                expects_reply=bool(response.followup_question) if response is not None else False,
                run_id=result_run_id,
                trace_id=state.trace_id,
                output_refs=list(response.output_refs) if response is not None else [],
                metadata=result_metadata,
            )
        except Exception as exc:  # pragma: no cover - exact source varies by runtime.
            await forwarder.drain()
            await _emit_backend_error(forwarder, request=request, error=exc)
            return RealtimeAgentResult(
                status="error",
                run_id=request.run_id,
                metadata={
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "realtime_progress": forwarder.progress_summary(),
                },
            )
        finally:
            await _stop_task(first_progress_task)
            await _stop_task(heartbeat_task)

    def _assistant_request_stream(
        self,
        request: UserRequest,
        *,
        cancel_token: RealtimeCancelToken | None = None,
        run_id: str | None = None,
    ) -> AgentRunStream[Any]:
        if self._run_request_stream is not None:
            return self._run_request_stream(
                request,
                load_env=self._load_env,
                enable_conversation_history=self._enable_conversation_history,
                cancel_token=cancel_token,
                run_id=run_id,
            )
        if self._run_request is not None:
            return _sync_run_request_stream(
                self._run_request,
                request,
                load_env=self._load_env,
                enable_conversation_history=self._enable_conversation_history,
                cancel_token=cancel_token,
                run_id=run_id,
            )
        return run_assistant_request_stream(
            request,
            load_env=self._load_env,
            enable_conversation_history=self._enable_conversation_history,
            cancel_token=cancel_token,
            run_id=run_id,
        )


def _append_realtime_backend_finished_event(
    *,
    artifacts: Any,
    request: RealtimeAgentRequest,
    state: Any,
    result_run_id: str | None,
    backend_latency_ms: int,
    runtime_call_latency_ms: int,
    progress_summary: dict[str, Any],
) -> None:
    runtime = getattr(artifacts, "runtime", None)
    trace_store = getattr(runtime, "trace_store", None)
    append_observability_event(
        trace_store,
        trace_id=state.trace_id,
        run_id=state.run_id,
        user_id=state.user_id,
        session_id=state.session_id,
        canonical_event="realtime.backend.finished",
        node_name="realtime_backend",
        status="succeeded" if state.status not in {"failed", "cancelled"} else state.status,
        latency_ms=backend_latency_ms,
        attributes={
            "run_id": result_run_id,
            "runtime_call_latency_ms": runtime_call_latency_ms,
            "user_visible_event_count": progress_summary.get("user_visible_event_count"),
            "sla_fallback_emitted": progress_summary.get("sla_fallback_emitted"),
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
    if not _suppress_default_source(metadata):
        metadata.setdefault("source", "realtime_agent_backend")

    return UserRequest(
        user_id=request.user_id,
        session_id=request.session_id,
        text=request.text,
        image_ids=list(request.image_ids),
        video_ids=list(request.video_ids),
        audio_id=request.audio_id,
        assistant_mode=request.assistant_mode,
        execution_strategy=_execution_strategy_from_metadata(metadata),
        response_style=request.response_style,
        metadata=metadata,
    )


def _execution_strategy_from_metadata(metadata: dict[str, Any]) -> str:
    return "plan_and_solve" if metadata.get("execution_strategy") == "plan_and_solve" else "react"


def _suppress_default_source(metadata: dict[str, Any]) -> bool:
    for key in ("gateway", "runtime"):
        value = metadata.get(key)
        if isinstance(value, dict) and value.get("suppress_realtime_backend_source") is True:
            return True
    return False


def _task_revision_progress_event(request: UserRequest) -> RealtimeAgentEvent | None:
    if not realtime_metadata_requests_interrupt(request.metadata):
        return None
    realtime = request.metadata.get("realtime")
    return RealtimeAgentEvent(
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


class _RealtimeForwardingEventSink:
    """Map runtime AgentEvent records to realtime events in the active event loop."""

    def __init__(
        self,
        *,
        event_sink: RealtimeEventSink | None,
        progress_policy: ProgressPolicy,
    ) -> None:
        self.events: list[AgentEvent] = []
        self._event_sink = event_sink
        self._progress: ProgressTracker = progress_policy.tracker()
        self._response_delta_seen = False

    @property
    def response_delta_seen(self) -> bool:
        return self._response_delta_seen

    def progress_summary(self) -> dict[str, object]:
        return self._progress.summary()

    async def forward_agent_event(self, event: AgentEvent) -> None:
        self.events.append(event)
        if self._event_sink is None or event.type not in _RUN_EVENT_TYPES:
            return
        if event.type == "response_delta":
            self._response_delta_seen = True
        mapped_events = map_agent_event_stream(event)
        if not mapped_events:
            return
        await self._forward_events(mapped_events)

    def start_heartbeat(
        self,
        cancel_token: RealtimeCancelToken | None,
    ) -> asyncio.Task[None] | None:
        if self._event_sink is None:
            return None
        return asyncio.create_task(self._heartbeat_loop(cancel_token))

    def start_first_progress_fallback(
        self,
        cancel_token: RealtimeCancelToken | None,
    ) -> asyncio.Task[None] | None:
        if self._event_sink is None or self._progress.policy.first_progress_timeout_s <= 0:
            return None
        return asyncio.create_task(self._first_progress_fallback_loop(cancel_token))

    async def _forward_events(self, events: list[RealtimeAgentEvent]) -> None:
        for event in events:
            await self.forward_realtime_event(event)

    async def forward_realtime_event(self, event: RealtimeAgentEvent) -> None:
        if self._event_sink is None:
            return
        if self._progress.should_emit(event):
            await self._event_sink(event)

    async def _first_progress_fallback_loop(
        self,
        cancel_token: RealtimeCancelToken | None,
    ) -> None:
        await asyncio.sleep(self._progress.policy.first_progress_timeout_s)
        if _is_cancelled(cancel_token):
            return
        fallback = self._progress.first_progress_fallback()
        if fallback is not None:
            await self.forward_realtime_event(fallback)

    async def _heartbeat_loop(self, cancel_token: RealtimeCancelToken | None) -> None:
        while not _is_cancelled(cancel_token):
            await asyncio.sleep(self._progress.heartbeat_poll_interval_s())
            if _is_cancelled(cancel_token):
                return
            heartbeat = self._progress.heartbeat()
            if heartbeat is not None and self._event_sink is not None:
                await self._event_sink(heartbeat)

    async def drain(self) -> None:
        return None


def _sync_run_request_stream(
    run_request: RunAssistantRequest,
    request: UserRequest,
    *,
    load_env: bool,
    enable_conversation_history: bool,
    cancel_token: RealtimeCancelToken | None,
    run_id: str | None = None,
) -> AgentRunStream[Any]:
    loop = asyncio.get_running_loop()
    stream: AgentRunStream[Any] = AgentRunStream(loop=loop)
    stream_sink = AsyncQueueEventSink(loop=loop, stream=stream)

    async def _run() -> None:
        try:
            artifacts = await asyncio.to_thread(
                run_request,
                request,
                event_sink=stream_sink,
                load_env=load_env,
                enable_conversation_history=enable_conversation_history,
                cancel_token=cancel_token,
                run_id=run_id,
            )
        except BaseException as exc:
            stream.set_exception(exc)
        else:
            stream.set_result(artifacts)

    asyncio.create_task(_run())
    return stream


async def _stop_task(task: asyncio.Task[None] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _emit_final_response_events(
    forwarder: _RealtimeForwardingEventSink,
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
        await forwarder.forward_realtime_event(realtime_event)


async def _emit_backend_error(
    forwarder: _RealtimeForwardingEventSink,
    *,
    request: RealtimeAgentRequest,
    error: Exception,
) -> None:
    error_type = type(error).__name__
    error_message = str(error)
    await forwarder.forward_realtime_event(
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
    for key in (
        "cancel_source",
        "cancel_reason",
        "deadline_ms",
        "realtime_turn_cancellation",
        "stale_outputs",
        "can_reuse_tool_result",
        "speakable",
    ):
        if key in details:
            metadata[key] = details[key]
    return metadata
