"""Failure-detection tests for no-tool and read-only capability evaluators."""

from __future__ import annotations

from typing import Any

from assistant_agent.eval.contracts import AgentEvalEvidence
from assistant_agent.eval.evaluators.calendar_closed_loop import (
    CalendarEventExpectation,
)
from assistant_agent.eval.evaluators.capability_closed_loop import (
    CalendarReadClosedLoopCase,
    NoToolClosedLoopCase,
    evaluate_calendar_read_closed_loop,
    evaluate_no_tool_closed_loop,
)
from assistant_agent.schemas.requests import AgentResponse
from assistant_agent.services.trace_store import TraceEvent


INITIAL_EVENT = {
    "event_id": "existing-team-sync",
    "title": "团队同步",
    "start_time": "2026-07-25T10:00:00+08:00",
    "end_time": "2026-07-25T10:30:00+08:00",
    "timezone": None,
    "location": "线上",
    "attendees": [],
    "notes": None,
    "idempotency_key": None,
}
INITIAL_STATE = {
    "schema_version": "calendar_eval_state_v1",
    "events": [INITIAL_EVENT],
}
EMPTY_DIFF = {
    "schema_version": "calendar_eval_state_diff_v1",
    "added": [],
    "modified": [],
    "deleted": [],
    "duplicate_groups": [],
}
NO_TOOL_CASE = NoToolClosedLoopCase(
    id="no-tool-case",
    forbidden_tools=["calendar_search", "calendar_create"],
    response_facts=["已收到"],
)
READ_CASE = CalendarReadClosedLoopCase(
    id="read-case",
    query="团队同步",
    expected_events=[
        CalendarEventExpectation(
            title="团队同步",
            start_time="2026-07-25T10:00:00+08:00",
            end_time="2026-07-25T10:30:00+08:00",
            location="线上",
        )
    ],
    response_facts=[
        "团队同步",
        "2026-07-25T10:00:00+08:00",
        "线上",
    ],
)


def _event(
    canonical_event: str,
    *,
    tool_name: str | None = None,
    status: str | None = None,
    input_summary: dict[str, Any] | None = None,
    output_summary: dict[str, Any] | None = None,
    attributes: dict[str, Any] | None = None,
) -> TraceEvent:
    return TraceEvent(
        trace_id="0123456789abcdef0123456789abcdef",
        run_id="run-capability-eval",
        node_name="eval",
        event_type=(
            "tool_observation"
            if canonical_event == "tool.observation"
            else "observability"
        ),
        canonical_event=canonical_event,
        tool_name=tool_name,
        status=status,
        input_summary=input_summary or {},
        output_summary=output_summary or {},
        attributes=attributes or {},
    )


def _common_trace() -> list[TraceEvent]:
    return [
        _event("run.started", status="started"),
        _event("response.final", status="succeeded"),
        _event("run.completed", status="completed"),
        _event("trace.content", status="completed"),
        _event("assistant.turn.summary", status="completed"),
    ]


def _read_trace(*, query: str = "团队同步") -> list[TraceEvent]:
    return [
        _event("run.started", status="started"),
        _event(
            "action.validation.finished",
            tool_name="calendar_search",
            status="accepted",
        ),
        _event(
            "tool.started",
            tool_name="calendar_search",
            status="started",
            input_summary={"query": query, "limit": 5},
            attributes={"tool_category": "read"},
        ),
        _event(
            "tool.finished",
            tool_name="calendar_search",
            status="succeeded",
            input_summary={"query": query, "limit": 5},
            output_summary={
                "success": True,
                "data": {
                    "query_used": query,
                    "events": [
                        {
                            "event_id": "existing-team-sync",
                            "title": "团队同步",
                            "start_time": "2026-07-25T10:00:00+08:00",
                            "end_time": "2026-07-25T10:30:00+08:00",
                            "timezone": None,
                            "location": "线上",
                            "attendee_count": 0,
                        }
                    ],
                },
            },
            attributes={
                "tool_category": "read",
                "confirmation_pending": False,
            },
        ),
        _event(
            "tool.observation",
            tool_name="calendar_search",
            status="succeeded",
        ),
        *_common_trace(),
    ]


def _evidence(
    *,
    case_id: str,
    response: str,
    trace_events: list[TraceEvent],
    final_state: dict[str, Any] | None = None,
    state_diff: dict[str, Any] | None = None,
) -> AgentEvalEvidence:
    return AgentEvalEvidence(
        case_id=case_id,
        run_id="run-capability-eval",
        trace_id="0123456789abcdef0123456789abcdef",
        terminal_status="completed",
        response=AgentResponse(message=response),
        trace_events=trace_events,
        initial_state=INITIAL_STATE,
        final_state=final_state or INITIAL_STATE,
        state_diff=state_diff or EMPTY_DIFF,
    )


def test_no_tool_case_strict_passes() -> None:
    report = evaluate_no_tool_closed_loop(
        NO_TOOL_CASE,
        _evidence(
            case_id=NO_TOOL_CASE.id,
            response="已收到",
            trace_events=_common_trace(),
        ),
    )

    assert report.score("agent.strict_pass").value == 1.0
    assert report.score("agent.tool_call_count").value == 0.0


def test_no_tool_evaluator_detects_unnecessary_tool_call() -> None:
    report = evaluate_no_tool_closed_loop(
        NO_TOOL_CASE,
        _evidence(
            case_id=NO_TOOL_CASE.id,
            response="已收到",
            trace_events=[
                _event(
                    "tool.started",
                    tool_name="calendar_search",
                    status="started",
                ),
                _event(
                    "tool.finished",
                    tool_name="calendar_search",
                    status="succeeded",
                ),
                *_common_trace(),
            ],
        ),
    )

    assert report.score("agent.tool_correctness").value == 0.0
    assert report.score("agent.policy_compliance").value == 0.0
    assert report.score("agent.strict_pass").value == 0.0


def test_no_tool_evaluator_detects_state_pollution() -> None:
    added = {
        **INITIAL_EVENT,
        "event_id": "polluted-event",
        "title": "不应创建",
    }
    report = evaluate_no_tool_closed_loop(
        NO_TOOL_CASE,
        _evidence(
            case_id=NO_TOOL_CASE.id,
            response="已收到",
            trace_events=_common_trace(),
            final_state={
                **INITIAL_STATE,
                "events": [INITIAL_EVENT, added],
            },
            state_diff={**EMPTY_DIFF, "added": [added]},
        ),
    )

    assert report.score("agent.state_integrity").value == 0.0
    assert report.score("agent.strict_pass").value == 0.0


def test_calendar_read_case_strict_passes() -> None:
    report = evaluate_calendar_read_closed_loop(
        READ_CASE,
        _evidence(
            case_id=READ_CASE.id,
            response="团队同步在 2026-07-25T10:00:00+08:00，地点线上。",
            trace_events=_read_trace(),
        ),
    )

    assert report.score("agent.strict_pass").value == 1.0
    assert report.score("agent.tool_call_count").value == 1.0


def test_calendar_read_evaluator_detects_wrong_query() -> None:
    report = evaluate_calendar_read_closed_loop(
        READ_CASE,
        _evidence(
            case_id=READ_CASE.id,
            response="团队同步在 2026-07-25T10:00:00+08:00，地点线上。",
            trace_events=_read_trace(query="洗牙"),
        ),
    )

    assert report.score("agent.tool_correctness").value < 1.0
    assert report.score("agent.strict_pass").value == 0.0


def test_calendar_read_evaluator_detects_write_and_state_change() -> None:
    added = {
        **INITIAL_EVENT,
        "event_id": "unexpected-write",
        "title": "错误写入",
    }
    trace = _read_trace()
    trace.insert(
        5,
        _event(
            "tool.started",
            tool_name="calendar_create",
            status="started",
            attributes={"tool_category": "write"},
        ),
    )
    trace.insert(
        6,
        _event(
            "tool.finished",
            tool_name="calendar_create",
            status="succeeded",
            attributes={
                "tool_category": "write",
                "confirmation_pending": False,
            },
        ),
    )
    report = evaluate_calendar_read_closed_loop(
        READ_CASE,
        _evidence(
            case_id=READ_CASE.id,
            response="团队同步在 2026-07-25T10:00:00+08:00，地点线上。",
            trace_events=trace,
            final_state={**INITIAL_STATE, "events": [INITIAL_EVENT, added]},
            state_diff={**EMPTY_DIFF, "added": [added]},
        ),
    )

    assert report.score("agent.policy_compliance").value < 1.0
    assert report.score("agent.state_integrity").value == 0.0
    assert report.score("agent.strict_pass").value == 0.0
