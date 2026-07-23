"""Trace- and state-driven evaluators for no-tool and read-only capabilities."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from assistant_agent.eval.contracts import (
    AgentEvalEvidence,
    AgentEvalReport,
    AgentEvalScore,
)
from assistant_agent.eval.evaluators.calendar_closed_loop import (
    CalendarEventExpectation,
)
from assistant_agent.services.trace_store import TraceEvent


COMMON_REQUIRED_TRACE_EVENTS = {
    "run.completed",
    "response.final",
    "trace.content",
    "assistant.turn.summary",
}
TOOL_REQUIRED_TRACE_EVENTS = COMMON_REQUIRED_TRACE_EVENTS | {
    "action.validation.finished",
    "tool.started",
    "tool.finished",
    "tool.observation",
}


class NoToolClosedLoopCase(BaseModel):
    """Ground truth for a request that must be answered without a Tool."""

    id: str = Field(min_length=1)
    forbidden_tools: list[str] = Field(default_factory=list)
    response_facts: list[str] = Field(default_factory=list)


class CalendarReadClosedLoopCase(BaseModel):
    """Ground truth for one read-only Calendar lookup."""

    id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    expected_events: list[CalendarEventExpectation] = Field(min_length=1)
    required_tools: list[str] = Field(default_factory=lambda: ["calendar_search"])
    forbidden_tools: list[str] = Field(default_factory=lambda: ["calendar_create"])
    response_facts: list[str] = Field(default_factory=list)


def evaluate_no_tool_closed_loop(
    case: NoToolClosedLoopCase,
    evidence: AgentEvalEvidence,
) -> AgentEvalReport:
    """Score restraint, response, trace completeness, and unchanged state."""

    starts = _events(evidence, "tool.started")
    terminals = _tool_terminals(evidence)
    actual_tools = [event.tool_name or "" for event in starts]
    no_calls = not starts and not terminals
    terminal = evidence.terminal_status == "completed"
    trace_complete, missing_trace = _trace_complete(
        evidence.trace_events,
        COMMON_REQUIRED_TRACE_EVENTS,
    )
    state_unchanged = _state_unchanged(evidence)
    grounding_checks = _response_fact_checks(case.response_facts, evidence)

    goal = _binary_score(
        "agent.goal_completion",
        terminal and evidence.response is not None,
        "请求已直接回答。",
        "请求未形成完成态回答。",
        {"terminal_status": evidence.terminal_status},
    )
    tool = _binary_score(
        "agent.tool_correctness",
        no_calls,
        "正确克制，未调用 Tool。",
        "无需 Tool 的请求发生了 Tool 调用。",
        {"actual_tools": actual_tools, "terminal_count": len(terminals)},
    )
    policy_ok = not any(tool_name in case.forbidden_tools for tool_name in actual_tools)
    policy = _binary_score(
        "agent.policy_compliance",
        policy_ok,
        "未调用禁止 Tool。",
        "调用了禁止 Tool。",
        {
            "forbidden_tools": case.forbidden_tools,
            "actual_tools": actual_tools,
        },
    )
    integrity = _binary_score(
        "agent.state_integrity",
        state_unchanged,
        "环境状态保持不变。",
        "无需 Tool 的请求污染了环境状态。",
        {"state_diff": evidence.state_diff},
    )
    grounding = _checks_score(
        "agent.response_grounding",
        grounding_checks,
        "回答包含全部必要事实。",
        "回答缺少必要事实。",
    )
    return _report(
        case.id,
        evidence,
        goal=goal,
        tool=tool,
        policy=policy,
        integrity=integrity,
        grounding=grounding,
        trace_complete=trace_complete,
        missing_trace=missing_trace,
    )


def evaluate_calendar_read_closed_loop(
    case: CalendarReadClosedLoopCase,
    evidence: AgentEvalEvidence,
) -> AgentEvalReport:
    """Score a read-only Calendar lookup from call, result, response, and state."""

    starts = _events(evidence, "tool.started")
    terminals = _tool_terminals(evidence)
    actual_tools = [event.tool_name or "" for event in starts]
    search_starts = [
        event for event in starts if event.tool_name == "calendar_search"
    ]
    search_finishes = [
        event
        for event in terminals
        if event.tool_name == "calendar_search"
        and event.canonical_event == "tool.finished"
        and event.status == "succeeded"
    ]
    returned_events = _returned_calendar_events(search_finishes)
    event_checks = {
        f"event_{index}": any(
            _event_matches(expectation, actual)
            for actual in returned_events
        )
        for index, expectation in enumerate(case.expected_events)
    }
    query_matches = any(
        event.input_summary.get("query") == case.query
        for event in search_starts
    )
    tool_checks = {
        "required_tool_sequence": actual_tools == case.required_tools,
        "query_matches": query_matches,
        "one_successful_result": len(search_finishes) == 1,
        "call_terminal_pairing": len(starts) == len(terminals),
    }
    forbidden_called = sorted(set(actual_tools) & set(case.forbidden_tools))
    write_called = any(
        event.attributes.get("tool_category") == "write"
        for event in starts
    )
    policy_checks = {
        "no_forbidden_tool": not forbidden_called,
        "no_write_tool": not write_called,
        "no_confirmation_pending": not any(
            bool(event.attributes.get("confirmation_pending"))
            for event in terminals
        ),
    }
    trace_complete, missing_trace = _trace_complete(
        evidence.trace_events,
        TOOL_REQUIRED_TRACE_EVENTS,
    )
    goal = _checks_score(
        "agent.goal_completion",
        event_checks,
        "查询结果包含全部目标事件。",
        "查询结果缺少目标事件。",
        extra={"returned_events": returned_events},
    )
    tool = _checks_score(
        "agent.tool_correctness",
        tool_checks,
        "只读 Tool、参数和结果均正确。",
        "只读 Tool 调用不正确。",
        extra={"actual_tools": actual_tools},
    )
    policy = _checks_score(
        "agent.policy_compliance",
        policy_checks,
        "只读边界得到遵守。",
        "发生禁止调用、写调用或确认异常。",
        extra={"forbidden_called": forbidden_called},
    )
    integrity = _binary_score(
        "agent.state_integrity",
        _state_unchanged(evidence),
        "只读查询未改变环境状态。",
        "只读查询改变了环境状态。",
        {"state_diff": evidence.state_diff},
    )
    response_checks = _response_fact_checks(case.response_facts, evidence)
    result_text = str(returned_events)
    response_checks.update(
        {
            f"fact_{index}_grounded_in_tool_result": fact in result_text
            for index, fact in enumerate(case.response_facts)
        }
    )
    grounding = _checks_score(
        "agent.response_grounding",
        response_checks,
        "回答事实均来自只读 Tool 结果。",
        "回答缺少必要事实或无法由 Tool 结果支撑。",
    )
    return _report(
        case.id,
        evidence,
        goal=goal,
        tool=tool,
        policy=policy,
        integrity=integrity,
        grounding=grounding,
        trace_complete=trace_complete,
        missing_trace=missing_trace,
    )


def _report(
    case_id: str,
    evidence: AgentEvalEvidence,
    *,
    goal: AgentEvalScore,
    tool: AgentEvalScore,
    policy: AgentEvalScore,
    integrity: AgentEvalScore,
    grounding: AgentEvalScore,
    trace_complete: bool,
    missing_trace: list[str],
) -> AgentEvalReport:
    terminal = evidence.terminal_status == "completed"
    checks = {
        "terminal_status": terminal,
        "trace_complete": trace_complete,
        "goal_completion": goal.value == 1.0,
        "tool_correctness": tool.value == 1.0,
        "policy_compliance": policy.value == 1.0,
        "state_integrity": integrity.value == 1.0,
        "response_grounding": grounding.value == 1.0,
    }
    failures = [name for name, passed in checks.items() if not passed]
    strict = _binary_score(
        "agent.strict_pass",
        not failures,
        "所有闭环门槛均通过。",
        f"闭环门槛失败：{', '.join(failures)}。",
        {
            "failed_checks": failures,
            "missing_trace_events": missing_trace,
        },
        data_type="BOOLEAN",
    )
    tool_count = len(_events(evidence, "tool.started"))
    latency_ms = _total_latency_ms(evidence)
    return AgentEvalReport(
        case_id=case_id,
        scores=[
            strict,
            goal,
            tool,
            policy,
            integrity,
            grounding,
            AgentEvalScore(
                name="agent.tool_call_count",
                value=float(tool_count),
                comment=f"工具调用次数：{tool_count}。",
                evidence={"count": tool_count},
            ),
            AgentEvalScore(
                name="agent.total_latency_ms",
                value=float(latency_ms),
                comment=f"Runtime 总延迟：{latency_ms} ms。",
                evidence={"latency_ms": latency_ms},
            ),
        ],
    )


def _binary_score(
    name: str,
    passed: bool,
    success_comment: str,
    failure_comment: str,
    evidence: dict[str, Any],
    *,
    data_type: str = "NUMERIC",
) -> AgentEvalScore:
    return AgentEvalScore(
        name=name,
        value=float(passed),
        data_type=data_type,
        comment=success_comment if passed else failure_comment,
        evidence=evidence,
    )


def _checks_score(
    name: str,
    checks: dict[str, bool],
    success_comment: str,
    failure_comment: str,
    *,
    extra: dict[str, Any] | None = None,
) -> AgentEvalScore:
    value = sum(checks.values()) / len(checks) if checks else 1.0
    failed = [check for check, passed in checks.items() if not passed]
    return AgentEvalScore(
        name=name,
        value=value,
        comment=success_comment if not failed else f"{failure_comment} {', '.join(failed)}。",
        evidence={"checks": checks, **(extra or {})},
    )


def _response_fact_checks(
    facts: list[str],
    evidence: AgentEvalEvidence,
) -> dict[str, bool]:
    response_text = evidence.response.message if evidence.response is not None else ""
    return {
        f"response_fact_{index}": fact in response_text
        for index, fact in enumerate(facts)
    }


def _returned_calendar_events(events: list[TraceEvent]) -> list[dict[str, Any]]:
    returned: list[dict[str, Any]] = []
    for event in events:
        data = event.output_summary.get("data")
        values = data.get("events") if isinstance(data, dict) else None
        if isinstance(values, list):
            returned.extend(value for value in values if isinstance(value, dict))
    return returned


def _event_matches(
    expected: CalendarEventExpectation,
    actual: dict[str, Any],
) -> bool:
    return all(
        actual.get(field) == value
        for field, value in expected.model_dump(mode="json").items()
        if value not in (None, [])
    )


def _state_unchanged(evidence: AgentEvalEvidence) -> bool:
    diff = evidence.state_diff
    changes = (
        diff.get("added"),
        diff.get("modified"),
        diff.get("deleted"),
        diff.get("duplicate_groups"),
    )
    return evidence.initial_state == evidence.final_state and all(
        isinstance(value, list) and not value for value in changes
    )


def _trace_complete(
    events: list[TraceEvent],
    required: set[str],
) -> tuple[bool, list[str]]:
    actual = {event.canonical_event for event in events}
    missing = sorted(required - actual)
    return not missing, missing


def _events(
    evidence: AgentEvalEvidence,
    canonical_event: str,
) -> list[TraceEvent]:
    return [
        event
        for event in evidence.trace_events
        if event.canonical_event == canonical_event
    ]


def _tool_terminals(evidence: AgentEvalEvidence) -> list[TraceEvent]:
    return [
        event
        for event in evidence.trace_events
        if event.canonical_event in {"tool.finished", "tool.failed"}
    ]


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
    return (
        terminal.latency_ms
        if terminal is not None and terminal.latency_ms is not None
        else 0
    )
