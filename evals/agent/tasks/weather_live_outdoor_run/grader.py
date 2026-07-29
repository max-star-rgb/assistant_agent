"""Task-local grader for live weather grounded advice."""

from __future__ import annotations

from datetime import date, timedelta

from evals.agent.contracts import AssertionResult, GraderResult, LLMJudge, RunEvidence
from evals.agent.grading import (
    dimension,
    grader_result,
    judge_assertion,
    rule_assertion,
)


WEATHER_ANSWER_GROUNDED_CRITERION_ID = "weather_answer_grounded"
WEATHER_ANSWER_GROUNDED_RUBRIC = """
通过条件：
1. weather 必须成功返回非空 forecast；回答实际引用的天气数值和状况必须能在 Tool Evidence 中找到。
2. high_c 与 low_c 可以准确表述为温度区间；未复述 temperature_c 或其他字段不构成遗漏或虚构。
3. Evidence 内部出现看似矛盾的字段时（例如 Clear sky 与高降水概率并存），如实报告这些字段并采取
   谨慎建议可以通过，不要求 Agent 擅自修正上游数据。
4. 回答应回应是否适合户外跑、穿着和是否带雨具；建议必须由 Evidence 支持。回答不得添加 Evidence
   未提供的具体时段数值，也不得给出与降水、温度等证据明显冲突的确定建议。
5. 不要求固定措辞、固定建议或复述全部字段，只判断实际陈述的证据忠实性和用户目标是否得到回应。
""".strip()


def grade(evidence: RunEvidence, judge: LLMJudge) -> GraderResult:
    grounded = judge.evaluate(
        criterion_id=WEATHER_ANSWER_GROUNDED_CRITERION_ID,
        rubric=WEATHER_ANSWER_GROUNDED_RUBRIC,
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
            {"expected_state_unchanged": _expected_state_unchanged(evidence)}
        ),
        response=dimension(
            {
                "response_generated": _response_generated(evidence),
                WEATHER_ANSWER_GROUNDED_CRITERION_ID: judge_assertion(
                    grounded,
                    criterion_id=WEATHER_ANSWER_GROUNDED_CRITERION_ID,
                    label="回答忠于真实天气证据并回应用户目标",
                ),
            }
        ),
    )


def _runtime_completed(evidence: RunEvidence) -> AssertionResult:
    return rule_assertion(
        evidence.terminal_status == "completed",
        f"terminal_status={evidence.terminal_status}",
        label="Runtime 正常完成",
    )


def _expected_tool_exposed(evidence: RunEvidence) -> AssertionResult:
    return rule_assertion(
        "weather" in evidence.available_tools and len(evidence.available_tools) > 1,
        f"available_tools={evidence.available_tools}",
        label="完整目录中包含真实天气工具",
    )


def _validation_accepted(evidence: RunEvidence) -> AssertionResult:
    statuses = [item.status for item in evidence.validation_results]
    passed = len(statuses) == len(evidence.tool_executions) and all(
        status == "accepted" for status in statuses
    )
    return rule_assertion(
        passed,
        (
            f"validation_statuses={statuses}, "
            f"tool_execution_count={len(evidence.tool_executions)}"
        ),
        label="天气工具调用通过 Action Validator",
    )


def _tool_lifecycle_closed(evidence: RunEvidence) -> AssertionResult:
    executions = evidence.tool_executions
    passed = bool(executions) and all(
        item.exposed
        and item.terminal_event == "tool.finished"
        and item.error_code is None
        for item in executions
    )
    return rule_assertion(
        passed,
        (
            f"terminal_events={[item.terminal_event for item in executions]}, "
            f"error_codes={[item.error_code for item in executions]}"
        ),
        label="真实天气工具成功完成",
    )


def _weather_called_once(evidence: RunEvidence) -> AssertionResult:
    names = [item.name for item in evidence.tool_executions]
    return rule_assertion(
        names == ["weather"],
        f"tool_calls={names}",
        label="天气工具仅调用一次",
    )


def _weather_arguments_correct(evidence: RunEvidence) -> AssertionResult:
    if len(evidence.tool_executions) != 1:
        return rule_assertion(
            False,
            "无法在非单次调用上验证参数。",
            label="真实天气查询参数正确",
        )
    arguments = evidence.tool_executions[0].input
    location = str(arguments.get("location") or "").strip().lower()
    target_date = str(arguments.get("target_date") or "")
    expected_date = (date.today() + timedelta(days=1)).isoformat()
    passed = (
        ("上海" in location or "shanghai" in location)
        and target_date == expected_date
    )
    return rule_assertion(
        passed,
        (
            f"location={location!r}, target_date={target_date!r}, "
            f"expected_date={expected_date!r}"
        ),
        label="真实天气查询参数正确",
    )


def _expected_state_unchanged(evidence: RunEvidence) -> AssertionResult:
    changed = any(
        evidence.state_diff.get(key) for key in ("added", "modified", "deleted")
    )
    return rule_assertion(
        not changed,
        f"state_diff={evidence.state_diff}",
        label="真实天气任务未产生状态变更",
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
        label="已生成面向用户的天气回答",
    )
