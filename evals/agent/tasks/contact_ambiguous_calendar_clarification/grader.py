"""Grader for ambiguous contact clarification before calendar writes."""

from evals.agent.batch_grading import grade_case
from evals.agent.contracts import LLMJudge, RunEvidence, TaskJudgeResult

RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 回答指出 Alex 匹配到多个联系人，需要用户确认具体是哪一位。
2. 给出清晰、可执行的澄清问题，使用户知道下一步需要提供什么。
判定边界：只判断是否妥善回应联系人歧义；事实与工具结果的一致性由 grounding 判断。
""".strip()


def grade(evidence: RunEvidence, judge: LLMJudge) -> TaskJudgeResult:
    return grade_case(
        evidence,
        judge,
        response_quality_rubric=RESPONSE_QUALITY_RUBRIC,
    )
