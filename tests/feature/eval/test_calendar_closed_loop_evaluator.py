"""Closed-loop Calendar evaluator feature tests."""

from __future__ import annotations

from typing import Any

import pytest

from assistant_agent.eval.contracts import AgentEvalEvidence
from assistant_agent.eval.evaluators.calendar_closed_loop import (
    CalendarClosedLoopCase,
    CalendarEventExpectation,
    evaluate_calendar_closed_loop,
)
from assistant_agent.eval.fixtures.calendar import (
    CalendarEvalEnvironment,
    EvalCalendarEvent,
)
from assistant_agent.schemas.personal_assistant import CalendarCreateRequest
from assistant_agent.schemas.requests import AgentResponse
from assistant_agent.services.trace_store import TraceEvent


EXPECTED_EVENT = {
    "title": "洗牙",
    "start_time": "2026-07-25T15:00:00+08:00",
    "end_time": "2026-07-25T16:00:00+08:00",
    "timezone": None,
    "location": "静安牙科诊所",
    "attendees": [],
    "notes": "提前十分钟到",
}
CASE = CalendarClosedLoopCase(
    id="daily_simple_015_create_dentist_event",
    required_event=CalendarEventExpectation.model_validate(EXPECTED_EVENT),
    forbidden_tools=["calendar_search", "web_search"],
    response_facts=[
        "2026-07-25T15:00:00+08:00",
        "静安牙科诊所",
    ],
)


def _initial_event() -> EvalCalendarEvent:
    return EvalCalendarEvent(
        event_id="existing-team-sync",
        title="团队同步",
        start_time="2026-07-25T10:00:00+08:00",
        end_time="2026-07-25T10:30:00+08:00",
        location="线上",
    )


def _trace_event(
    canonical_event: str,
    *,
    tool_name: str | None = None,
    status: str | None = None,
    call_id: str | None = None,
    input_summary: dict[str, Any] | None = None,
    output_summary: dict[str, Any] | None = None,
    attributes: dict[str, Any] | None = None,
) -> TraceEvent:
    event_attributes = dict(attributes or {})
    if call_id is not None:
        event_attributes["tool_call_id"] = call_id
    return TraceEvent(
        trace_id="0123456789abcdef0123456789abcdef",
        run_id="run-eval-calendar",
        user_id="eval-calendar-user",
        session_id="eval-calendar-session",
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
        attributes=event_attributes,
    )


def _base_trace(
    *,
    tool_input: dict[str, Any] | None = None,
    response_success: bool = True,
) -> list[TraceEvent]:
    call_id = "tool-call-calendar"
    return [
        _trace_event("run.completed", status="completed"),
        _trace_event(
            "action.validation.finished",
            tool_name="calendar_create",
            status="accepted",
        ),
        _trace_event(
            "tool.started",
            tool_name="calendar_create",
            status="started",
            call_id=call_id,
            input_summary={
                **(tool_input or EXPECTED_EVENT),
                "idempotency_key": "runtime-owned-key",
            },
        ),
        _trace_event(
            "tool.finished",
            tool_name="calendar_create",
            status="succeeded",
            call_id=call_id,
            output_summary={"success": response_success},
            attributes={"confirmation_pending": False},
        ),
        _trace_event(
            "tool.observation",
            tool_name="calendar_create",
            status="succeeded",
        ),
        _trace_event("response.final", status="completed"),
        _trace_event("trace.content", status="completed"),
        _trace_event("assistant.turn.summary", status="completed"),
    ]


def _evidence(
    *,
    added: list[dict[str, Any]] | None = None,
    modified: list[dict[str, Any]] | None = None,
    deleted: list[dict[str, Any]] | None = None,
    duplicate_groups: list[list[str]] | None = None,
    trace_events: list[TraceEvent] | None = None,
    response_text: str | None = None,
    confirmed: bool = True,
) -> AgentEvalEvidence:
    event = {
        "event_id": "eval-calendar-0002",
        **EXPECTED_EVENT,
        "idempotency_key": "runtime-owned-key",
    }
    actual_added = [event] if added is None else added
    response = response_text or (
        "已创建洗牙，时间是 2026-07-25T15:00:00+08:00，地点是静安牙科诊所。"
    )
    request_metadata: dict[str, Any] = {}
    if confirmed:
        request_metadata["tool_confirmation"] = {
            "confirmed": True,
            "tool_name": "calendar_create",
        }
    return AgentEvalEvidence(
        case_id=CASE.id,
        run_id="run-eval-calendar",
        trace_id="0123456789abcdef0123456789abcdef",
        terminal_status="completed",
        response=AgentResponse(
            message=response,
            data={"calendar_created": "已创建" in response},
        ),
        trace_events=trace_events or _base_trace(),
        initial_state={"events": [_initial_event().model_dump(mode="json")]},
        final_state={"events": [_initial_event().model_dump(mode="json"), *actual_added]},
        state_diff={
            "added": actual_added,
            "modified": modified or [],
            "deleted": deleted or [],
            "duplicate_groups": duplicate_groups or [],
        },
        runtime_metadata={
            "available_tool_names": ["calendar_create"],
            "request_metadata": request_metadata,
        },
    )


def test_correct_creation_strict_passes() -> None:
    report = evaluate_calendar_closed_loop(CASE, _evidence())

    assert report.score("agent.strict_pass").value == 1.0
    assert report.score("agent.goal_completion").value == 1.0
    assert report.score("agent.policy_compliance").value == 1.0
    assert report.score("agent.tool_call_count").value == 1.0


@pytest.mark.parametrize(
    ("variant", "evidence", "failed_score"),
    [
        (
            "wrong_start_time",
            _evidence(
                added=[
                    {
                        "event_id": "eval-calendar-0002",
                        **{
                            **EXPECTED_EVENT,
                            "start_time": "2026-07-25T16:00:00+08:00",
                        },
                        "idempotency_key": "runtime-owned-key",
                    }
                ],
                trace_events=_base_trace(
                    tool_input={
                        **EXPECTED_EVENT,
                        "start_time": "2026-07-25T16:00:00+08:00",
                    }
                ),
            ),
            "agent.goal_completion",
        ),
        (
            "missing_confirmation",
            _evidence(
                confirmed=False,
                trace_events=[
                    event.model_copy(
                        update={"attributes": {**event.attributes, "confirmation_pending": True}}
                    )
                    if event.canonical_event == "tool.finished"
                    else event
                    for event in _base_trace()
                ],
            ),
            "agent.policy_compliance",
        ),
        (
            "duplicate_creation",
            _evidence(
                added=[
                    {
                        "event_id": event_id,
                        **EXPECTED_EVENT,
                        "idempotency_key": key,
                    }
                    for event_id, key in (
                        ("eval-calendar-0002", "key-one"),
                        ("eval-calendar-0003", "key-two"),
                    )
                ],
                duplicate_groups=[
                    ["eval-calendar-0002", "eval-calendar-0003"]
                ],
                trace_events=[
                    *_base_trace(),
                    _trace_event(
                        "tool.started",
                        tool_name="calendar_create",
                        status="started",
                        call_id="tool-call-calendar-2",
                        input_summary={
                            **EXPECTED_EVENT,
                            "idempotency_key": "key-two",
                        },
                    ),
                    _trace_event(
                        "tool.finished",
                        tool_name="calendar_create",
                        status="succeeded",
                        call_id="tool-call-calendar-2",
                        output_summary={"success": True},
                        attributes={"confirmation_pending": False},
                    ),
                ],
            ),
            "agent.state_integrity",
        ),
        (
            "existing_event_modified",
            _evidence(
                modified=[
                    {
                        "event_id": "existing-team-sync",
                        "before": _initial_event().model_dump(mode="json"),
                        "after": {
                            **_initial_event().model_dump(mode="json"),
                            "location": "被错误修改",
                        },
                    }
                ],
            ),
            "agent.state_integrity",
        ),
        (
            "tool_failed_but_claimed_created",
            _evidence(
                added=[],
                trace_events=[
                    event.model_copy(
                        update={
                            "canonical_event": "tool.failed",
                            "status": "failed",
                            "output_summary": {"success": False},
                        }
                    )
                    if event.canonical_event == "tool.finished"
                    else event
                    for event in _base_trace(response_success=False)
                ],
            ),
            "agent.response_grounding",
        ),
        (
            "wrong_response_location",
            _evidence(
                response_text=(
                    "已创建洗牙，时间是 2026-07-25T15:00:00+08:00，"
                    "地点是错误诊所。"
                )
            ),
            "agent.response_grounding",
        ),
        (
            "forbidden_web_search",
            _evidence(
                trace_events=[
                    *_base_trace(),
                    _trace_event(
                        "tool.started",
                        tool_name="web_search",
                        status="started",
                        call_id="tool-call-web",
                        input_summary={"query": "牙科"},
                    ),
                    _trace_event(
                        "tool.finished",
                        tool_name="web_search",
                        status="succeeded",
                        call_id="tool-call-web",
                        output_summary={"success": True},
                    ),
                ]
            ),
            "agent.tool_correctness",
        ),
    ],
)
def test_failure_variants_are_rejected(
    variant: str,
    evidence: AgentEvalEvidence,
    failed_score: str,
) -> None:
    report = evaluate_calendar_closed_loop(CASE, evidence)

    assert report.score("agent.strict_pass").value == 0.0, variant
    assert report.score(failed_score).value < 1.0, variant


def test_calendar_environment_preserves_full_state_and_idempotency() -> None:
    environment = CalendarEvalEnvironment([_initial_event()])
    before = environment.snapshot()
    request = CalendarCreateRequest(
        **EXPECTED_EVENT,
        idempotency_key="stable-runtime-key",
    )

    first = environment.create(request)
    second = environment.create(request)
    after = environment.snapshot()
    diff = environment.diff(before, after)

    assert first.event_id == second.event_id
    assert len(diff["added"]) == 1
    assert diff["added"][0]["location"] == "静安牙科诊所"
    assert diff["added"][0]["notes"] == "提前十分钟到"
    assert diff["modified"] == []
    assert diff["deleted"] == []
    assert diff["duplicate_groups"] == []
