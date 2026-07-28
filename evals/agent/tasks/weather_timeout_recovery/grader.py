"""Task-local Rule assertions and LLM Judge criteria."""

from __future__ import annotations

from datetime import date, timedelta

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


OUTCOME_EVIDENCE_USAGE_CRITERION_ID = "outcome_evidence_usage"
OUTCOME_EVIDENCE_USAGE_RUBRIC = """
通过条件：
1. Agent 必须把 weather 的失败结果理解为“没有获得可用天气事实”，不得暗示查询成功。
2. 回答中的天气事实必须能由工具 Evidence 支持；工具失败时，不得虚构温度、降水、风力、
   天气状况或“适合跑步”的确定结论。
判定边界：只判断 Agent 是否正确理解并消费工具结果；不判断调用次数、参数、措辞质量或建议是否充分。
""".strip()


def grade(
    evidence: RunEvidence,
    judge: LLMJudge,
) -> GraderResult:
    outcome_evidence_usage = judge.evaluate(
        criterion_id=OUTCOME_EVIDENCE_USAGE_CRITERION_ID,
        rubric=OUTCOME_EVIDENCE_USAGE_RUBRIC,
        evidence=evidence,
    )
    return grader_result(
        tool_execution=dimension(
            {
                "runtime_completed": _runtime_completed(evidence),
                "expected_tool_exposed": _expected_tool_exposed(evidence),
                "validation_accepted": _validation_accepted(evidence),
                "tool_lifecycle_closed": _tool_lifecycle_closed(evidence),
            }
        ),
        tool_use=dimension(
            {
                "weather_called_once": _weather_called_once(evidence),
                "weather_arguments_correct": _weather_arguments_correct(evidence),
            }
        ),
        state=dimension(
            {
                "expected_state_unchanged": _expected_state_unchanged(evidence),
            }
        ),
        response=dimension(
            {
                "response_generated": _response_generated(evidence),
                OUTCOME_EVIDENCE_USAGE_CRITERION_ID: judge_assertion(
                    outcome_evidence_usage,
                    criterion_id=OUTCOME_EVIDENCE_USAGE_CRITERION_ID,
                    label="回答忠于工具证据",
                ),
            }
        ),
    )


def _runtime_completed(evidence: RunEvidence) -> AssertionResult:
    passed = evidence.terminal_status == "completed"
    return rule_assertion(
        passed,
        f"terminal_status={evidence.terminal_status}",
        label="Runtime 正常完成",
    )


def _expected_tool_exposed(evidence: RunEvidence) -> AssertionResult:
    passed = evidence.available_tools == ["weather"]
    return rule_assertion(
        passed,
        f"available_tools={evidence.available_tools}",
        label="目标工具已向 Agent 暴露",
    )


def _validation_accepted(evidence: RunEvidence) -> AssertionResult:
    statuses = [result.status for result in evidence.validation_results]
    passed = len(statuses) == len(evidence.tool_executions) and all(
        status == "accepted" for status in statuses
    )
    return rule_assertion(
        passed,
        (
            f"validation_statuses={statuses}, "
            f"tool_execution_count={len(evidence.tool_executions)}"
        ),
        label="工具调用通过 Action Validator",
    )


def _tool_lifecycle_closed(evidence: RunEvidence) -> AssertionResult:
    executions = evidence.tool_executions
    passed = bool(executions) and all(
        execution.exposed
        and (
            (
                execution.terminal_event == "tool.finished"
                and execution.error_code is None
            )
            or (
                execution.terminal_event == "tool.failed"
                and execution.error_code is not None
            )
        )
        for execution in executions
    )
    return rule_assertion(
        passed,
        (
            "terminal_events="
            f"{[execution.terminal_event for execution in executions]}, "
            "error_codes="
            f"{[execution.error_code for execution in executions]}, "
            "exposed="
            f"{[execution.exposed for execution in executions]}"
        ),
        label="工具调用生命周期完整闭合",
    )


def _weather_called_once(evidence: RunEvidence) -> AssertionResult:
    names = [execution.name for execution in evidence.tool_executions]
    passed = names == ["weather"]
    return rule_assertion(
        passed,
        f"tool_calls={names}",
        label="天气工具调用次数符合策略",
    )


def _weather_arguments_correct(evidence: RunEvidence) -> AssertionResult:
    if len(evidence.tool_executions) != 1:
        return rule_assertion(
            False,
            "无法在非单次调用上验证参数。",
            label="天气查询参数正确",
        )
    arguments = evidence.tool_executions[0].input
    location = str(arguments.get("location") or "")
    target_date = str(arguments.get("target_date") or "")
    expected_date = (date.today() + timedelta(days=1)).isoformat()
    passed = "上海" in location and target_date == expected_date
    return rule_assertion(
        passed,
        (
            f"location={location!r}, target_date={target_date!r}, "
            f"expected_date={expected_date!r}"
        ),
        label="天气查询参数正确",
    )


def _expected_state_unchanged(evidence: RunEvidence) -> AssertionResult:
    changed = any(
        evidence.state_diff.get(key) for key in ("added", "modified", "deleted")
    )
    return rule_assertion(
        not changed,
        f"state_diff={evidence.state_diff}",
        label="只读任务未产生状态变更",
    )


def _response_generated(evidence: RunEvidence) -> AssertionResult:
    message = (
        str(evidence.response.get("message") or "").strip()
        if evidence.response is not None
        else ""
    )
    return rule_assertion(
        bool(message),
        f"response_present={bool(message)}",
        label="已生成面向用户的回答",
    )
