"""Prompt-safe end-to-end latency analysis for agent-service chat turns."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from time import perf_counter_ns
from typing import Literal

from pydantic import BaseModel, Field

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
    turn_id: str | None = None
    gateway_run_id: str | None = None
    assistant_run_id: str | None = None
    trace_id: str | None = None

    def mark(self, name: CheckpointName, *, at_ns: int | None = None) -> None:
        self.checkpoints[name] = perf_counter_ns() if at_ns is None else at_ns

    def bind_turn(
        self,
        *,
        turn_id: str | None,
        gateway_run_id: str | None,
        assistant_run_id: str | None,
        trace_id: str | None,
    ) -> None:
        self.turn_id = turn_id
        self.gateway_run_id = gateway_run_id
        self.assistant_run_id = assistant_run_id
        self.trace_id = trace_id


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
    snapshot_age_ms: int | None = Field(default=None, ge=0)
    observation_latency_ms: int | None = Field(default=None, ge=0)
    pending_count: int | None = Field(default=None, ge=0)
    in_flight: bool | None = None
    fallback_used: bool = False
    snapshot_sequence: int | None = Field(default=None, ge=0)


class TurnLatencySummary(BaseModel):
    """Safe terminal latency view for one media-service chat turn."""

    schema_version: Literal["agent_service_turn_latency_v1"] = "agent_service_turn_latency_v1"
    status: str
    delivery_id: str
    session_turn: int
    chat_index_digest: str
    turn_id: str | None = None
    gateway_run_id: str | None = None
    assistant_run_id: str | None = None
    trace_id: str | None = None
    total_ms: int | None = Field(default=None, ge=0)
    stages: list[TurnLatencyStage] = Field(default_factory=list)
    bottleneck: str | None = None
    bottleneck_ms: int | None = Field(default=None, ge=0)
    bottleneck_share_pct: float | None = Field(default=None, ge=0, le=100)
    unattributed_ms: int | None = Field(default=None, ge=0)
    ack_status: Literal["not_negotiated", "pending", "acked"]
    ack_latency_ms: int | None = Field(default=None, ge=0)
    terminal_stage: str | None = None
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
    return TurnLatencySummary(
        status=status,
        delivery_id=timing.delivery_id,
        session_turn=timing.session_turn,
        chat_index_digest=timing.chat_index_digest,
        turn_id=timing.turn_id,
        gateway_run_id=timing.gateway_run_id,
        assistant_run_id=timing.assistant_run_id,
        trace_id=timing.trace_id,
        total_ms=total_ms,
        stages=stages,
        bottleneck=bottleneck.name if bottleneck is not None else None,
        bottleneck_ms=bottleneck.duration_ms if bottleneck is not None else None,
        bottleneck_share_pct=_share(bottleneck.duration_ms, total_ms) if bottleneck is not None else None,
        unattributed_ms=unattributed_ms,
        ack_status=ack_status,
        ack_latency_ms=ack_latency_ms,
        terminal_stage=_terminal_stage(timing),
        video=_video_context(events),
    )


def append_turn_latency_trace(
    trace_store: TraceStore | None,
    *,
    timing: AgentServiceTurnTiming,
    summary: TurnLatencySummary,
) -> bool:
    """Append the safe terminal summary when Assistant correlation exists."""

    if trace_store is None or not timing.trace_id or not timing.assistant_run_id:
        return False
    try:
        trace_store.append(
            TraceEvent(
                trace_id=timing.trace_id,
                run_id=timing.assistant_run_id,
                node_name="agent_service",
                event_type="observability",
                canonical_event="agent_service.turn.finished",
                span_id=new_span_id(),
                status=summary.status,
                latency_ms=summary.total_ms,
                attributes={
                    "delivery_id": timing.delivery_id,
                    "session_turn": timing.session_turn,
                    "chat_index_digest": timing.chat_index_digest,
                    "turn_id": timing.turn_id,
                    "gateway_run_id": timing.gateway_run_id,
                },
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
            "turn_latency status=%s trace=%s gateway_run=%s assistant_run=%s "
            "delivery=%s session_turn=%s total=%s bottleneck=%s bottleneck_ms=%s share=%s",
            summary.status,
            summary.trace_id or "none",
            summary.gateway_run_id or "none",
            summary.assistant_run_id or "none",
            summary.delivery_id,
            summary.session_turn,
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
        if event.canonical_event not in _TOOL_TERMINAL_EVENTS or event.tool_name != "video_understanding":
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


def _share(duration_ms: int, total_ms: int | None) -> float | None:
    if total_ms is None or total_ms <= 0:
        return None
    return min(100.0, round(duration_ms * 100 / total_ms, 1))


def _format_ms(value: int | None) -> str:
    return "none" if value is None else f"{value}ms"


def _format_share(value: float | None) -> str:
    return "none" if value is None else f"{value:.1f}%"
