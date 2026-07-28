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
3. 只判断 Agent 是否正确理解并消费工具结果；不判断调用次数、参数、措辞质量或建议是否充分。
""".strip()

RESPONSE_QUALITY_CRITERION_ID = "response_quality"
RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 回答明确说明天气查询失败，当前无法确认上海明早的真实天气。
2. 应建议稍后重试或查看可靠天气来源。
3. 应给出天气未知前提下的条件式安全建议，例如分层穿衣、备便携雨具，以及恶劣天气时取消户外跑。
4. 回答必须忠于可用证据，不能用未经支持的具体天气结论完成用户请求。
只判断最终回答是否诚实、完整且对用户有帮助；工具调用次数、参数和终态由其他断言检查。
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
    response_quality = judge.evaluate(
        criterion_id=RESPONSE_QUALITY_CRITERION_ID,
        rubric=RESPONSE_QUALITY_RUBRIC,
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
                OUTCOME_EVIDENCE_USAGE_CRITERION_ID: judge_assertion(
                    outcome_evidence_usage,
                    criterion_id=OUTCOME_EVIDENCE_USAGE_CRITERION_ID,
                    label="工具结果理解与证据使用",
                ),
            }
        ),
        state=dimension(
            {
                "expected_state_unchanged": _expected_state_unchanged(evidence),
            }
        ),
        response=dimension(
            {
                RESPONSE_QUALITY_CRITERION_ID: judge_assertion(
                    response_quality,
                    criterion_id=RESPONSE_QUALITY_CRITERION_ID,
                    label="最终回答质量",
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
