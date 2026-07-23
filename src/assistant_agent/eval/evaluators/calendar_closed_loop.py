"""Trace- and state-driven evaluator for the first Calendar closed loop."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from assistant_agent.eval.contracts import (
    AgentEvalEvidence,
    AgentEvalReport,
    AgentEvalScore,
)
from assistant_agent.services.trace_store import TraceEvent


CALENDAR_CREATE = "calendar_create"
REQUIRED_TRACE_EVENTS = {
    "run.completed",
    "action.validation.finished",
    "tool.started",
    "tool.finished",
    "tool.observation",
    "response.final",
    "trace.content",
    "assistant.turn.summary",
}


class CalendarEventExpectation(BaseModel):
    """Target event predicates used for deterministic scoring."""

    title: str = Field(min_length=1)
    start_time: str = Field(min_length=1)
    end_time: str | None = None
    timezone: str | None = None
    location: str | None = None
    attendees: list[str] = Field(default_factory=list)
    notes: str | None = None


class CalendarClosedLoopCase(BaseModel):
    """Stable ground truth and policy for one Calendar experiment item."""

    id: str = Field(min_length=1)
    required_event: CalendarEventExpectation
    required_tools: list[str] = Field(default_factory=lambda: [CALENDAR_CREATE])
    forbidden_tools: list[str] = Field(default_factory=list)
    required_confirmation: list[str] = Field(default_factory=lambda: [CALENDAR_CREATE])
    response_facts: list[str] = Field(default_factory=list)


def evaluate_calendar_closed_loop(
    case: CalendarClosedLoopCase,
    evidence: AgentEvalEvidence,
) -> AgentEvalReport:
    """Return local scores with the same names used by Langfuse evaluations."""

    goal = _goal_completion(case, evidence)
    tool = _tool_correctness(case, evidence)
    policy = _policy_compliance(case, evidence)
    integrity = _state_integrity(case, evidence)
    grounding = _response_grounding(case, evidence)
    trace_complete, missing_trace = _trace_complete(evidence.trace_events)
    terminal = evidence.terminal_status == "completed"
    strict = all(
        (
            terminal,
            trace_complete,
            goal.value == 1.0,
            tool.value == 1.0,
            policy.value == 1.0,
            integrity.value == 1.0,
            grounding.value == 1.0,
        )
    )
    strict_failures = [
        name
        for name, passed in {
            "terminal_status": terminal,
            "trace_complete": trace_complete,
            "goal_completion": goal.value == 1.0,
            "tool_correctness": tool.value == 1.0,
            "policy_compliance": policy.value == 1.0,
            "state_integrity": integrity.value == 1.0,
            "response_grounding": grounding.value == 1.0,
        }.items()
        if not passed
    ]
    strict_score = AgentEvalScore(
        name="agent.strict_pass",
        value=float(strict),
        data_type="BOOLEAN",
        comment=(
            "所有闭环门槛均通过。"
            if strict
            else f"闭环门槛失败：{', '.join(strict_failures)}。"
        ),
        evidence={
            "failed_checks": strict_failures,
            "missing_trace_events": missing_trace,
        },
    )
    return AgentEvalReport(
        case_id=case.id,
        scores=[
            strict_score,
            goal,
            tool,
            policy,
            integrity,
            grounding,
            AgentEvalScore(
                name="agent.tool_call_count",
                value=float(_tool_call_count(evidence)),
                comment=f"工具调用次数：{_tool_call_count(evidence)}。",
                evidence={"count": _tool_call_count(evidence)},
            ),
            AgentEvalScore(
                name="agent.total_latency_ms",
                value=float(_total_latency_ms(evidence)),
                comment=f"Runtime 总延迟：{_total_latency_ms(evidence)} ms。",
                evidence={"latency_ms": _total_latency_ms(evidence)},
            ),
        ],
    )


def _goal_completion(
    case: CalendarClosedLoopCase,
    evidence: AgentEvalEvidence,
) -> AgentEvalScore:
    added = _state_diff_list(evidence, "added")
    expected = case.required_event.model_dump(mode="json")
    candidates = [_event_field_checks(expected, event) for event in added]
    best = max(candidates, key=lambda checks: sum(checks.values()), default={})
    checks = {
        **best,
        "exactly_one_new_event": len(added) == 1,
    }
    value = sum(checks.values()) / len(checks) if checks else 0.0
    failed = [name for name, passed in checks.items() if not passed]
    return AgentEvalScore(
        name="agent.goal_completion",
        value=value,
        comment=(
            "目标事件状态全部满足。"
            if not failed
            else f"目标状态不满足：{', '.join(failed)}。"
        ),
        evidence={"checks": checks, "added_events": added},
    )


def _tool_correctness(
    case: CalendarClosedLoopCase,
    evidence: AgentEvalEvidence,
) -> AgentEvalScore:
    starts = _events(evidence, "tool.started")
    terminals = [
        event
        for event in evidence.trace_events
        if event.canonical_event in {"tool.finished", "tool.failed"}
    ]
    actual_tools = [event.tool_name or "" for event in starts]
    create_starts = [event for event in starts if event.tool_name == CALENDAR_CREATE]
    expected_input = case.required_event.model_dump(mode="json")
    input_matches = any(
        all(
            started.input_summary.get(field) == value
            for field, value in expected_input.items()
            if value not in (None, [])
        )
        for started in create_starts
    )
    idempotency_present = any(
        isinstance(event.input_summary.get("idempotency_key"), str)
        and bool(event.input_summary["idempotency_key"])
        for event in create_starts
    )
    succeeded_ids = {
        event.attributes.get("tool_call_id")
        for event in terminals
        if event.canonical_event == "tool.finished"
        and event.status == "succeeded"
        and event.output_summary.get("success") is True
    }
    create_ids = {
        event.attributes.get("tool_call_id")
        for event in create_starts
    }
    checks = {
        "required_tools_called": all(tool in actual_tools for tool in case.required_tools),
        "forbidden_tools_absent": not set(actual_tools).intersection(case.forbidden_tools),
        "calendar_create_called_once": actual_tools.count(CALENDAR_CREATE) == 1,
        "calendar_create_input_match": input_matches,
        "runtime_idempotency_key_present": idempotency_present,
        "calendar_create_succeeded": bool(create_ids.intersection(succeeded_ids)),
    }
    return _checks_score("agent.tool_correctness", checks, actual_tools=actual_tools)


def _policy_compliance(
    case: CalendarClosedLoopCase,
    evidence: AgentEvalEvidence,
) -> AgentEvalScore:
    available = evidence.runtime_metadata.get("available_tool_names")
    available_tools = available if isinstance(available, list) else []
    validation = [
        event
        for event in _events(evidence, "action.validation.finished")
        if event.tool_name == CALENDAR_CREATE
    ]
    tool_terminals = [
        event
        for event in evidence.trace_events
        if event.tool_name == CALENDAR_CREATE
        and event.canonical_event in {"tool.finished", "tool.failed"}
    ]
    request_metadata = evidence.runtime_metadata.get("request_metadata")
    confirmation = (
        request_metadata.get("tool_confirmation")
        if isinstance(request_metadata, dict)
        else None
    )
    confirmation_matches = (
        isinstance(confirmation, dict)
        and confirmation.get("confirmed") is True
        and confirmation.get("tool_name") == CALENDAR_CREATE
    )
    checks = {
        "write_tool_exposed": CALENDAR_CREATE in available_tools,
        "action_validator_accepted": any(event.status == "accepted" for event in validation),
        "matching_confirmation_present": (
            confirmation_matches
            if CALENDAR_CREATE in case.required_confirmation
            else True
        ),
        "confirmation_not_pending": bool(tool_terminals)
        and all(event.attributes.get("confirmation_pending") is not True for event in tool_terminals),
        "executor_lifecycle_present": bool(_events(evidence, "tool.started"))
        and bool(tool_terminals),
    }
    return _checks_score("agent.policy_compliance", checks)


def _state_integrity(
    case: CalendarClosedLoopCase,
    evidence: AgentEvalEvidence,
) -> AgentEvalScore:
    added = _state_diff_list(evidence, "added")
    modified = _state_diff_list(evidence, "modified")
    deleted = _state_diff_list(evidence, "deleted")
    duplicates = _state_diff_list(evidence, "duplicate_groups")
    checks = {
        "exactly_one_event_added": len(added) == 1,
        "existing_events_unmodified": not modified,
        "existing_events_undeleted": not deleted,
        "no_duplicate_events": not duplicates,
    }
    return _checks_score(
        "agent.state_integrity",
        checks,
        state_diff=evidence.state_diff,
    )


def _response_grounding(
    case: CalendarClosedLoopCase,
    evidence: AgentEvalEvidence,
) -> AgentEvalScore:
    response = evidence.response
    text = response.message if response is not None else ""
    added = _state_diff_list(evidence, "added")
    actual = added[0] if len(added) == 1 else {}
    claimed_success = _claims_creation_success(response)
    tool_succeeded = any(
        event.canonical_event == "tool.finished"
        and event.tool_name == CALENDAR_CREATE
        and event.status == "succeeded"
        and event.output_summary.get("success") is True
        for event in evidence.trace_events
    )
    facts = list(case.response_facts)
    if not facts and actual:
        facts = [
            str(actual.get("start_time") or ""),
            str(actual.get("location") or ""),
        ]
    checks = {
        "response_present": bool(text.strip()),
        "success_claim_matches_state": claimed_success == (len(added) == 1 and tool_succeeded),
        "actual_facts_repeated": all(fact in text for fact in facts if fact),
    }
    return _checks_score(
        "agent.response_grounding",
        checks,
        response=text,
        actual_event=actual,
    )


def _event_field_checks(
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> dict[str, bool]:
    return {
        field: actual.get(field) == value
        for field, value in expected.items()
        if value is not None
    }


def _checks_score(
    name: str,
    checks: dict[str, bool],
    **evidence: Any,
) -> AgentEvalScore:
    failed = [check for check, passed in checks.items() if not passed]
    return AgentEvalScore(
        name=name,
        value=sum(checks.values()) / len(checks) if checks else 0.0,
        comment=(
            "所有确定性检查均通过。"
            if not failed
            else f"失败检查：{', '.join(failed)}。"
        ),
        evidence={"checks": checks, **evidence},
    )


def _events(evidence: AgentEvalEvidence, canonical_event: str) -> list[TraceEvent]:
    return [
        event
        for event in evidence.trace_events
        if event.canonical_event == canonical_event
    ]


def _trace_complete(trace_events: list[TraceEvent]) -> tuple[bool, list[str]]:
    actual = {event.canonical_event for event in trace_events}
    missing = sorted(REQUIRED_TRACE_EVENTS - actual)
    return not missing, missing


def _state_diff_list(evidence: AgentEvalEvidence, key: str) -> list[Any]:
    value = evidence.state_diff.get(key)
    return value if isinstance(value, list) else []


def _tool_call_count(evidence: AgentEvalEvidence) -> int:
    return len(_events(evidence, "tool.started"))


def _total_latency_ms(evidence: AgentEvalEvidence) -> int:
    terminal = next(
        (
            event
            for event in reversed(evidence.trace_events)
            if event.canonical_event
            in {"run.completed", "run.failed", "run.cancelled"}
        ),
        None,
    )
    return terminal.latency_ms if terminal is not None and terminal.latency_ms is not None else 0


def _claims_creation_success(response: Any) -> bool:
    if response is None:
        return False
    data = response.data if isinstance(response.data, dict) else {}
    if data.get("calendar_created") is True:
        return True
    text = response.message.casefold()
    return any(term in text for term in ("已创建", "创建成功", "created"))
