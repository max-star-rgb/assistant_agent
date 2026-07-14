import logging

import pytest

from assistant_agent.services.agent_service_latency import (
    AgentServiceTurnTiming,
    analyze_agent_service_turn,
    append_turn_latency_trace,
    report_turn_latency,
)
from assistant_agent.services.trace_store import InMemoryTraceStore, TraceEvent


def _event(
    canonical_event: str,
    *,
    latency_ms: int | None = None,
    attributes: dict | None = None,
    output_summary: dict | None = None,
    tool_name: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> TraceEvent:
    return TraceEvent(
        trace_id="trace_1",
        run_id="assistant_run_1",
        node_name="runtime",
        event_type="observability",
        canonical_event=canonical_event,
        latency_ms=latency_ms,
        attributes=attributes or {},
        output_summary=output_summary or {},
        tool_name=tool_name,
        provider=provider,
        model=model,
        status="succeeded",
    )


def _sent_timing(*, total_ms: int = 100, expects_ack: bool = False) -> AgentServiceTurnTiming:
    timing = AgentServiceTurnTiming(
        delivery_id="delivery_1",
        session_turn=3,
        chat_index_digest="digest_1",
        expects_ack=expects_ack,
        received_ns=1_000_000_000,
        accepted_ns=1_004_000_000,
    )
    timing.mark("queue_entered", at_ns=1_005_000_000)
    timing.mark("queue_acquired", at_ns=1_012_000_000)
    timing.mark("gateway_started", at_ns=1_013_000_000)
    timing.mark("gateway_finished", at_ns=1_080_000_000)
    timing.mark("response_built", at_ns=1_081_000_000)
    timing.mark("send_started", at_ns=1_081_000_000)
    timing.mark("send_finished", at_ns=1_000_000_000 + total_ms * 1_000_000)
    timing.bind_turn(
        turn_id="turn_1",
        gateway_run_id="gateway_run_1",
        assistant_run_id="assistant_run_1",
        trace_id="trace_1",
    )
    return timing


def test_turn_timing_computes_transport_durations_without_content() -> None:
    timing = _sent_timing(total_ms=84)

    summary = analyze_agent_service_turn(timing, [], status="sent")

    assert summary.total_ms == 84
    assert summary.stage("entry_parse").duration_ms == 4
    assert summary.stage("chat_queue_wait").duration_ms == 7
    assert summary.stage("websocket_send").duration_ms == 3
    assert summary.ack_status == "not_negotiated"
    assert "speech" not in summary.model_dump_json()


def test_llm_bottleneck_uses_wall_latency_not_provider_latency() -> None:
    events = [
        _event(
            "llm.chat.finished",
            latency_ms=30,
            attributes={"iteration": 2, "provider_latency_ms": 30, "wall_latency_ms": 90},
            provider="qwen",
            model="qwen-plus",
        )
    ]

    summary = analyze_agent_service_turn(_sent_timing(total_ms=100), events, status="sent")

    stage = summary.stage("llm_chat[2]")
    assert stage.duration_ms == 90
    assert stage.provider_latency_ms == 30
    assert stage.provider == "qwen"
    assert stage.model == "qwen-plus"
    assert summary.bottleneck == "llm_chat[2]"


def test_analyzer_extracts_leaf_stages_gateway_overhead_and_unattributed_time() -> None:
    events = [
        _event("conversation.prepare.finished", latency_ms=5),
        _event("memory.load.finished", latency_ms=4),
        _event("context.build.finished", latency_ms=6, attributes={"iteration": 1}),
        _event("action.validation.finished", latency_ms=2, attributes={"iteration": 1}),
        _event("tool.finished", latency_ms=12, tool_name="video_understanding"),
        _event("response.final", latency_ms=3),
        _event("runtime.postprocess.finished", latency_ms=2),
        _event("realtime.backend.finished", latency_ms=60),
    ]

    summary = analyze_agent_service_turn(_sent_timing(total_ms=100), events, status="sent")

    assert summary.stage("conversation_prepare").duration_ms == 5
    assert summary.stage("context_build[1]").duration_ms == 6
    assert summary.stage("tool_execute[video_understanding]").duration_ms == 12
    assert summary.stage("gateway_overhead").duration_ms == 7
    assert summary.unattributed_ms == 29
    assert summary.stage("unattributed").duration_ms == 29
    assert summary.bottleneck == "unattributed"


def test_analyzer_keeps_rolling_video_diagnostics_off_critical_path() -> None:
    events = [
        _event(
            "tool.finished",
            latency_ms=8,
            tool_name="video_understanding",
            output_summary={
                "source": "rolling_memory",
                "snapshot_age_ms": 145,
                "observation_latency_ms": 83,
                "pending_count": 1,
                "in_flight": True,
                "fallback_used": False,
                "snapshot_sequence": 7,
                "provider": "qwen",
                "model": "qwen-vl-max",
                "frame_path": "/secret/frame.jpg",
            },
        )
    ]

    summary = analyze_agent_service_turn(_sent_timing(total_ms=30), events, status="sent")

    assert summary.video is not None
    assert summary.video.source == "rolling_memory"
    assert summary.video.snapshot_age_ms == 145
    assert summary.video.observation_latency_ms == 83
    assert summary.video.pending_count == 1
    assert summary.video.in_flight is True
    assert summary.video.fallback_used is False
    assert summary.video.snapshot_sequence == 7
    assert summary.video.provider == "qwen"
    assert summary.video.model == "qwen-vl-max"
    assert "frame_path" not in summary.model_dump_json()
    assert all(stage.name != "video_observation" for stage in summary.stages)


def test_analyzer_prefers_video_context_consumed_by_llm_over_front_tool_projection() -> None:
    events = [
        _event(
            "context.build.finished",
            latency_ms=2,
            output_summary={
                "context": {
                    "realtime_video": {
                        "present": True,
                        "status": "ready",
                        "snapshot_age_ms": 90,
                        "snapshot_sequence": 9,
                        "observation_latency_ms": 70,
                        "pending_count": 0,
                        "in_flight": False,
                        "provider": "qwen",
                        "model": "qwen-vl-max",
                        "summary": "must not escape",
                    }
                }
            },
        ),
        _event(
            "tool.finished",
            latency_ms=8,
            tool_name="video_understanding",
            output_summary={"source": "front_tool", "snapshot_sequence": 2},
        ),
    ]

    summary = analyze_agent_service_turn(_sent_timing(total_ms=30), events, status="sent")

    assert summary.video is not None
    assert summary.video.source == "realtime_video_context"
    assert summary.video.snapshot_sequence == 9
    assert summary.video.snapshot_age_ms == 90
    assert "must not escape" not in summary.model_dump_json()


def test_negotiated_ack_is_separate_from_primary_turn_latency() -> None:
    timing = _sent_timing(total_ms=100, expects_ack=True)
    timing.mark("ack_received", at_ns=1_125_000_000)

    summary = analyze_agent_service_turn(timing, [], status="sent")

    assert summary.total_ms == 100
    assert summary.ack_status == "acked"
    assert summary.ack_latency_ms == 25


def test_terminal_summary_is_appended_only_with_trace_correlation() -> None:
    store = InMemoryTraceStore()
    timing = _sent_timing(total_ms=100)
    summary = analyze_agent_service_turn(timing, [], status="sent")

    assert append_turn_latency_trace(store, timing=timing, summary=summary) is True
    event = store.list_by_trace("trace_1")[-1]
    assert event.canonical_event == "agent_service.turn.finished"
    assert event.output_summary["turn_latency"]["total_ms"] == 100

    missing = _sent_timing(total_ms=100)
    missing.trace_id = None
    assert append_turn_latency_trace(store, timing=missing, summary=summary) is False
    assert len(store.events) == 1


def test_reporter_emits_one_prompt_safe_line(caplog: pytest.LogCaptureFixture) -> None:
    summary = analyze_agent_service_turn(_sent_timing(total_ms=100), [], status="sent")

    with caplog.at_level(logging.INFO, logger="test.turn_latency"):
        report_turn_latency(summary, logger=logging.getLogger("test.turn_latency"))

    assert len(caplog.records) == 1
    line = caplog.records[0].getMessage()
    assert "turn_latency status=sent" in line
    assert "trace=trace_1" in line
    assert "gateway_run=gateway_run_1" in line
    assert "assistant_run=assistant_run_1" in line
    assert "delivery=delivery_1" in line
    assert "session_turn=3" in line
    assert "digest_1" not in line
