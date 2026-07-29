"""Shared aggregation for batch Task-local graders."""

from __future__ import annotations

from collections.abc import Callable

from evals.agent.contracts import (
    AssertionResult,
    GraderResult,
    LLMJudge,
    RunEvidence,
)
from evals.agent.grading import (
    dimension,
    grader_result,
    judge_assertion,
    rule_assertion,
)
from evals.agent.task_support import (
    expected_tools_exposed,
    no_tool_execution,
    optional_successful_tool_lifecycle,
    response_generated,
    runtime_completed,
    state_unchanged,
    successful_tool_lifecycle,
    tool_sequence,
    validations_accepted,
)


ArgumentCheck = Callable[[RunEvidence], tuple[bool, str]]


def grade_case(
    evidence: RunEvidence,
    judge: LLMJudge,
    *,
    criterion_id: str,
    rubric: str,
    expected_tools: tuple[str, ...],
    expected_sequence: list[str] | None,
    argument_check: ArgumentCheck | None = None,
    state_changes: bool = False,
) -> GraderResult:
    verdict = judge.evaluate(
        criterion_id=criterion_id,
        rubric=rubric,
        evidence=evidence,
    )
    arguments = (
        _arguments_assertion(evidence, argument_check)
        if argument_check is not None
        else rule_assertion(True, "该 Task 无额外参数约束。", label="工具参数符合任务")
    )
    return grader_result(
        tool_execution=dimension(
            {
                "runtime_completed": runtime_completed(evidence),
                "expected_tools_exposed": expected_tools_exposed(
                    evidence, *expected_tools
                ),
                "validation_accepted": validations_accepted(evidence),
                "tool_lifecycle": _tool_lifecycle(
                    evidence,
                    expected_sequence,
                ),
            }
        ),
        tool_use=dimension(
            {
                "tool_sequence": _tool_sequence(evidence, expected_sequence),
                "tool_arguments": arguments,
            }
        ),
        state=dimension(
            {
                "expected_state": (
                    _state_changed(evidence)
                    if state_changes
                    else state_unchanged(evidence)
                )
            }
        ),
        response=dimension(
            {
                "response_generated": response_generated(evidence),
                criterion_id: judge_assertion(
                    verdict,
                    criterion_id=criterion_id,
                    label="回答满足 Task 专属语义条件",
                ),
            }
        ),
    )


def _tool_lifecycle(
    evidence: RunEvidence,
    expected_sequence: list[str] | None,
) -> AssertionResult:
    if expected_sequence is None:
        return optional_successful_tool_lifecycle(evidence)
    if expected_sequence:
        return successful_tool_lifecycle(evidence)
    return no_tool_execution(evidence)


def _tool_sequence(
    evidence: RunEvidence,
    expected_sequence: list[str] | None,
) -> AssertionResult:
    if expected_sequence is not None:
        return tool_sequence(evidence, expected_sequence)
    return rule_assertion(
        True,
        f"tool_calls={[item.name for item in evidence.tool_executions]}",
        label="辅助工具选择不属于本 Task 判定范围",
    )


def _arguments_assertion(
    evidence: RunEvidence,
    check: ArgumentCheck,
) -> AssertionResult:
    passed, reason = check(evidence)
    return rule_assertion(passed, reason, label="工具参数符合任务")


def _state_changed(evidence: RunEvidence) -> AssertionResult:
    final_calendar = evidence.final_state.get("calendar", {})
    events = (
        final_calendar.get("events", [])
        if isinstance(final_calendar, dict)
        else []
    )
    passed = (
        evidence.state_diff.get("modified") == ["calendar"]
        and len(events) == 1
    )
    return rule_assertion(
        passed,
        f"state_diff={evidence.state_diff}, event_count={len(events)}",
        label="隔离日历仅提交一个事件",
    )
