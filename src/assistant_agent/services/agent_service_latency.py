"""Prompt-safe end-to-end latency analysis for agent-service chat turns."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from time import perf_counter_ns
from typing import Literal

from pydantic import BaseModel, Field

from assistant_agent.schemas.tool_ids import IMAGE_UNDERSTANDING_TOOL_NAME
from assistant_agent.services.trace_store import TraceEvent, TraceStore, new_span_id


CheckpointName = Literal[
    "queue_entered",
    "queue_acquired",
    "gateway_started",
    "gateway_finished",
    "response_built",
    "send_started",
    "send_finished",
    "ack_received",
    "failed",
    "disconnected",
]

TRACE_STAGE_EVENTS = {
    "conversation.prepare.finished": "conversation_prepare",
    "memory.load.finished": "memory_load",
    "context.build.finished": "context_build",
    "llm.chat.finished": "llm_chat",
    "action.validation.finished": "action_validation",
    "tool.finished": "tool_execute",
    "tool.failed": "tool_execute",
    "response.final": "response_finalize",
    "runtime.postprocess.finished": "runtime_postprocess",
}
_TOOL_TERMINAL_EVENTS = {"tool.finished", "tool.failed"}


@dataclass
class AgentServiceTurnTiming:
    """Constant-size monotonic checkpoints for one accepted chat delivery."""

    delivery_id: str
    session_turn: int
    chat_index_digest: str
    expects_ack: bool
    received_ns: int
    accepted_ns: int
    checkpoints: dict[str, int] = field(default_factory=dict)
    stream_requested: bool = False
    provider_token_stream_seen: bool = False
    stream_chunk_count: int = 0
    first_stream_chunk_ns: int | None = None
    turn_id: str | None = None
    run_id: str | None = None
    trace_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    client_type: str = "media_agent"
    client_name: str | None = None
    runtime_status: str = "unknown"
    failure_code: str | None = None
    failure_source: str | None = None
    deadline_ms: int | None = None

    def mark(self, name: CheckpointName, *, at_ns: int | None = None) -> None:
        self.checkpoints[name] = perf_counter_ns() if at_ns is None else at_ns

    def record_stream_chunk(self, *, at_ns: int | None = None) -> None:
        """Record one delivered provider-token packet without retaining its text."""

        recorded_ns = perf_counter_ns() if at_ns is None else at_ns
        self.provider_token_stream_seen = True
        self.stream_chunk_count += 1
        if self.first_stream_chunk_ns is None:
            self.first_stream_chunk_ns = recorded_ns

    def observe_provider_token_delta(self) -> None:
        """Mark provider streaming without retaining the token delta."""

        self.provider_token_stream_seen = True

    def bind_turn(
        self,
        *,
        turn_id: str | None,
        run_id: str | None,
        trace_id: str | None,
    ) -> None:
        self.turn_id = turn_id
        self.run_id = run_id
        self.trace_id = trace_id

    def mark_failure(
        self,
        *,
        code: str,
        source: str,
        runtime_status: str,
        deadline_ms: int | None = None,
    ) -> None:
        self.failure_code = code
        self.failure_source = source
        self.runtime_status = runtime_status
        self.deadline_ms = deadline_ms


class TurnLatencyStage(BaseModel):
    """One non-overlapping stage or safe secondary diagnostic."""

    name: str
    duration_ms: int = Field(ge=0)
    critical_path: bool = True
    iteration: int | None = None
    tool_name: str | None = None
    provider: str | None = None
    model: str | None = None
    provider_latency_ms: int | None = Field(default=None, ge=0)


class VideoLatencyContext(BaseModel):
    """Latest rolling-video state consumed by this turn."""

    source: str | None = None
    snapshot_status: str | None = None
    snapshot_age_ms: int | None = Field(default=None, ge=0)
    observation_latency_ms: int | None = Field(default=None, ge=0)
    pending_count: int | None = Field(default=None, ge=0)
    in_flight: bool | None = None
    fallback_used: bool = False
    snapshot_sequence: int | None = Field(default=None, ge=0)
    target_sequence: int | None = Field(default=None, ge=0)
    sequence_gap: int | None = Field(default=None, ge=0)
    frame_capture_age_ms: int | None = Field(default=None, ge=0)
    snapshot_publish_age_ms: int | None = Field(default=None, ge=0)
    freshness_waited_ms: int | None = Field(default=None, ge=0)
    freshness_satisfied: bool | None = None
    provider: str | None = None
    model: str | None = None
    waited_for_initial_snapshot: bool = False


class TurnLatencySummary(BaseModel):
    """Safe terminal latency view for one media-service chat turn."""

    schema_version: Literal["agent_service_turn_latency_v2"] = "agent_service_turn_latency_v2"
    status: str
    delivery_id: str
    session_turn: int
    chat_index_digest: str
    client_type: str = "media_agent"
    client_name: str | None = None
    turn_id: str | None = None
    run_id: str | None = None
    trace_id: str | None = None
    runtime_status: str = "unknown"
    failure_code: str | None = None
    failure_source: str | None = None
    deadline_ms: int | None = Field(default=None, ge=0)
    active_stage: str | None = None
    open_span_count: int = Field(default=0, ge=0)
    total_ms: int | None = Field(default=None, ge=0)
    stages: list[TurnLatencyStage] = Field(default_factory=list)
    bottleneck: str | None = None
    bottleneck_ms: int | None = Field(default=None, ge=0)
    bottleneck_share_pct: float | None = Field(default=None, ge=0, le=100)
    unattributed_ms: int | None = Field(default=None, ge=0)
    ack_status: Literal["not_negotiated", "pending", "acked"]
    ack_latency_ms: int | None = Field(default=None, ge=0)
    terminal_stage: str | None = None
    stream_requested: bool = False
    provider_token_stream_seen: bool = False
    stream_chunk_count: int = Field(default=0, ge=0)
    first_stream_chunk_latency_ms: int | None = Field(default=None, ge=0)
    final_response_sent: bool = False
    video: VideoLatencyContext | None = None

    def stage(self, name: str) -> TurnLatencyStage:
        for item in self.stages:
            if item.name == name:
                return item
        raise KeyError(name)


def analyze_agent_service_turn(
    timing: AgentServiceTurnTiming,
    events: list[TraceEvent],
    *,
    status: str,
) -> TurnLatencySummary:
    """Merge transport checkpoints and Assistant trace leaves."""

    total_ms = _duration_ms(timing.received_ns, timing.checkpoints.get("send_finished"))
    stages = _transport_stages(timing)
    stages.extend(_trace_stages(events))

    gateway_duration_ms = _duration_ms(
        timing.checkpoints.get("gateway_started"),
        timing.checkpoints.get("gateway_finished"),
    )
    backend_latency_ms = _latest_latency(events, "realtime.backend.finished")
    if gateway_duration_ms is not None and backend_latency_ms is not None:
        stages.append(
            TurnLatencyStage(
                name="gateway_overhead",
                duration_ms=max(0, gateway_duration_ms - backend_latency_ms),
            )
        )

    send_ms = _duration_ms(
        timing.checkpoints.get("send_started"),
        timing.checkpoints.get("send_finished"),
    )
    if send_ms is not None:
        stages.append(TurnLatencyStage(name="websocket_send", duration_ms=send_ms))

    measured_ms = sum(stage.duration_ms for stage in stages if stage.critical_path)
    unattributed_ms = None if total_ms is None else max(0, total_ms - measured_ms)
    if unattributed_ms:
        stages.append(TurnLatencyStage(name="unattributed", duration_ms=unattributed_ms))

    bottleneck = max(
        (stage for stage in stages if stage.critical_path),
        key=lambda stage: stage.duration_ms,
        default=None,
    )
    ack_status, ack_latency_ms = _ack_timing(timing)
    active_stage, open_span_count = _active_trace_stage(events)
    runtime_status = timing.runtime_status
    if runtime_status == "unknown" and status in {"sent", "acked"}:
        runtime_status = "completed"
    return TurnLatencySummary(
        status=status,
        delivery_id=timing.delivery_id,
        session_turn=timing.session_turn,
        chat_index_digest=timing.chat_index_digest,
        client_type=timing.client_type or "media_agent",
        client_name=timing.client_name,
        turn_id=timing.turn_id,
        run_id=timing.run_id,
        trace_id=timing.trace_id,
        runtime_status=runtime_status,
        failure_code=timing.failure_code,
        failure_source=timing.failure_source,
        deadline_ms=timing.deadline_ms,
        active_stage=active_stage,
        open_span_count=open_span_count,
        total_ms=total_ms,
        stages=stages,
        bottleneck=bottleneck.name if bottleneck is not None else None,
        bottleneck_ms=bottleneck.duration_ms if bottleneck is not None else None,
        bottleneck_share_pct=_share(bottleneck.duration_ms, total_ms) if bottleneck is not None else None,
        unattributed_ms=unattributed_ms,
        ack_status=ack_status,
        ack_latency_ms=ack_latency_ms,
        terminal_stage=_terminal_stage(timing),
        stream_requested=timing.stream_requested,
        provider_token_stream_seen=timing.provider_token_stream_seen,
        stream_chunk_count=timing.stream_chunk_count,
        first_stream_chunk_latency_ms=_duration_ms(
            timing.received_ns,
            timing.first_stream_chunk_ns,
        ),
        final_response_sent="send_finished" in timing.checkpoints,
        video=_video_context(events),
    )


def append_turn_latency_trace(
    trace_store: TraceStore | None,
    *,
    timing: AgentServiceTurnTiming,
    summary: TurnLatencySummary,
) -> bool:
    """Append the safe terminal summary when Assistant correlation exists."""

    if trace_store is None or not timing.trace_id or not timing.run_id:
        return False
    attributes = {
        "delivery_id": timing.delivery_id,
        "session_turn": timing.session_turn,
        "chat_index_digest": timing.chat_index_digest,
        "turn_id": timing.turn_id,
        "run_id": timing.run_id,
        "client_type": timing.client_type or "media_agent",
        "runtime_status": summary.runtime_status,
    }
    if timing.client_name:
        attributes["client_name"] = timing.client_name
    try:
        trace_store.append(
            TraceEvent(
                trace_id=timing.trace_id,
                run_id=timing.run_id,
                user_id=timing.user_id,
                session_id=timing.session_id,
                node_name="agent_service",
                event_type="observability",
                canonical_event="agent_service.turn.finished",
                span_id=new_span_id(),
                status=summary.status,
                latency_ms=summary.total_ms,
                attributes=attributes,
                error=(
                    {
                        "code": summary.failure_code,
                        "source": summary.failure_source,
                    }
                    if summary.failure_code
                    else None
                ),
                output_summary={"turn_latency": summary.model_dump(mode="json")},
            )
        )
    except Exception:
        return False
    return True


def report_turn_latency(summary: TurnLatencySummary, *, logger: logging.Logger) -> None:
    """Emit one prompt-safe operator line without affecting delivery."""

    try:
        logger.info(
            "turn_latency status=%s trace=%s run=%s "
            "delivery=%s session_turn=%s client=%s total=%s bottleneck=%s bottleneck_ms=%s share=%s",
            summary.status,
            summary.trace_id or "none",
            summary.run_id or "none",
            summary.delivery_id,
            summary.session_turn,
            summary.client_type,
            _format_ms(summary.total_ms),
            summary.bottleneck or "none",
            _format_ms(summary.bottleneck_ms),
            _format_share(summary.bottleneck_share_pct),
        )
    except Exception:
        return


def _transport_stages(timing: AgentServiceTurnTiming) -> list[TurnLatencyStage]:
    stages: list[TurnLatencyStage] = []
    entry_ms = _duration_ms(timing.received_ns, timing.accepted_ns)
    if entry_ms is not None:
        stages.append(TurnLatencyStage(name="entry_parse", duration_ms=entry_ms))
    queue_ms = _duration_ms(
        timing.checkpoints.get("queue_entered"),
        timing.checkpoints.get("queue_acquired"),
    )
    if queue_ms is not None:
        stages.append(TurnLatencyStage(name="chat_queue_wait", duration_ms=queue_ms))
    return stages


def _trace_stages(events: list[TraceEvent]) -> list[TurnLatencyStage]:
    stages: list[TurnLatencyStage] = []
    for event in events:
        base_name = TRACE_STAGE_EVENTS.get(event.canonical_event or "")
        if base_name is None:
            continue
        duration_ms = _event_duration(event)
        if duration_ms is None:
            continue
        iteration = _safe_int(event.attributes.get("iteration"))
        name = base_name
        if base_name == "tool_execute" and event.tool_name:
            name = f"{base_name}[{event.tool_name}]"
        elif iteration is not None:
            name = f"{base_name}[{iteration}]"
        stages.append(
            TurnLatencyStage(
                name=name,
                duration_ms=duration_ms,
                iteration=iteration,
                tool_name=event.tool_name,
                provider=event.provider,
                model=event.model,
                provider_latency_ms=_provider_latency(event) if base_name == "llm_chat" else None,
            )
        )
    return stages


def _active_trace_stage(events: list[TraceEvent]) -> tuple[str | None, int]:
    finished_spans = {
        event.span_id
        for event in events
        if event.span_id and (event.canonical_event or "").endswith(".finished")
    }
    open_events = [
        event
        for event in events
        if event.span_id
        and (event.canonical_event or "").endswith(".started")
        and event.span_id not in finished_spans
        and event.attributes.get("execution_phase") != "post_response_background"
    ]
    if not open_events:
        return None, 0
    active = open_events[-1]
    name = (active.canonical_event or "active").removesuffix(".started").replace(".", "_")
    iteration = _safe_int(active.attributes.get("iteration"))
    if iteration is not None:
        name = f"{name}[{iteration}]"
    return name, len(open_events)


def _event_duration(event: TraceEvent) -> int | None:
    if event.canonical_event == "llm.chat.finished":
        wall_latency = _safe_int(event.attributes.get("wall_latency_ms"))
        if wall_latency is not None:
            return wall_latency
    return _safe_int(event.latency_ms)


def _provider_latency(event: TraceEvent) -> int | None:
    return _safe_int(event.attributes.get("provider_latency_ms")) or _safe_int(event.latency_ms)


def _latest_latency(events: list[TraceEvent], canonical_event: str) -> int | None:
    for event in reversed(events):
        if event.canonical_event == canonical_event:
            return _event_duration(event)
    return None


def _video_context(events: list[TraceEvent]) -> VideoLatencyContext | None:
    for event in reversed(events):
        if event.canonical_event != "context.build.finished":
            continue
        context = event.output_summary.get("context")
        video = context.get("realtime_video") if isinstance(context, dict) else None
        if not isinstance(video, dict) or video.get("present") is not True:
            continue
        return VideoLatencyContext(
            source="realtime_video_context",
            snapshot_status=_safe_text(video.get("status")),
            snapshot_age_ms=_safe_int(video.get("snapshot_age_ms")),
            observation_latency_ms=_safe_int(video.get("observation_latency_ms")),
            pending_count=_safe_int(video.get("pending_count")),
            in_flight=video.get("in_flight") if isinstance(video.get("in_flight"), bool) else None,
            snapshot_sequence=_safe_int(video.get("snapshot_sequence")),
            target_sequence=_safe_int(video.get("target_sequence")),
            sequence_gap=_safe_int(video.get("sequence_gap")),
            frame_capture_age_ms=_safe_int(video.get("frame_capture_age_ms")),
            snapshot_publish_age_ms=_safe_int(video.get("snapshot_publish_age_ms")),
            freshness_waited_ms=_safe_int(video.get("freshness_waited_ms")),
            freshness_satisfied=_safe_bool(video.get("freshness_satisfied")),
            provider=_safe_text(video.get("provider")),
            model=_safe_text(video.get("model")),
            waited_for_initial_snapshot=video.get("waited_for_initial_snapshot") is True,
        )
    for event in reversed(events):
        if event.canonical_event not in _TOOL_TERMINAL_EVENTS or event.tool_name != IMAGE_UNDERSTANDING_TOOL_NAME:
            continue
        payload = event.output_summary
        return VideoLatencyContext(
            source=_safe_text(payload.get("source")),
            snapshot_age_ms=_safe_int(payload.get("snapshot_age_ms")),
            observation_latency_ms=_safe_int(payload.get("observation_latency_ms")),
            pending_count=_safe_int(payload.get("pending_count")),
            in_flight=payload.get("in_flight") if isinstance(payload.get("in_flight"), bool) else None,
            fallback_used=payload.get("fallback_used") is True,
            snapshot_sequence=_safe_int(payload.get("snapshot_sequence")),
            target_sequence=_safe_int(payload.get("target_sequence")),
            sequence_gap=_safe_int(payload.get("sequence_gap")),
            frame_capture_age_ms=_safe_int(payload.get("frame_capture_age_ms")),
            snapshot_publish_age_ms=_safe_int(payload.get("snapshot_publish_age_ms")),
            freshness_waited_ms=_safe_int(payload.get("freshness_waited_ms")),
            freshness_satisfied=_safe_bool(payload.get("freshness_satisfied")),
            provider=_safe_text(payload.get("provider")),
            model=_safe_text(payload.get("model")),
        )
    return None


def _ack_timing(timing: AgentServiceTurnTiming) -> tuple[str, int | None]:
    if not timing.expects_ack:
        return "not_negotiated", None
    ack_received = timing.checkpoints.get("ack_received")
    if ack_received is None:
        return "pending", None
    return "acked", _duration_ms(timing.checkpoints.get("send_finished"), ack_received)


def _terminal_stage(timing: AgentServiceTurnTiming) -> str | None:
    for name in ("disconnected", "failed", "ack_received", "send_finished", "gateway_finished"):
        if name in timing.checkpoints:
            return name
    return None


def _duration_ms(start_ns: int | None, end_ns: int | None) -> int | None:
    if start_ns is None or end_ns is None:
        return None
    return max(0, int((end_ns - start_ns) / 1_000_000))


def _safe_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))


def _safe_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _safe_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _share(duration_ms: int, total_ms: int | None) -> float | None:
    if total_ms is None or total_ms <= 0:
        return None
    return min(100.0, round(duration_ms * 100 / total_ms, 1))


def _format_ms(value: int | None) -> str:
    return "none" if value is None else f"{value}ms"


def _format_share(value: float | None) -> str:
    return "none" if value is None else f"{value:.1f}%"
