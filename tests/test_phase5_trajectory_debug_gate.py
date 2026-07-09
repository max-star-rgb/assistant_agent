from assistant_agent.services.trace_store import TraceEvent
from assistant_agent.services.trajectory_debug import (
    build_redacted_trajectory_replay,
    evaluate_trajectory_improvement_gate,
)


def test_trajectory_replay_uses_redacted_debug_data_only() -> None:
    events = _sensitive_trace_events()

    replay = build_redacted_trajectory_replay(events)

    assert replay.replay_mode == "debug_replay_eval_only"
    assert replay.raw_data_included is False
    assert replay.production_mutation_allowed is False
    assert replay.timeline[0].canonical_event == "run.started"
    assert replay.timeline[1].canonical_event == "react.decision"
    assert replay.timeline[1].tool_name == "memory_save"
    assert replay.timeline[2].provider == "mock"
    assert replay.timeline[2].error_code == "provider_timeout"
    assert replay.timeline[2].span_id == "span_tool"
    assert replay.timeline[2].parent_span_id == "span_decision"

    serialized = replay.model_dump_json()
    assert "raw user asked for private trip" not in serialized
    assert "private parent memory" not in serialized
    assert "raw provider response body" not in serialized
    assert "Bearer secret-token" not in serialized
    assert "sk-secret" not in serialized
    assert "data:image/png;base64" not in serialized
    assert replay.redaction["raw_payloads_included"] is False
    assert replay.redaction["memory_content_included"] is False
    assert replay.redaction["conversation_history_included"] is False
    assert replay.redaction["media_bodies_included"] is False


def test_memory_improvement_requires_memory_regression_before_manual_review() -> None:
    replay = build_redacted_trajectory_replay(_safe_trace_events())

    report = evaluate_trajectory_improvement_gate(
        replay,
        target="memory",
        memory_regression_passed=False,
        skill_regression_passed=True,
    )

    assert report.manual_review_allowed is False
    assert report.production_mutation_allowed is False
    assert report.auto_apply_allowed is False
    assert report.learning_loop_mode == "debug_replay_eval_only"
    assert "memory_regression_required" in report.blocked_reasons
    assert report.required_regression_suites == ["memory"]


def test_skill_improvement_requires_skill_regression_before_manual_review() -> None:
    replay = build_redacted_trajectory_replay(_safe_trace_events())

    report = evaluate_trajectory_improvement_gate(
        replay,
        target="skill",
        memory_regression_passed=True,
        skill_regression_passed=False,
    )

    assert report.manual_review_allowed is False
    assert report.production_mutation_allowed is False
    assert report.auto_apply_allowed is False
    assert "skill_regression_required" in report.blocked_reasons
    assert report.required_regression_suites == ["skill"]


def test_regression_pass_allows_manual_review_but_never_auto_apply() -> None:
    replay = build_redacted_trajectory_replay(_safe_trace_events())

    report = evaluate_trajectory_improvement_gate(
        replay,
        target="skill",
        memory_regression_passed=True,
        skill_regression_passed=True,
    )

    assert report.manual_review_allowed is True
    assert report.production_mutation_allowed is False
    assert report.auto_apply_allowed is False
    assert report.learning_loop_mode == "debug_replay_eval_only"
    assert report.blocked_reasons == []


def _safe_trace_events() -> list[TraceEvent]:
    return [
        TraceEvent(
            trace_id="trace_phase5",
            run_id="run_phase5",
            user_id="u1",
            session_id="s1",
            node_name="runtime",
            event_type="observability",
            canonical_event="run.started",
            status="started",
            span_id="span_run",
        ),
        TraceEvent(
            trace_id="trace_phase5",
            run_id="run_phase5",
            user_id="u1",
            session_id="s1",
            node_name="native_runtime",
            event_type="assistant_decision",
            canonical_event="react.decision",
            status="tool_call",
            tool_name="memory_save",
            span_id="span_decision",
            parent_span_id="span_run",
            attributes={"decision_type": "tool_call", "tool_call_id": "call_1"},
        ),
        TraceEvent(
            trace_id="trace_phase5",
            run_id="run_phase5",
            user_id="u1",
            session_id="s1",
            node_name="runtime",
            event_type="observability",
            canonical_event="run.completed",
            status="completed",
            span_id="span_done",
            parent_span_id="span_run",
        ),
    ]


def _sensitive_trace_events() -> list[TraceEvent]:
    events = _safe_trace_events()
    events[0].input_summary["text"] = "raw user asked for private trip"
    events[1].attributes.update(
        {
            "decision_type": "tool_call",
            "tool_call_id": "call_1",
            "memory_context_text": "private parent memory",
            "Authorization": "Bearer secret-token",
            "raw_provider_response": "raw provider response body",
        }
    )
    events[1].output_summary.update(
        {
            "output_ref": "local://artifact/1",
            "inline_media": "data:image/png;base64,AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        }
    )
    events.insert(
        2,
        TraceEvent(
            trace_id="trace_phase5",
            run_id="run_phase5",
            user_id="u1",
            session_id="s1",
            node_name="tool_executor",
            event_type="tool_failed",
            canonical_event="tool.failed",
            status="failed",
            tool_name="memory_save",
            provider="mock",
            error_code="provider_timeout",
            span_id="span_tool",
            parent_span_id="span_decision",
            latency_ms=17,
            output_summary={
                "result_count": 0,
                "raw_provider_response": "raw provider response body",
            },
            attributes={
                "recovery_action": "retry_or_report",
                "retry_count": 1,
                "api_key": "sk-secret-12345",
            },
            error={
                "code": "provider_timeout",
                "message": "Authorization: Bearer secret-token sk-secret-12345",
                "raw": "raw provider response body",
            },
        ),
    )
    return events
