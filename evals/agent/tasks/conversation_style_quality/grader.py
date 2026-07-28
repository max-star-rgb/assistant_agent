"""Task-local conversational response quality grader."""

from __future__ import annotations

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


CONVERSATIONAL_RESPONSE_QUALITY_CRITERION_ID = "conversational_response_quality"
CONVERSATIONAL_RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 回答必须正确承接上一轮语境，理解“第二种”是周五开半小时同步会，并围绕团队只有六个人、
   项目变化快、同步对齐效率和保留简短纪要解释推荐理由。
2. 直接回答当前追问，不机械复述用户问题或大段重述双方已经知道的背景。
3. 对这个一两段即可说清的简单追问，不使用标题、小标题、编号、项目符号、表格，或
   “结论/原因/建议”等报告式模板标签。
4. 语气自然、简洁、像连续对话；不得用客服式寒暄、虚构情绪或过度亲昵称呼制造拟人感。
只有四项同时满足才通过。不要因为事实大致正确而忽略不必要结构化，也不要仅凭没有 Markdown
就判定自然承接合格。
""".strip()


def grade(
    evidence: RunEvidence,
    judge: LLMJudge,
) -> GraderResult:
    response_quality = judge.evaluate(
        criterion_id=CONVERSATIONAL_RESPONSE_QUALITY_CRITERION_ID,
        rubric=CONVERSATIONAL_RESPONSE_QUALITY_RUBRIC,
        evidence=evidence,
    )
    return grader_result(
        tool_execution=dimension(
            {
                "runtime_completed": _runtime_completed(evidence),
                "no_tools_exposed": _no_tools_exposed(evidence),
            }
        ),
        tool_use=dimension(
            {
                "no_tool_activity": _no_tool_activity(evidence),
            }
        ),
        state=dimension(
            {
                "expected_state_unchanged": _expected_state_unchanged(evidence),
            }
        ),
        response=dimension(
            {
                CONVERSATIONAL_RESPONSE_QUALITY_CRITERION_ID: judge_assertion(
                    response_quality,
                    criterion_id=CONVERSATIONAL_RESPONSE_QUALITY_CRITERION_ID,
                    label="对话自然度与必要结构控制",
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


def _no_tools_exposed(evidence: RunEvidence) -> AssertionResult:
    return rule_assertion(
        evidence.available_tools == [],
        f"available_tools={evidence.available_tools}",
        label="表达任务未暴露工具",
    )


def _no_tool_activity(evidence: RunEvidence) -> AssertionResult:
    passed = not evidence.tool_executions and not evidence.validation_results
    return rule_assertion(
        passed,
        (
            f"tool_execution_count={len(evidence.tool_executions)}, "
            f"validation_count={len(evidence.validation_results)}"
        ),
        label="表达任务未产生工具活动",
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
